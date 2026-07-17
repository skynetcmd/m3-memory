from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from llm_failover import get_best_llm, get_smallest_llm
from m3_sdk import M3Context, resolve_db_path

from . import config as _config
from .config import ORIGIN_DEVICE
from .db import _record_history
from .util import sha256_hex as _sha256_hex


def _read_gate(name: str):
    """Read a runtime-mutable gate flag.

    Tests monkeypatch `memory_core.<NAME>` (the public shim) — see
    test_fact_enriched, test_entity_graph. Production sets these via
    env vars at import time, which `memory.config` binds to constants.

    Resolution order at call time:
      1. memory_core.<NAME>          — what tests patch
      2. memory.config.<NAME>        — the canonical source

    Lazy import so this module loads cleanly without memory_core (e.g.
    in early-bootstrap unit tests that don't pull the shim).
    """
    try:
        import memory_core  # type: ignore
        if hasattr(memory_core, name):
            return getattr(memory_core, name)
    except ImportError:
        pass
    return getattr(_config, name)
from .db import _db
from .embed import _content_hash, _get_embed_client

logger = logging.getLogger("memory.enrich")

def _ctx() -> M3Context:
    return M3Context.for_db(resolve_db_path(None))

_CLASSIFY_CACHE: dict[str, str] = {}


_AUTO_TITLE_CACHE: dict[str, str] = {}


_AUTO_ENTITIES_CACHE: dict[str, list[str]] = {}


# Fact-enrichment dispatch primitives. Defined here (in the module that
# uses them) because the Phase 7+8 refactor moved the consumers from
# memory_core but left these module-level objects behind. memory_core
# now re-exports the names through `_resolve_mc_callbacks()` so legacy
# patching still works.
from .config import FACT_ENRICH_CONCURRENCY

_FACT_ENRICH_SEM: asyncio.Semaphore = asyncio.Semaphore(FACT_ENRICH_CONCURRENCY)
_PENDING_FACT_TASKS: set[asyncio.Task] = set()


def _ingest_llm_enabled(flag: str) -> bool:
    return os.environ.get(flag, "0").strip().lower() in ("1", "true", "yes", "on")


async def _auto_classify(content: str, title: str) -> str:
    """Uses the local LLM to classify a memory into a valid type."""
    c_hash = _content_hash(content + title)
    if c_hash in _CLASSIFY_CACHE:
        return _CLASSIFY_CACHE[c_hash]

    # Localized copy of mcp_tool_catalog.VALID_MEMORY_TYPES minus "auto"
    # (auto is the sentinel that requests classification, not a classifier output).
    # Kept local to avoid circular import: mcp_tool_catalog imports memory_core.
    # Keep this list in sync with mcp_tool_catalog.VALID_MEMORY_TYPES.
    # MUST stay in sync with mcp_tool_catalog.VALID_MEMORY_TYPES (spec.py) minus
    # "auto" (auto is the sentinel that REQUESTS classification, not an output).
    # tests/test_procedural_type.py asserts this parity so drift can't recur.
    valid_types = {
        "note", "fact", "decision", "preference", "conversation", "message",
        "task", "code", "config", "observation", "plan", "summary", "snippet",
        "reference", "log", "home", "user_fact", "scratchpad", "knowledge",
        "event_extraction", "fact_enriched", "chat_log", "belief",
        "local_device", "network_config", "infrastructure", "home_automation",
        "migration-log", "security",
        "windows_only", "macos_only", "linux_only", "to_do", "procedure",
    }

    token = _ctx().get_secret("LM_API_TOKEN") or "lm-studio"
    client = _get_embed_client()
    result = await get_best_llm(client, token)
    if not result:
        return "note"

    base_url, model = result
    prompt = (
        f"Classify this memory into exactly one type. Valid types: {', '.join(sorted(valid_types))}\n"
        f"Title: {title}\n"
        f"Content: {content[:500]}\n"
        f"Reply with ONLY the type name, nothing else."
    )

    try:
        resp = await client.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0
        )
        resp.raise_for_status()
        m_type = resp.json()["choices"][0]["message"]["content"].strip().lower()
        if m_type in valid_types:
            _CLASSIFY_CACHE[c_hash] = m_type
            return m_type
    except Exception as e:
        logger.debug(f"Auto-classification failed: {e}")
        from llm_failover import clear_failover_caches
        clear_failover_caches()

    return "note"


async def _maybe_auto_title(content: str, title: str, force: bool = False) -> str:
    """If M3_INGEST_AUTO_TITLE=1 and title is empty/trivial, ask a small LLM
    for a 4-8 word descriptive title derived from content. Returns the
    original title on any error or when the gate is off.

    A title is considered "trivial" if it is empty, a bare role prefix like
    "user:" or "assistant:", or shorter than 4 chars.

    Pass `force=True` to bypass both the env gate and the trivial-title
    check — callers that want to force LLM enrichment for a specific
    pipeline variant can opt in regardless of M3_INGEST_AUTO_TITLE.
    """
    if not force and not _ingest_llm_enabled("M3_INGEST_AUTO_TITLE"):
        return title
    if not content:
        return title
    if not force:
        t = (title or "").strip()
        trivial = (not t) or len(t) < 4 or t.rstrip(":").lower() in {
            "user", "assistant", "system", "tool", "msg", "note"
        }
        if not trivial:
            return title

    c_hash = _content_hash(content[:800])
    if c_hash in _AUTO_TITLE_CACHE:
        return _AUTO_TITLE_CACHE[c_hash]

    try:
        token = _ctx().get_secret("LM_API_TOKEN") or "lm-studio"
        client = _get_embed_client()
        result = await get_smallest_llm(client, token)
        if not result:
            return title
        base_url, model = result
        prompt = (
            "Summarize the following text as a concise title of 4 to 8 words. "
            "Do not use quotes. Do not add a trailing period. No prefix.\n\n"
            f"{content[:600]}"
        )
        resp = await client.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 32,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
        resp.raise_for_status()
        out = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        # Strip wrapping quotes and trailing punctuation
        out = out.strip("\"'").rstrip(".!?,;:").strip()
        if not out or len(out) > 120:
            return title
        _AUTO_TITLE_CACHE[c_hash] = out
        return out
    except Exception as e:
        logger.debug(f"auto-title failed: {e}")
        return title


async def _maybe_auto_entities(content: str, force: bool = False) -> list[str]:
    """If M3_INGEST_AUTO_ENTITIES=1, ask a small LLM for up to 8 salient
    entities / named concepts in `content`. Returns [] on any error or when
    the gate is off. Callers typically store the result under
    metadata["entities"] and include it in embed_text for retrieval boost.

    Pass `force=True` to bypass the env gate — callers that want per-variant
    LLM enrichment can opt in regardless of M3_INGEST_AUTO_ENTITIES.
    """
    if not force and not _ingest_llm_enabled("M3_INGEST_AUTO_ENTITIES"):
        return []
    if not content:
        return []
    c_hash = _content_hash(content[:800])
    if c_hash in _AUTO_ENTITIES_CACHE:
        return list(_AUTO_ENTITIES_CACHE[c_hash])

    try:
        token = _ctx().get_secret("LM_API_TOKEN") or "lm-studio"
        client = _get_embed_client()
        result = await get_smallest_llm(client, token)
        if not result:
            return []
        base_url, model = result
        prompt = (
            "List up to 8 salient entities or named concepts from the text. "
            "Reply with a JSON array of strings, nothing else.\n\n"
            f"{content[:600]}"
        )
        resp = await client.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 128,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
        )
        resp.raise_for_status()
        raw = (resp.json()["choices"][0]["message"]["content"] or "").strip()
        # Be lenient: strip code fences and pull the first JSON array.
        raw = raw.strip("`").strip()
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end < 0 or end <= start:
            return []
        parsed = json.loads(raw[start:end + 1])
        if not isinstance(parsed, list):
            return []
        ents = [str(x).strip() for x in parsed if isinstance(x, (str, int, float)) and str(x).strip()]
        ents = ents[:8]
        _AUTO_ENTITIES_CACHE[c_hash] = ents
        return list(ents)
    except Exception as e:
        logger.debug(f"auto-entities failed: {e}")
        return []


async def _try_enrich_or_enqueue(memory_id: str, content: str, fact_enricher, db, variant: str | None = None, allowlist: set[str] | None = None) -> None:
    """Non-blocking: try enrichment under semaphore; on miss, enqueue.

    Variant-skip rule: if variant is not None and (allowlist is None or variant not in allowlist),
    return without doing anything.
    """
    if not _read_gate("ENABLE_FACT_ENRICHED") or fact_enricher is None:
        return

    # Skip variant rows unless explicitly allowed
    if variant is not None and (allowlist is None or variant not in allowlist):
        return

    # Resolve the semaphore + pending-set at call time so tests that
    # monkeypatch memory_core._FACT_ENRICH_SEM (with a fresh-capacity
    # asyncio.Semaphore) take effect. Tests in test_fact_enriched rely
    # on this to constrain concurrency to 1.
    sem = _FACT_ENRICH_SEM
    pending = _PENDING_FACT_TASKS
    try:
        import memory_core as _mc  # type: ignore
        sem = getattr(_mc, "_FACT_ENRICH_SEM", sem)
        pending = getattr(_mc, "_PENDING_FACT_TASKS", pending)
    except ImportError:
        pass

    # Try non-blocking acquire with very short timeout
    try:
        async with asyncio.timeout(0.001):  # try-acquire only
            await sem.acquire()
    except (asyncio.TimeoutError, Exception):
        # Semaphore full or error — enqueue and return immediately
        _enqueue_fact_enrichment(memory_id, db)
        return

    # Acquired semaphore — spawn task and track it
    task = asyncio.create_task(_run_fact_enricher(memory_id, content, fact_enricher, sem=sem))
    pending.add(task)
    task.add_done_callback(lambda t: pending.discard(t))


def _enqueue_fact_enrichment(memory_id: str, db) -> None:
    """INSERT OR IGNORE into fact_enrichment_queue."""
    try:
        # Dedup on UNIQUE(memory_id) (idx_feq_memory_id). SQLite: unchanged
        # "INSERT OR IGNORE" with empty suffix. Postgres: "INSERT INTO ...
        # ON CONFLICT (memory_id) DO NOTHING".
        from memory.backends import dialect

        _d = dialect()
        _ins = _d.insert_or_ignore()
        _suffix = _d.on_conflict_ignore(conflict_target="(memory_id)")
        db.execute(
            f"{_ins} fact_enrichment_queue(memory_id) VALUES ({_d.placeholder(1)}) {_suffix}".rstrip(),
            (memory_id,)
        )
    except Exception as e:
        logger.debug(f"Failed to enqueue fact enrichment for {memory_id}: {e}")


async def _run_fact_enricher(memory_id: str, content: str, fact_enricher,
                              sem: "asyncio.Semaphore | None" = None) -> None:
    """Run the actual fact extractor with error handling and retries.

    `sem` is the semaphore that was acquired in the caller; we release
    the same instance here. None falls back to the module-level
    `_FACT_ENRICH_SEM` (legacy behavior).
    """
    if sem is None:
        sem = _FACT_ENRICH_SEM
    try:
        facts = await fact_enricher(content)
        if facts:
            await _write_fact_rows(memory_id, facts)
    except Exception as e:
        # Record error and bump attempts in queue
        try:
            with _db() as db:
                # Dedup target is UNIQUE(memory_id) (idx_feq_memory_id). Verb is
                # plain "INSERT INTO" on both backends (SQLite accepts ON
                # CONFLICT DO UPDATE from a bare INSERT). attempts is a
                # correlated-subquery increment computed pre-insert.
                from memory.backends import dialect

                _d = dialect()
                _p = _d.param()
                _suffix = _d.on_conflict_update(
                    conflict_target="(memory_id)",
                    set_columns=["attempts", "last_error", "last_attempt_at"],
                )
                # last_attempt_at via _d.now(): the SQLite strftime literal both
                # is wrong on PG *and* its %Y/%m tokens collide with psycopg's %s
                # parameter parsing (raises before the SQL reaches the server).
                db.execute(f"""
                    INSERT INTO fact_enrichment_queue(memory_id, attempts, last_error, last_attempt_at)
                    VALUES ({_p}, COALESCE((SELECT attempts FROM fact_enrichment_queue WHERE memory_id={_p}),0)+1, {_p}, {_d.now()})
                    {_suffix}
                """.rstrip(), (memory_id, memory_id, str(e)[:500]))
        except Exception as db_err:
            logger.debug(f"Failed to record enrichment error for {memory_id}: {db_err}")
    finally:
        sem.release()


async def _write_fact_rows(memory_id: str, facts: list[dict]) -> None:
    """Write one fact_enriched row per fact, with references edge and metadata."""
    for fact_dict in facts:
        fact_text = fact_dict.get("text", "").strip()
        if not fact_text:
            continue

        confidence = float(fact_dict.get("confidence", 0.5))
        fact_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # Build metadata with source and confidence
        metadata = {
            "source_turn_id": memory_id,
            "confidence": confidence,
        }

        try:
            from memory.backends import dialect
            _d = dialect()
            with _db() as db:
                # Insert the fact row
                db.execute(
                    "INSERT INTO memory_items (id, type, title, content, metadata_json, change_agent, source, origin_device, scope, created_at, content_hash) "
                    f"VALUES ({_d.placeholder(11)})",
                    (
                        fact_id,
                        "fact_enriched",
                        fact_text[:100],  # Use fact text as title (truncated)
                        fact_text,
                        json.dumps(metadata),
                        "fact_enricher",
                        "fact_enricher",
                        ORIGIN_DEVICE,
                        "agent",
                        now,
                        _sha256_hex(fact_text.encode("utf-8")),
                    )
                )
                # Link via references edge: fact_id -> memory_id (from fact to source)
                db.execute(
                    f"INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES ({_d.placeholder(5)})",
                    (
                        str(uuid.uuid4()),
                        fact_id,
                        memory_id,
                        "references",
                        now,
                    )
                )
                _record_history(fact_id, "create", None, fact_text, "content", "fact_enricher", db=db)
        except Exception as e:
            logger.debug(f"Failed to write fact row for {memory_id}: {e}")


def _select_pending_fact_enrichment(db, limit: int | None = None, allowed_variants: list[str] | None = None) -> list[tuple[str, str]]:
    """Returns [(memory_id, content), ...] eligible for enrichment.

    Eligibility: type != fact_enriched, variant IS NULL (or in allowed_variants),
    no existing fact_enriched child via references edge, attempts < max_attempts.

    When allowed_variants is provided, loosen the variant filter from strict NULL
    to (variant IS NULL OR variant IN (...)).
    """
    from memory.backends import dialect
    _d = dialect()
    p = _d.param()
    # Build the variant clause
    if allowed_variants:
        variant_clause = f"AND (mi.variant IS NULL OR mi.variant IN ({_d.placeholder(len(allowed_variants))}))"
        variant_params = list(allowed_variants)
    else:
        variant_clause = "AND mi.variant IS NULL"
        variant_params = []

    sql = f"""
    WITH eligible AS (
        SELECT mi.id, mi.content
        FROM memory_items mi
        WHERE mi.type != 'fact_enriched'
          AND COALESCE(mi.is_deleted, 0) = 0
          {variant_clause}
          AND NOT EXISTS (
              SELECT 1 FROM memory_relationships mr
              JOIN memory_items child ON child.id = mr.from_id
              WHERE mr.to_id = mi.id
                AND mr.relationship_type = 'references'
                AND child.type = 'fact_enriched'
          )
    ),
    queued AS (
        SELECT mi.id, mi.content, q.attempts
        FROM fact_enrichment_queue q
        JOIN memory_items mi ON mi.id = q.memory_id
        WHERE q.attempts < {p}
    )
    SELECT id, content FROM queued
    UNION
    SELECT id, content FROM eligible
    WHERE id NOT IN (SELECT memory_id FROM fact_enrichment_queue)
    ORDER BY id
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    params = variant_params + [_read_gate("FACT_ENRICH_MAX_ATTEMPTS")]
    return list(db.execute(sql, params).fetchall())


async def enrich_pending_impl(dry_run: bool = True, limit: int = 0, allowed_variants: list[str] | None = None) -> dict:
    """Enrich pending memory items. Dry-run reports count + ETA; execute drains queue.

    Returns:
    - dry_run=True: {"count": N, "est_wall_clock_seconds": F, "sample_ids": [...]}
    - dry_run=False: {"processed": N, "succeeded": N, "failed": N, "errors_summary": str}
    """
    with _db() as db:
        pending = _select_pending_fact_enrichment(db, limit=limit, allowed_variants=allowed_variants)

    if not pending:
        if dry_run:
            return {"count": 0, "est_wall_clock_seconds": 0.0, "sample_ids": []}
        else:
            return {"processed": 0, "succeeded": 0, "failed": 0, "errors_summary": "No pending items"}

    if dry_run:
        # Dry run: report count + ETA estimate (2.0 sec/item conservative default)
        est_secs = len(pending) * 2.0
        sample_ids = [mid for mid, _ in pending[:3]]
        return {
            "count": len(pending),
            "est_wall_clock_seconds": est_secs,
            "sample_ids": sample_ids,
        }

    # Execute: drain the queue using the semaphore
    # For execution, we'd need to have a fact_enricher available. Since this is the
    # core implementation and the enricher is passed at write time, we can't execute
    # here without the enricher. This function is typically called as an MCP tool
    # with an enricher injected. For now, return a placeholder indicating execution mode.
    # In Wave 3 (MCP tool), the caller will provide the enricher.
    return {
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "errors_summary": "Execution requires enricher (Wave 3 MCP tool)",
    }


