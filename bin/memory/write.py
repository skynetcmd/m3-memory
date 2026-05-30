from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from embedding_utils import (
    infer_change_agent as _infer_change_agent_util,
)
from embedding_utils import (
    pack as _pack,
)
from embedding_utils import (
    unpack as _unpack,
)
from m3_sdk import M3Context, resolve_db_path

from . import embed as _embed_mod
from .config import (
    AUTO_RELATED_LINK,
    AUTO_RELATED_LINK_SCOPE_BY_VARIANT,
    CONTRADICTION_THRESHOLD,
    CONTRADICTION_TITLE_GATE,
    CONTRADICTION_TYPE_EXCLUSIONS,
    DEFAULT_CHANGE_AGENT,
    ENABLE_FACT_ENRICHED,
    INGEST_EVENT_ROWS,
    INGEST_GIST_ROWS,
    INGEST_WINDOW_CHUNKS,
    ORIGIN_DEVICE,
    VALID_SCOPES,
)
from .db import _db as _canonical_db


def _db(*args, **kwargs):
    """Connection context manager — dynamic-lookup wrapper.

    Tests monkeypatch `memory_core._db` (pre-refactor pattern) to inject
    a fake DB; honor that override at call time. If no patch is in
    place, route to the canonical `memory.db._db`.

    This wrapper lives in memory.write specifically (not all consumers
    need it) because the bulk + write impls moved here in the Phase 7+8
    refactor and a handful of regression tests pin patching behavior
    that pre-dated the move.
    """
    try:
        import memory_core as _mc  # type: ignore
        override = getattr(_mc, "_db", None)
        # Avoid infinite recursion if memory_core._db is the canonical
        # `with` shim that simply re-exports us.
        if override is not None and override is not _db:
            return override(*args, **kwargs)
    except ImportError:
        pass
    return _canonical_db(*args, **kwargs)
from .embed import (
    _DENSE_ERR_RE,
    _EMBED_GGUF_MODEL_TAG,
    _EMBED_SEM,
    _augment_embed_text_with_anchors,
    _chunk_for_sliding_window,
    _content_hash,
    _embed,
    _embedded_label,
    _get_embedded_embedder,
    _record_embed_backend,
    _subdivide_dense_chunk,
)
from .emitters import _maybe_emit_event_rows, _maybe_emit_gist_row, _maybe_emit_window_chunk
from .enrich import _auto_classify, _ingest_llm_enabled, _maybe_auto_entities, _maybe_auto_title, _try_enrich_or_enqueue
from .entity import _try_extract_or_enqueue
from .fts import _augment_title_with_role
from .util import _batch_cosine, _check_content_safety
from .util import sha256_hex as _sha256_hex

logger = logging.getLogger("memory.write")


# ── Internal callback registry ──────────────────────────────────────────────
# To break circular dependencies with memory_core (which imports this package),
# we lazily bind core-shim callbacks here.
_MC_CALLBACKS_BOUND = False
_MC_CALLBACK_NAMES = (
    "_track_cost",
    "_record_history",
)

# Forward declarations for the deferred-bound callbacks above. These None
# placeholders are overwritten by `_resolve_mc_callbacks()` before any impl
# runs; they exist so static analysis sees the names defined at module scope.
# Typed as Callable (the runtime binding is always a function) so callers
# are not flagged as calling None.
_track_cost: Callable[..., object] = None  # type: ignore[assignment]
_record_history: Callable[..., object] = None  # type: ignore[assignment]

def _resolve_mc_callbacks() -> None:
    global _MC_CALLBACKS_BOUND
    if _MC_CALLBACKS_BOUND:
        return
    try:
        import memory_core
        for name in _MC_CALLBACK_NAMES:
            globals()[name] = getattr(memory_core, name)
        _MC_CALLBACKS_BOUND = True
    except (ImportError, AttributeError) as e:
        logger.warning(f"Failed to bind memory_core callbacks for write path: {e}")

def _ctx() -> M3Context:
    return M3Context.for_db(resolve_db_path(None))


def memory_link_impl(from_id: str, to_id: str, relationship_type: str = "related", db=None) -> str:
    """Creates a directional link between two memory items. Valid types:
    related, supports, contradicts, extends, supersedes, references,
    consolidates, message, handoff.
    """
    with _db(db) as db_conn:
        db_conn.execute(
            "INSERT OR REPLACE INTO memory_relationships (from_id, to_id, relationship_type) VALUES (?, ?, ?)",
            (from_id, to_id, relationship_type)
        )
    return f"Linked {from_id} --[{relationship_type}]--> {to_id}"

async def memory_write_impl(
type, content, title="", metadata="{}", agent_id="", model_id="", change_agent="", importance=0.5, source="agent", embed=True, user_id="", scope="agent", valid_from="", valid_to="", auto_classify=False, conversation_id="", refresh_on="", refresh_reason="", variant=None, embed_text=None, fact_enricher: "Callable[[str], Awaitable[list[dict]]] | None" = None, fact_enricher_variant_allowlist: "set[str] | None" = None, entity_extractor: "Callable[[str], Awaitable[dict]] | None" = None, entity_extractor_variant_allowlist: "set[str] | None" = None):
    _resolve_mc_callbacks()
    """Internal implementation for memory_write. Contradiction detection is automatic.

    `variant` tags the item with a free-form ingestion-pipeline identifier so
    multiple variants (e.g. "baseline", "heuristic_c1c4", "llm_v1") can coexist
    and be compared. Default None = untagged.

    `embed_text` overrides the default text fed to the embedder (which is
    `content or title`). Useful when callers want to enrich the embedding with
    titles/entities without polluting the displayed content.

    `fact_enricher` is an optional async callable that extracts facts from content.
    `fact_enricher_variant_allowlist` controls which variants get enriched (default:
    None means skip all variants).
    """
    if isinstance(metadata, dict):
        metadata = json.dumps(metadata)
    elif not isinstance(metadata, str):
        metadata = "{}"
    _track_cost("write_calls")

    if auto_classify and (not type or type == "auto"):
        type = await _auto_classify(content, title)

    # Leak gate: reject `window:*` summary rows when the variant is NULL.
    # See bulk-write impl for the same gate + history (task #189, memory
    # 372f49b0). Mirrored here for the singleton path so misconfigured
    # bench callers who write items individually don't slip through.
    if (
        type == "summary"
        and isinstance(title, str)
        and title.startswith("window:")
        and not variant
    ):
        return (
            "Error: window:* summary rows require an explicit variant "
            "(rejected to prevent core-memory leak; see task #189)."
        )

    # Defense-in-depth content size check (primary validation is in memory_bridge.py)
    if content and len(content) > 50_000:
        return f"Error: content too large ({len(content)} chars, max 50000)"
    safety_err = _check_content_safety(content)
    if safety_err:
        return safety_err
    if scope not in VALID_SCOPES:
        scope = "agent"
    item_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        importance = float(importance)
    except (TypeError, ValueError):
        importance = 0.5
    agent = change_agent.strip().lower() or _infer_change_agent_util(agent_id, model_id, default=DEFAULT_CHANGE_AGENT)

    # Session-scoped memories auto-expire in 24 hours
    expires_at = None
    if scope == "session":
        from datetime import timedelta
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    # Opt-in ingest-time enrichment (env-gated, fail-open).
    title = await _maybe_auto_title(content or "", title)
    title = _augment_title_with_role(title, metadata)
    if _ingest_llm_enabled("M3_INGEST_AUTO_ENTITIES"):
        ents = await _maybe_auto_entities(content or "")
        if ents:
            try:
                meta_dict = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
            except json.JSONDecodeError:
                meta_dict = {}
            if isinstance(meta_dict, dict) and "entities" not in meta_dict:
                meta_dict["entities"] = ents
                metadata = json.dumps(meta_dict)

    with _db() as db:
        _vf = valid_from or now
        # Canonicalize "open-ended validity" as NULL, not "". The as_of range
        # predicate in memory_search_scored_impl historically had to allow both
        # NULL and "" because the single-write path stored "" while the bulk
        # path stored either; normalizing at write time lets future read paths
        # rely on NULL alone without carrying that compat clause forever.
        _vt = valid_to or None
        _cid = conversation_id or None
        _ron = refresh_on or None
        _rreason = refresh_reason or None
        # Same story for variant — MCP schema default is "" but search filters
        # untagged rows with `variant IS NULL`.
        _variant = variant or None
        db.execute(
            "INSERT INTO memory_items (id, type, title, content, metadata_json, agent_id, model_id, change_agent, importance, source, origin_device, user_id, scope, expires_at, created_at, valid_from, valid_to, conversation_id, refresh_on, refresh_reason, variant) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, type, title, content, metadata, agent_id, model_id, agent, importance, source, ORIGIN_DEVICE, user_id, scope, expires_at, now, _vf, _vt, _cid, _ron, _rreason, _variant)
        )
        # NOTE: chroma_sync_queue insert moved below into the `if vec:` block
        # so embed failures don't leave orphan queue rows.
        db.execute("UPDATE memory_items SET content_hash = ? WHERE id = ?",
                   (_sha256_hex((content or "").encode("utf-8")), item_id))

    vec = None
    if embed:
        _et = _augment_embed_text_with_anchors(
            embed_text or content or title, metadata
        )
        # Sliding window: short inputs return a single (text, 0) and produce
        # one vector_kind='default' row (back-compat). Long inputs return N
        # windows and produce N vector_kind='window_<idx>' rows. Retrieval
        # picks across kinds with vector_kind_strategy='max'.
        chunks = _chunk_for_sliding_window(_et)

        # Dense-content recovery uses the in-process Rust embedder directly
        # to keep error context (the "input too long: NNNN tokens" message
        # is what we parse). Falls back to _embed() if the in-process
        # embedder isn't configured for this deployment.
        _direct_embedder = _get_embedded_embedder()

        async def _embed_chunk_with_dense_recovery(
            txt: str, base_kind: str,
        ) -> list[tuple[str, str, list[float], str]]:
            """Embed one chunk, recovering from dense-overflow if needed.

            Returns list of (sub_text, kind_suffix, vector, model_tag).
            kind_suffix is empty string for the no-recovery case, or
            '_dense_<j>' for sub-chunks created by recovery. Caller
            appends suffix to base_kind for vector_kind on insert.
            """
            # Fast path: caller has no in-process embedder configured —
            # fall through to _embed (which itself tries in-process first;
            # any error there will produce None and we just skip the chunk).
            if _direct_embedder is None:
                cvec, mm = await _embed(txt)
                if cvec:
                    return [(txt, "", cvec, mm)]
                return []
            # In-process path: catch input-too-long, recurse with smaller
            # sub-chunks sized by the observed chars/token ratio.
            try:
                cvec = await asyncio.to_thread(
                    lambda: _direct_embedder.embed([txt])[0]
                )
                if cvec:
                    _record_embed_backend(_embedded_label(), 1)
                    return [(txt, "", cvec, _EMBED_GGUF_MODEL_TAG)]
                return []
            except Exception as e:
                err = str(e)
                rmatch = _DENSE_ERR_RE.search(err)
                if not rmatch:
                    # Non-dense error: log and skip; this chunk won't get
                    # a vector. memory_items row is already persisted, so
                    # FTS-only retrieval still finds it.
                    logger.warning(
                        f"memory_write_impl: non-dense embed failure for {item_id} "
                        f"chunk base_kind={base_kind}: {err}"
                    )
                    return []
                observed_tokens = int(rmatch.group(1))
                subs = _subdivide_dense_chunk(txt, observed_tokens)
                logger.info(
                    f"memory_write_impl: dense overflow on {item_id} chunk base_kind={base_kind} "
                    f"({observed_tokens} tokens for {len(txt)} chars => "
                    f"{len(txt)/observed_tokens:.2f} c/t); subdividing into {len(subs)} sub-chunks"
                )
                results: list[tuple[str, str, list[float], str]] = []
                for j, sub in enumerate(subs):
                    try:
                        def _embed_one(s: str = sub) -> list[float]:
                            return _direct_embedder.embed([s])[0]
                        sv = await asyncio.to_thread(_embed_one)
                        if sv:
                            results.append((sub, f"_dense_{j}", sv, _EMBED_GGUF_MODEL_TAG))
                            _record_embed_backend(_embedded_label(), 1)
                    except Exception as se:
                        # Second-level failure: log and skip this sub-chunk.
                        # Don't recurse further — would mean truly pathological
                        # content where our chars/token estimate is wrong by
                        # >10%, which our 10% safety margin should already
                        # cover. Logging is sufficient.
                        logger.warning(
                            f"memory_write_impl: dense sub-chunk {j} of {len(subs)} still "
                            f"failed for {item_id}: {se}"
                        )
                return results

        first_vec: list[float] | None = None
        any_inserted = False
        for chunk_text, chunk_idx in chunks:
            base_kind = "default" if len(chunks) == 1 else f"window_{chunk_idx}"
            sub_results = await _embed_chunk_with_dense_recovery(chunk_text, base_kind)
            if not sub_results:
                logger.warning(
                    f"memory_write_impl: embed failed for {item_id} chunk {chunk_idx}; skipping that window"
                )
                continue
            for sub_text, kind_suffix, cvec, m in sub_results:
                kind = base_kind + kind_suffix
                with _db() as db:
                    db.execute(
                        "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash, vector_kind) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), item_id, _pack(cvec), m, len(cvec), now, _content_hash(sub_text), kind),
                    )
                any_inserted = True
                if first_vec is None:
                    first_vec = cvec
        if any_inserted:
            with _db() as db:
                # One chroma_sync_queue entry per memory_id, not per window.
                # Chroma sync replays whatever's currently in memory_embeddings
                # for the memory_id.
                db.execute(
                    "INSERT INTO chroma_sync_queue (memory_id, operation) VALUES (?,?)",
                    (item_id, "upsert"),
                )
            # Downstream code (contradiction check, MMR, etc.) needs *a*
            # vector for this memory. The first window's vector is the
            # closest analogue to the legacy single-vector behavior — it
            # represents the head of the augmented embed text.
            vec = first_vec
        else:
            logger.warning(
                f"memory_write_impl: all embed calls failed for {item_id}; "
                f"skipping memory_embeddings + chroma_sync_queue insert"
            )

    _record_history(item_id, "create", None, content, "content", agent_id or agent)

    # Fact enrichment (Phase 4). Non-blocking: tries semaphore, enqueues on miss.
    # Always succeeds — verbatim row is already persisted before enrichment.
    try:
        with _db() as db:
            await _try_enrich_or_enqueue(item_id, content or "", fact_enricher, db, variant=variant, allowlist=fact_enricher_variant_allowlist)
    except Exception as e:
        logger.debug(f"fact enrichment dispatch failed: {e}")

    # Entity extraction (Phase 4). Non-blocking: tries semaphore, enqueues on miss.
    # fact_enriched rows are NOT extracted to prevent recursion.
    if type != "fact_enriched":
        try:
            with _db() as db:
                await _try_extract_or_enqueue(
                    item_id, content or "", entity_extractor, db,
                    variant=variant, allowlist=entity_extractor_variant_allowlist,
                )
        except Exception as e:
            logger.debug(f"entity extraction dispatch failed: {e}")

    # Contradiction detection + auto-linking (runs after embedding is stored).
    # `variant` is threaded into _check_contradictions so candidates respect the
    # M3_AUTO_RELATED_LINK_SCOPE_BY_VARIANT scope rule (default ON: same-variant
    # only when variant is set on the inserted item).
    superseded_ids: list[str] = []
    if vec and type not in ("conversation", "message"):
        superseded_ids, related_candidates = await _check_contradictions(
            item_id, content, title, vec, type, agent_id, variant=variant,
        )
        # Auto-link top related (non-contradictory) memory. Gated by
        # M3_AUTO_RELATED_LINK (default ON for back-compat). Disable in any
        # deployment where you want only explicit `memory_link` calls or where
        # edge curation is handled by an offline tool.
        if AUTO_RELATED_LINK and related_candidates and not superseded_ids:
            best_id, best_score = related_candidates[0]
            try:
                memory_link_impl(item_id, best_id, "related")
                logger.debug(f"Auto-linked {item_id} -> {best_id} (score={best_score:.3f})")
            except Exception:
                pass

    # Opt-in ingestion emitters. Each one is gated off by default and fails
    # open — errors are logged but never propagate to the caller. They only
    # fire for 'message' rows; other types (facts, notes, etc.) are skipped
    # since windowing/gist/event-extraction are conversation-shaped features.
    if type == "message" and _cid:
        try:
            if INGEST_EVENT_ROWS:
                await _maybe_emit_event_rows(
                    content or "", metadata, _cid, user_id, item_id
                )
            if INGEST_WINDOW_CHUNKS:
                await _maybe_emit_window_chunk(_cid, user_id)
            if INGEST_GIST_ROWS:
                await _maybe_emit_gist_row(_cid, user_id)
        except Exception as e:
            logger.debug(f"ingest emitter failed: {e}")

    result = f"Created: {item_id}"
    if superseded_ids:
        result += f" (superseded {len(superseded_ids)} conflicting memories: {', '.join(superseded_ids[:3])})"
    return result


def _mark_superseded(
    old_id: str,
    new_id: str,
    *,
    close_at: str,
    db,
    actor_id: str = "",
    prev_value: str | None = None,
    require_active: bool = False,
) -> bool:
    """Mark ``old_id`` as superseded by ``new_id`` — the shared supersede write.

    Closes ``old_id``'s validity interval (``is_deleted=1``, ``valid_to``),
    writes a ``supersedes`` relationship edge new -> old, and records a
    ``supersede`` history event. All writes go through the caller's already-open
    connection ``db`` so they land in one transaction (and don't open a second
    pool connection — see ``_record_history``'s note on WAL writer contention).

    ``valid_to`` is set with ``COALESCE(NULLIF(valid_to, ''), ?)``: migration 010
    declared ``valid_to TEXT DEFAULT ''``, so a normally-created row has
    ``valid_to=''`` not ``NULL``. A bare ``COALESCE(valid_to, ?)`` would treat
    ``''`` as already-set and never close the interval; ``NULLIF`` maps ``''``
    back to ``NULL`` so ``COALESCE`` fills it, while a genuinely earlier
    ``valid_to`` is preserved.

    When ``require_active`` is True the UPDATE is gated on ``is_deleted = 0`` and
    the return value reports whether a row was actually closed — callers use it
    as a concurrency guard (a False means someone else closed ``old_id`` first,
    so the edge + history are skipped). With ``require_active`` False the close
    is unconditional and the function always returns True; this is the path for
    callers (e.g. ``_check_contradictions``) that already hold the writer and
    selected ``old_id`` from an ``is_deleted=0`` candidate set.

    ``prev_value`` is recorded as the history event's previous value when the
    caller has the old content cheaply to hand (``_check_contradictions`` does);
    otherwise leave it None.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    # Two full literal statements rather than an f-string'd WHERE clause: keeps
    # every SQL string a complete, greppable, parameterized literal (the `where`
    # token would be module-controlled, not user input, but a static SQL scan
    # can't know that — don't make it guess).
    if require_active:
        cur = db.execute(
            "UPDATE memory_items SET is_deleted = 1, "
            "valid_to = COALESCE(NULLIF(valid_to, ''), ?), updated_at = ? "
            "WHERE id = ? AND is_deleted = 0",
            (close_at, now_iso, old_id),
        )
    else:
        cur = db.execute(
            "UPDATE memory_items SET is_deleted = 1, "
            "valid_to = COALESCE(NULLIF(valid_to, ''), ?), updated_at = ? "
            "WHERE id = ?",
            (close_at, now_iso, old_id),
        )
    if require_active and cur.rowcount != 1:
        # old_id was closed concurrently between the caller's pre-check and now.
        return False
    db.execute(
        "INSERT INTO memory_relationships "
        "(id, from_id, to_id, relationship_type, created_at) VALUES (?,?,?,?,?)",
        (str(uuid.uuid4()), new_id, old_id, "supersedes", now_iso),
    )
    _record_history(old_id, "supersede", prev_value, new_id, "content", actor_id, db=db)
    return True


async def memory_supersede_impl(
    old_id: str,
    content: str,
    type: str = "",
    title: str = "",
    metadata: str = "{}",
    agent_id: str = "",
    model_id: str = "",
    change_agent: str = "",
    importance: float = -1.0,
    source: str = "agent",
    embed: bool = True,
    user_id: str = "",
    scope: str = "",
    valid_from: str = "",
    variant: str | None = None,
    embed_text: str | None = None,
) -> str:
    """Explicitly supersede memory ``old_id`` with a new memory.

    Unlike ``memory_write``'s automatic contradiction detection — which fires on
    a cosine + title heuristic and may or may not link the right prior memory —
    this targets a *specific* ``old_id`` deterministically. Use it to record an
    intentional update ("this fact replaces that one").

    Non-destructive, mirroring the bi-temporal supersede pattern already used by
    ``_check_contradictions``: the old row is retained, marked ``is_deleted=1``
    with ``valid_to`` closed at ``valid_from`` (or now), and a ``supersedes``
    edge is written new -> old. The old memory stays retrievable by id and via
    history; it is just excluded from default search. ``as_of``-filtered
    retrieval still sees it valid before the supersession point.

    Inheritance sentinels (chosen so the value works both when called directly
    and when delivered by the MCP typed-function layer, which substitutes the
    catalog's schema default rather than ``None``):
      * ``type`` / ``title`` / ``scope`` — empty string means "inherit from the
        old memory".
      * ``importance`` — any value < 0 (default ``-1.0``) means "inherit"; a
        real importance is always in ``[0.0, 1.0]``.
    So the caller passes only what changed.

    Returns ``"Superseded <old_id> -> Created: <new_id>"`` on success, or an
    ``"Error: ..."`` string (the old row is left untouched on any error).

    Concurrency: the step-4 UPDATE carries ``WHERE is_deleted = 0`` and the
    supersession is committed only if it changes a row. If a concurrent
    supersede/delete closed ``old_id`` between the step-1 read and step-4, this
    detects it, rolls back the just-created replacement, and returns an error —
    so two racing callers can never both replace the same memory.
    """
    _resolve_mc_callbacks()

    # 1. Fast pre-check: exists + active. This is NOT the correctness guard
    #    (step 4's conditional UPDATE is) — it just fails cheaply, before the
    #    expensive embed, on the common case, and supplies the fields to
    #    inherit. A concurrent close after this read is caught at step 4.
    with _db() as db:
        row = db.execute(
            "SELECT type, title, importance, scope, is_deleted "
            "FROM memory_items WHERE id = ?",
            (old_id,),
        ).fetchone()
    if row is None:
        return f"Error: memory {old_id} not found — nothing to supersede"
    if row["is_deleted"]:
        return (
            f"Error: memory {old_id} is already deleted/superseded "
            "(supersede an active memory, or write a fresh one)"
        )

    # 2. Inherit unspecified fields from the old memory — the caller passes
    #    only what changed. See the docstring for the sentinel convention:
    #    empty string for type/title/scope, importance < 0 for importance.
    new_type = type if type else row["type"]
    new_title = title if title else (row["title"] or "")
    new_importance = importance if importance >= 0 else row["importance"]
    new_scope = scope if scope else (row["scope"] or "agent")

    # 3. Create the replacement via memory_write_impl — reuses embedding,
    #    history, enrichment, and the size/safety gates. Done BEFORE touching
    #    the old row so a write failure leaves the old memory fully intact.
    write_result = await memory_write_impl(
        type=new_type,
        content=content,
        title=new_title,
        metadata=metadata,
        agent_id=agent_id,
        model_id=model_id,
        change_agent=change_agent,
        importance=new_importance,
        source=source,
        embed=embed,
        user_id=user_id,
        scope=new_scope,
        valid_from=valid_from,
        variant=variant,
        embed_text=embed_text,
    )
    if not write_result.startswith("Created:"):
        # memory_write_impl returns "Error: ..." on a rejected write — propagate
        # it verbatim; the old memory has not been modified.
        return write_result
    # memory_write_impl's success string is "Created: <uuid>[ (superseded ...)]".
    # Extract the uuid defensively rather than trusting the exact format: if the
    # token isn't UUID-shaped the contract changed underneath us — fail loudly
    # instead of writing a supersedes edge to a garbage id.
    new_id = write_result.split("Created:", 1)[1].strip().split()[0]
    try:
        uuid.UUID(new_id)
    except (ValueError, AttributeError):
        return (
            f"Error: could not parse new memory id from memory_write result "
            f"({write_result!r}); supersede aborted"
        )

    # 4. Close the old memory's validity interval via the shared helper. With
    #    require_active=True its UPDATE is gated on is_deleted=0 and the return
    #    value is the real concurrency guard: False means old_id was closed by a
    #    concurrent supersede/delete after step 1's pre-check.
    close_at = valid_from or datetime.now(timezone.utc).isoformat()
    with _db() as db:
        won_race = _mark_superseded(
            old_id, new_id, close_at=close_at, db=db,
            actor_id=agent_id, require_active=True,
        )

    if not won_race:
        # The replacement created in step 3 is now orphaned — roll it back
        # (soft-delete) so a failed supersede leaves no live side effect.
        try:
            with _db() as db:
                db.execute(
                    "UPDATE memory_items SET is_deleted = 1, updated_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), new_id),
                )
        except Exception as e:  # pragma: no cover - best-effort cleanup
            logger.warning(f"failed to roll back orphaned memory {new_id}: {e}")
        return (
            f"Error: memory {old_id} was superseded or deleted concurrently; "
            f"this supersede was rolled back (orphan {new_id} soft-deleted)"
        )

    logger.info(f"Memory {new_id} explicitly supersedes {old_id} (valid_to={close_at})")
    try:
        from audit_trail import write_audit_entry
        write_audit_entry(
            action="memory_supersede",
            target_id=old_id,
            metadata={"new_id": new_id, "valid_to": close_at, "agent_id": agent_id}
        )
    except Exception as e:
        logger.warning(f"Failed to write audit trail entry for supersede: {e}")
    return f"Superseded {old_id} -> Created: {new_id}"


async def memory_write_bulk_impl(

    items: list[dict],
    *,
    enrich: bool | None = None,
    check_contradictions: bool | None = None,
    emit_conversation: bool | None = None,
    variant: str | None = None,
    embed_key_enricher: "Callable[[str, dict], Awaitable[str]] | None" = None,
    embed_key_enricher_concurrency: int = 4,
    dual_embed: bool = False,
    fact_enricher: "Callable[[str], Awaitable[list[dict]]] | None" = None,
    fact_enricher_concurrency: int = 2,
    fact_enricher_variant_allowlist: set[str] | None = None,
    entity_extractor: "Callable[[str], Awaitable[dict]] | None" = None,
    entity_extractor_concurrency: int = 2,
    entity_extractor_variant_allowlist: "set[str] | None" = None,
) -> list[str]:
    _resolve_mc_callbacks()
    """Bulk write that routes embeddings through `_embed_many`. Intended for
    benchmark / import paths where per-item contradiction detection would
    dominate wall-clock. Returns a list of item_ids (or empty string on failure).

    enrich=None means "inherit env gates" (M3_INGEST_AUTO_TITLE, M3_INGEST_AUTO_ENTITIES).
    True forces on, False forces off.

    check_contradictions=None means "off by default in bulk" (perf), True enables,
    False disables. Differs from single path because bulk may have thousands of items.

    emit_conversation=None means "on if conversation_id present and type==message"
    (mirror single path), False disables.

    variant is used as default when items don't set their own variant.

    enrich, check_contradictions, and emit_conversation are intentionally not
    exposed via MCP — they are bulk-only perf knobs used by benchmark and
    import drivers. Only variant is advertised on the memory_write MCP schema
    and via --variant on bench CLIs.

    dual_embed=True (default False) combines with embed_key_enricher to write
    TWO vectors per item instead of one: a 'default'-kind vector from the
    raw `content` (what single-session terse queries match best) AND an
    'enriched'-kind vector from the SLM-enriched embed_text (what multi-hop
    aggregation queries match best). Requires v022+ schema. When dual_embed
    is False (default), the enricher's output replaces the raw content in
    embed_text as before — single-vector, original behavior. When True but
    enricher is None, dual_embed is a no-op (only one thing to embed).

    Retrieval-side fusion (vector_kind_strategy kwarg on
    memory_search_scored_impl, upcoming commit) decides how to combine the
    two vectors at query time. 'max' takes per-memory_id max score across
    kinds.
    """
    if not items:
        return []

    now = datetime.now(timezone.utc).isoformat()
    prepared: list[dict] = []
    for it in items:
        mid = it.get("id") or str(uuid.uuid4())
        meta = it.get("metadata", "{}")
        if isinstance(meta, dict):
            meta = json.dumps(meta)
        scope = it.get("scope", "agent")
        if scope not in VALID_SCOPES:
            scope = "agent"
        content = it.get("content") or ""
        title = it.get("title") or ""
        agent = (
            (it.get("change_agent") or "").strip().lower()
            or _infer_change_agent_util(
                it.get("agent_id", ""), it.get("model_id", ""), default=DEFAULT_CHANGE_AGENT
            )
        )
        try:
            importance = float(it.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        expires_at = None
        if scope == "session":
            from datetime import timedelta
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=24)
            ).isoformat()

        # Resolve auto_classify before adding to prepared
        item_type = it.get("type", "note")
        if it.get("auto_classify") and (not item_type or item_type == "auto"):
            item_type = await _auto_classify(content, title)

        # Resolve effective variant once so the leak gate below can check it.
        eff_variant = (it.get("variant") or variant) or None

        # Leak gate: reject `window:*` summary rows when the variant is NULL
        # (i.e. would land in real core memory). The bench harness emits
        # session-window summaries with title like 'window:<sessionhash>::<i>:<j>'
        # for retrieval debugging — those are valid when stamped under a
        # bench variant, but historically leaked into core memory via
        # bulk writes that didn't pass --variant. 644 such rows had to be
        # cleaned manually on 2026-04-28 (memory 372f49b0).
        # See task #189, decision b5abb7cc.
        if (
            item_type == "summary"
            and isinstance(title, str)
            and title.startswith("window:")
            and eff_variant is None
        ):
            logger.warning(
                f"memory_write_bulk_impl: rejecting window:* summary leak "
                f"(title={title[:60]!r}) — provide an explicit variant if intentional."
            )
            continue

        prepared.append(
            {
                "id": mid,
                "type": item_type,
                "title": title,
                "content": content,
                "metadata": meta,
                "agent_id": it.get("agent_id", ""),
                "model_id": it.get("model_id", ""),
                "change_agent": agent,
                "importance": importance,
                "source": it.get("source", "agent"),
                "user_id": it.get("user_id", ""),
                "scope": scope,
                "expires_at": expires_at,
                "valid_from": it.get("valid_from") or now,
                "valid_to": it.get("valid_to") or None,
                "conversation_id": it.get("conversation_id") or None,
                "refresh_on": it.get("refresh_on") or None,
                "refresh_reason": it.get("refresh_reason") or None,
                "embed": it.get("embed", True),
                "embed_text": None,  # Will be set after enrichment
                "variant": eff_variant,
            }
        )

    # Pre-enrichment phase: auto-title, auto-entities, augment embed_text.
    # This runs before embedding so enriched text is included in the embed vector.
    for p in prepared:
        # Resolve enrich flag: None -> check env gates, True -> force on, False -> force off
        if enrich is True:
            p["title"] = await _maybe_auto_title(p["content"], p["title"], force=True)
        elif enrich is None:
            p["title"] = await _maybe_auto_title(p["content"], p["title"], force=False)
        # else: enrich is False, skip auto-title

        # Auto-entities: similar gating pattern
        if enrich is True or (enrich is None and _ingest_llm_enabled("M3_INGEST_AUTO_ENTITIES")):
            ents = await _maybe_auto_entities(p["content"], force=(enrich is True))
            if ents:
                try:
                    meta_dict = json.loads(p["metadata"]) if isinstance(p["metadata"], str) else (p["metadata"] or {})
                except json.JSONDecodeError:
                    meta_dict = {}
                if isinstance(meta_dict, dict) and "entities" not in meta_dict:
                    meta_dict["entities"] = ents
                    p["metadata"] = json.dumps(meta_dict)

        # Augment title with role (single path does this at L2056)
        p["title"] = _augment_title_with_role(p["title"], p["metadata"])

        # Set embed_text with anchors after enrichment
        p["embed_text"] = _augment_embed_text_with_anchors(
            p["content"] or p["title"], p["metadata"]
        )

    # Optional hook: rewrite embed_text via caller-supplied async enricher.
    # The enricher receives (content, metadata_dict) and returns a string
    # that REPLACES embed_text for the vector / FTS-index path. The stored
    # `content` column is not touched — this is a "keys only, values verbatim"
    # enrichment. Intended for bench / import drivers that want to prepend
    # SLM-extracted atomic facts (LoCoMo `llm_v1` / LongMemEval contextual-keys
    # pattern). Errors fall back to the un-enriched embed_text for that item.
    #
    # When enrichment fires, we also persist the enriched text to
    # `metadata_json.enriched_embed_text` so post-hoc analysis can audit
    # SLM output quality without rerunning the embedder or the enricher.
    # The raw content stays verbatim in the `content` column; only the
    # metadata grows. Callers who want to strip this for disk-space
    # reasons can filter it out in a later pass.
    if embed_key_enricher is not None and prepared:
        sem = asyncio.Semaphore(max(1, int(embed_key_enricher_concurrency)))

        async def _enrich_one(p: dict) -> None:
            if not p.get("embed_text") or not p.get("embed"):
                return
            try:
                meta = p.get("metadata") or "{}"
                meta_dict = json.loads(meta) if isinstance(meta, str) else (meta or {})
            except (json.JSONDecodeError, TypeError):
                meta_dict = {}
            raw_content = p.get("content") or ""
            async with sem:
                try:
                    enriched = await embed_key_enricher(raw_content, meta_dict)
                except Exception as e:
                    logger.debug(f"embed_key_enricher failed on item {p.get('id')}: {e}")
                    return
                # Skip the pass-through case where the enricher returned the
                # raw content unchanged (e.g. bench short-turn skip shortcut).
                # Nothing to persist if nothing changed.
                if not enriched or enriched == raw_content:
                    return
                # Keep the anchor-prefix semantics: run anchors AFTER enrichment
                # so time-aware retrieval still works.
                enriched = _augment_embed_text_with_anchors(enriched, p.get("metadata"))
                # When dual_embed=True, preserve the pre-enrichment embed_text
                # so Phase 2 can emit a SECOND vector (vector_kind='default')
                # from the raw content. embed_text itself becomes the enriched
                # string so Phase 2's existing path emits the 'enriched' vector.
                if dual_embed:
                    p["_dual_default_embed_text"] = p["embed_text"]
                p["embed_text"] = enriched
                # Persist the enriched text into metadata for post-hoc audit.
                meta_dict["enriched_embed_text"] = enriched
                p["metadata"] = json.dumps(meta_dict)

        await asyncio.gather(*(_enrich_one(p) for p in prepared))

    # Phase 1: INSERT memory_items + chroma queue + history in one transaction.
    with _db() as db:
        for p in prepared:
            db.execute(
                "INSERT INTO memory_items (id, type, title, content, metadata_json, agent_id, model_id, "
                "change_agent, importance, source, origin_device, user_id, scope, expires_at, created_at, "
                "valid_from, valid_to, conversation_id, refresh_on, refresh_reason, content_hash, variant) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    p["id"], p["type"], p["title"], p["content"], p["metadata"],
                    p["agent_id"], p["model_id"], p["change_agent"], p["importance"],
                    p["source"], ORIGIN_DEVICE, p["user_id"], p["scope"], p["expires_at"],
                    now, p["valid_from"], p["valid_to"], p["conversation_id"],
                    p["refresh_on"], p["refresh_reason"],
                    _sha256_hex((p["content"] or "").encode("utf-8")),
                    p["variant"],
                ),
            )
            # NOTE: chroma_sync_queue insert moved to Phase 2 (post-embed) so
            # we don't enqueue rows whose embedding fails (orphan accumulation).
            _record_history(
                p["id"], "create", None, p["content"], "content",
                p["agent_id"] or p["change_agent"], db=db,
            )

    # Phase 2: batched embeddings for items that requested them.
    # Dedup by content_hash(text) so variants/kinds that share identical
    # text don't trigger duplicate embedder calls. Cache hits inside
    # _embed_many already handle DB-cached vectors, but this additionally
    # deduplicates within the current batch.
    #
    # Dual-embed: when p["_dual_default_embed_text"] is present, emit TWO
    # rows — vector_kind='default' from the raw pre-enrichment text and
    # vector_kind='enriched' from p["embed_text"]. Otherwise emit a single
    # vector_kind='default' row from p["embed_text"].
    to_embed = [p for p in prepared if p["embed"] and p["embed_text"]]
    if to_embed:
        hash_to_first: dict[str, int] = {}
        unique_texts: list[str] = []
        # List of (p, kind, idx) triples — one per vector to emit.
        emit_plan: list[tuple[dict, str, int]] = []

        def _schedule(p: dict, kind: str, text: str) -> None:
            h = _content_hash(text)
            if h not in hash_to_first:
                hash_to_first[h] = len(unique_texts)
                unique_texts.append(text)
            emit_plan.append((p, kind, hash_to_first[h]))

        for p in to_embed:
            raw = p.get("_dual_default_embed_text")
            if raw:
                _schedule(p, "default", raw)
                _schedule(p, "enriched", p["embed_text"])
            else:
                _schedule(p, "default", p["embed_text"])

        # Dynamic lookup so tests that monkeypatch memory_core._embed_many
        # (pre-refactor pattern) or memory.embed._embed_many still flow
        # through. memory_core re-exports memory.embed._embed_many; we
        # check memory_core first for back-compat with patched shim tests.
        _emb = None
        try:
            import memory_core as _mc  # type: ignore
            _emb = getattr(_mc, "_embed_many", None)
        except ImportError:
            pass
        if _emb is None:
            _emb = _embed_mod._embed_many
        unique_vecs = await _emb(unique_texts)
        # Track per-item default-kind embed success so we only enqueue once.
        default_ok: set[str] = set()
        default_fail: set[str] = set()
        with _db() as db:
            for p, kind, idx in emit_plan:
                vec, m = unique_vecs[idx]
                if not vec:
                    if kind == "default":
                        default_fail.add(p["id"])
                    continue
                text_for_hash = (
                    p["_dual_default_embed_text"] if kind == "default" and p.get("_dual_default_embed_text")
                    else p["embed_text"]
                )
                db.execute(
                    "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash, vector_kind) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()), p["id"], _pack(vec), m, len(vec), now,
                        _content_hash(text_for_hash), kind,
                    ),
                )
                if kind == "default":
                    default_ok.add(p["id"])
            # Only enqueue chroma sync for items whose canonical default-kind
            # vector landed. This prevents orphan queue rows when the embed
            # server fails (e.g. context-size 400) — see chroma_sync_queue
            # orphan accumulation 2026-04-22.
            for p in to_embed:
                if p["id"] in default_ok:
                    db.execute(
                        "INSERT INTO chroma_sync_queue (memory_id, operation) VALUES (?,?)",
                        (p["id"], "upsert"),
                    )
        for mid in default_fail - default_ok:
            logger.warning(
                f"memory_write_bulk_impl: embed failed for {mid}; "
                f"skipping memory_embeddings + chroma_sync_queue insert"
            )

    # Phase 2.5: Fact enrichment (Phase 4 on-write hook).
    # Non-blocking per-row dispatch: tries semaphore, enqueues on miss.
    # Mirrors embed_key_enricher pattern at lines 1290-1327.
    if fact_enricher is not None and ENABLE_FACT_ENRICHED:
        for p in prepared:
            # Skip variant rows unless explicitly allowed
            item_variant = p.get("variant")
            if item_variant is not None and (fact_enricher_variant_allowlist is None or item_variant not in fact_enricher_variant_allowlist):
                continue

            # Get a DB connection for the non-blocking dispatch
            with _db() as db:
                try:
                    await _try_enrich_or_enqueue(
                        p["id"],
                        p.get("content") or "",
                        fact_enricher,
                        db,
                        variant=item_variant,
                        allowlist=fact_enricher_variant_allowlist
                    )
                except Exception as e:
                    logger.debug(f"fact enrichment dispatch failed for {p['id']}: {e}")

    # Phase 2.6: Entity extraction (Phase 4 on-write hook).
    # Non-blocking per-row dispatch: tries semaphore, enqueues on miss.
    # Mirrors Phase 2.5 fact enrichment pattern above.
    # fact_enriched rows are NOT extracted to prevent recursion.
    if entity_extractor is not None:
        for p in prepared:
            if p.get("type") == "fact_enriched":
                continue
            item_variant = p.get("variant")
            with _db() as db:
                try:
                    await _try_extract_or_enqueue(
                        p["id"],
                        p.get("content") or "",
                        entity_extractor,
                        db,
                        variant=item_variant,
                        allowlist=entity_extractor_variant_allowlist,
                    )
                except Exception as e:
                    logger.debug(f"entity extraction dispatch failed for {p['id']}: {e}")

    # Phase 3: Contradiction detection (if requested, with bounded concurrency).
    # Default is off in bulk (perf), must explicitly enable with check_contradictions=True.
    if check_contradictions is True:
        # Use semaphore to limit concurrency (avoid overwhelming LLM/search)
        sem = asyncio.Semaphore(8)

        async def check_one(p: dict) -> tuple[str, list[str]]:
            async with sem:
                # Only check if we have an embedding and type is not conversation/message
                vec_row = None
                with _db() as db:
                    r = db.execute(
                        "SELECT embedding FROM memory_embeddings WHERE memory_id = ? LIMIT 1",
                        (p["id"],)
                    ).fetchone()
                    if r:
                        vec_row = r

                if not vec_row or p["type"] in CONTRADICTION_TYPE_EXCLUSIONS:
                    return p["id"], []

                vec = _unpack(vec_row["embedding"])
                superseded_ids, _ = await _check_contradictions(
                    p["id"], p["content"], p["title"], vec, p["type"], p["agent_id"],
                    new_valid_from=p.get("valid_from"),
                    variant=p.get("variant"),
                )
                return p["id"], superseded_ids

        results = await asyncio.gather(*[check_one(p) for p in prepared], return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.debug(f"Contradiction check in bulk failed: {result}")

    # Phase 4: Conversation emitters (event rows, window chunks, gist rows).
    # Default behavior: emit if conversation_id is present and type==message (mirror single path).
    # Can be disabled with emit_conversation=False.
    if emit_conversation is not False:  # None or True
        # Group items by conversation_id for emitter calls
        by_conv: dict[str, list[dict]] = {}
        for p in prepared:
            cid = p.get("conversation_id")
            if cid and p["type"] == "message":
                if cid not in by_conv:
                    by_conv[cid] = []
                by_conv[cid].append(p)

        for cid, conv_items in by_conv.items():
            # Sort items by valid_from to preserve turn order (mirror single path L2119-2126)
            conv_items.sort(key=lambda x: x.get("valid_from") or now)

            # Process each message in conversation
            for p in conv_items:
                user_id = p.get("user_id", "")
                try:
                    if INGEST_EVENT_ROWS:
                        await _maybe_emit_event_rows(
                            p["content"] or "", p["metadata"], cid, user_id, p["id"]
                        )
                except Exception as e:
                    logger.debug(f"event_extraction emit failed in bulk: {e}")

            # Window and gist emitters (run once per conversation group, not per message)
            user_id = conv_items[0].get("user_id", "") if conv_items else ""
            try:
                if INGEST_WINDOW_CHUNKS:
                    await _maybe_emit_window_chunk(cid, user_id)
            except Exception as e:
                logger.debug(f"window chunk emit failed in bulk: {e}")

            try:
                if INGEST_GIST_ROWS:
                    await _maybe_emit_gist_row(cid, user_id)
            except Exception as e:
                logger.debug(f"gist row emit failed in bulk: {e}")

    return [p["id"] for p in prepared]


async def _check_contradictions(

    item_id: str,
    content: str,
    title: str,
    vec: list[float],
    type_: str,
    agent_id: str,
    new_valid_from: str | None = None,
    variant: str | None = None,
) -> tuple[list[str], list[tuple[str, float]]]:
    _resolve_mc_callbacks()
    """
    Detects contradictions with existing memories of the same type.
    Returns (superseded_ids, related_candidates) where related_candidates
    are (id, score) pairs with cosine > 0.7 that are NOT contradictions.

    When `variant` is non-None and `AUTO_RELATED_LINK_SCOPE_BY_VARIANT` is on
    (default), candidate scan is restricted to memories of the same variant.
    This prevents cross-variant contamination during obs INSERT.
    """
    superseded: list[str] = []
    related: list[tuple[str, float]] = []
    try:
        with _db() as db:
            # Find top-5 similar memories of the same type
            where = "mi.is_deleted = 0 AND mi.type = ? AND mi.id != ?"
            params = [type_, item_id]
            if agent_id:
                where += " AND mi.agent_id = ?"
                params.append(agent_id)
            # Resolve scope gate at call time so tests that monkeypatch
            # memory_core.AUTO_RELATED_LINK_SCOPE_BY_VARIANT take effect.
            _scope_on = AUTO_RELATED_LINK_SCOPE_BY_VARIANT
            try:
                import memory_core as _mc  # type: ignore
                if hasattr(_mc, "AUTO_RELATED_LINK_SCOPE_BY_VARIANT"):
                    _scope_on = _mc.AUTO_RELATED_LINK_SCOPE_BY_VARIANT
            except ImportError:
                pass
            if variant is not None and _scope_on:
                where += " AND mi.variant = ?"
                params.append(variant)
            rows = db.execute(
                f"SELECT mi.id, mi.title, mi.content, me.embedding FROM memory_items mi "
                f"JOIN memory_embeddings me ON mi.id = me.memory_id WHERE {where} LIMIT 200",
                params
            ).fetchall()

        if not rows:
            return superseded, related

        embeddings = [_unpack(r["embedding"]) for r in rows]
        scores = _batch_cosine(vec, embeddings)

        for i, row in enumerate(rows):
            score = scores[i]
            if score > CONTRADICTION_THRESHOLD:
                # High similarity — check if it's a contradiction (same topic, different content).
                # Title-match gate is configurable via CONTRADICTION_TITLE_GATE env var:
                #   'strict' = legacy substring match required
                #   'loose'  = cosine + content-differs is enough (default since 2026-04-27)
                #   'off'    = bypass content check too (research mode)
                old_title = (row["title"] or "").strip().lower()
                new_title = (title or "").strip().lower()
                titles_match = old_title == new_title or (old_title and new_title and (
                    old_title in new_title or new_title in old_title
                ))
                content_differs = (row["content"] or "").strip() != (content or "").strip()

                if CONTRADICTION_TITLE_GATE == "strict":
                    fires = titles_match and content_differs
                elif CONTRADICTION_TITLE_GATE == "loose":
                    fires = content_differs
                else:  # 'off'
                    fires = True

                if fires:
                    # Contradiction detected — supersede old memory via the
                    # shared helper. Bi-temporal validity (Zep/Graphiti pattern,
                    # 2026-04-27): close the older memory's validity interval at
                    # the new memory's valid_from, falling back to now() when the
                    # caller supplied none — so `as_of`-filtered retrieval still
                    # sees the older fact valid before the supersession point.
                    # require_active=False: the candidate query above already
                    # filtered is_deleted=0 and we hold the writer, so the close
                    # is unconditional.
                    _close_at = new_valid_from or datetime.now(timezone.utc).isoformat()
                    with _db() as db:
                        _mark_superseded(
                            row["id"], item_id, close_at=_close_at, db=db,
                            prev_value=row["content"],
                        )
                    superseded.append(row["id"])
                    logger.info(f"Memory {item_id} supersedes {row['id']} (contradiction detected, valid_to={_close_at})")
            elif score > 0.7:
                related.append((row["id"], score))
    except Exception as e:
        logger.debug(f"Contradiction check failed: {e}")
    return superseded, related


async def memory_write_from_file_impl(
    path: str,
    type: str,
    title: str = "",
    metadata: str = "{}",
    agent_id: str = "",
    model_id: str = "",
    change_agent: str = "",
    importance: float = 0.5,
    source: str = "agent",
    embed: bool = True,
    user_id: str = "",
    scope: str = "agent",
    valid_from: str = "",
    valid_to: str = "",
    auto_classify: bool = False,
    conversation_id: str = "",
    refresh_on: str = "",
    refresh_reason: str = "",
    variant: str | None = None,
    delete_after_read: bool = True,
):
    """Write a memory whose `content` is read from a file on disk.

    Bypasses the LLM-streaming bottleneck for large memory writes: when the
    LLM authors a multi-thousand-token markdown body inline in a tool_use,
    the autoregressive decode time of streaming the JSON `input` field
    dominates the wall-clock (24-90s typical). Writing to a file with the
    Write tool is off the streaming path; the resulting tool_use here only
    needs to stream a path string + a few short metadata fields.

    `path` must be an absolute path on the host where this MCP server
    runs. The file is read once, contents become the memory `content`,
    and (by default) the file is deleted on success — keeping the temp
    directory clean and signalling that the contents are now authoritative
    in m3-memory, not on disk.

    Read errors / missing files return a string "Error: ..." mirroring
    the singleton path's contract. The underlying memory_write_impl is
    called unchanged with the read content, so all existing gates
    (content-safety, leak-gate, scope, contradiction detection,
    auto-classify, etc.) apply identically.

    Reference: bench / diagnostic data in
    `.scratch/memory_latency_diagnostic.md` — Phase K rationale.
    """
    if not path:
        return "Error: path is required"
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(p):
        return f"Error: file not found: {p}"
    if not os.path.isfile(p):
        return f"Error: not a file: {p}"
    try:
        size = os.path.getsize(p)
    except OSError as e:
        return f"Error: cannot stat file: {type(e).__name__}: {e}"
    # Defense-in-depth size check — memory_write_impl will also enforce
    # 50_000-char limit on content, but we should fail fast before reading
    # a multi-megabyte file off disk.
    if size > 200_000:
        return f"Error: file too large ({size} bytes; max 200000 for memory_write_from_file)"

    try:
        with open(p, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return f"Error: cannot read file: {type(e).__name__}: {e}"

    # Delegate to the canonical singleton path. Every gate applies to the
    # disk-read content the same way it applies to inline content.
    result = await memory_write_impl(
        type=type,
        content=content,
        title=title,
        metadata=metadata,
        agent_id=agent_id,
        model_id=model_id,
        change_agent=change_agent,
        importance=importance,
        source=source,
        embed=embed,
        user_id=user_id,
        scope=scope,
        valid_from=valid_from,
        valid_to=valid_to,
        auto_classify=auto_classify,
        conversation_id=conversation_id,
        refresh_on=refresh_on,
        refresh_reason=refresh_reason,
        variant=variant,
    )

    # Only delete the source file if memory_write_impl actually wrote a
    # row (success messages start with "Created:"). On error, leave the
    # file in place so the caller can inspect it.
    if delete_after_read and isinstance(result, str) and result.startswith("Created:"):
        try:
            os.unlink(p)
        except OSError as e:
            # Non-fatal — the row landed; we just couldn't clean up the temp.
            logger.warning(f"memory_write_from_file: row written but file unlink failed: {e}")
            return result + f" (warning: could not delete source file {p}: {e})"

    return result


async def memory_write_batch_impl(items: list[dict]):
    """
    Speed Optimization: Parallelized batch memory write (Speed #1).
    Expects list of dicts with keys matching memory_write_impl args.
    """
    results = []
    # 1. First pass: Insert metadata in one transaction
    now = datetime.now(timezone.utc).isoformat()

    write_tasks = []
    for item in items:
        mid = str(uuid.uuid4())
        agent = item.get("change_agent", "").strip().lower() or _infer_change_agent_util(item.get("agent_id", ""), item.get("model_id", ""), default=DEFAULT_CHANGE_AGENT)

        with _db() as db:
            db.execute(
                "INSERT INTO memory_items (id, type, title, content, metadata_json, agent_id, model_id, change_agent, importance, source, origin_device, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (mid, item["type"], item.get("title", ""), item["content"], item.get("metadata", "{}"),
                 item.get("agent_id", ""), item.get("model_id", ""), agent, item.get("importance", 0.5),
                 item.get("source", "agent"), ORIGIN_DEVICE, now)
            )
            # NOTE: chroma_sync_queue insert moved to Phase 2 below so embed
            # failures don't leave orphan queue rows.

        if item.get("embed", True):
            # Queue for parallel embedding (gather)
            write_tasks.append((mid, item.get("content") or item.get("title")))
        results.append(mid)

    # 2. Parallelize embedding generation (Speed Optimization #1)
    # Bounded by _EMBED_SEM to prevent LM Studio overload
    async def _bounded_embed(text):
        async with _EMBED_SEM:
            return await _embed(text)

    if write_tasks:
        embed_jobs = [_bounded_embed(text) for _, text in write_tasks]
        try:
            embeddings = await asyncio.wait_for(
                asyncio.gather(*embed_jobs, return_exceptions=True),
                timeout=120.0
            )
        except asyncio.TimeoutError:
            logger.error(f"Batch embedding timed out after 120s for {len(write_tasks)} items")
            embeddings = [None] * len(write_tasks)

        with _db() as db:
            for (mid, text), result in zip(write_tasks, embeddings):
                if isinstance(result, Exception):
                    logger.warning(f"Batch embed failed for {mid}: {result}; skipping chroma_sync_queue insert")
                    continue
                if result is None:
                    logger.warning(f"Batch embed returned None for {mid}; skipping chroma_sync_queue insert")
                    continue
                vec, m = result
                if vec:
                    db.execute(
                        "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), mid, _pack(vec), m, len(vec), now, _content_hash(text))
                    )
                    db.execute(
                        "INSERT INTO chroma_sync_queue (memory_id, operation) VALUES (?,?)",
                        (mid, "upsert"),
                    )
                else:
                    logger.warning(f"Batch embed empty vec for {mid}; skipping chroma_sync_queue insert")

    return f"Batch created: {len(results)} items"



