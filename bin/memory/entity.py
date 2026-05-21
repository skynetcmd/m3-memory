"""Entity extraction and entity-CRUD — Phase 6 of the modularization.

This module hosts the entity subsystem lifted from `bin/memory_core.py`:
vocabulary loading, canonical-name resolution (sync + async + fuzzy),
entity creation, memory<->entity linking, entity<->entity relationship
linking, the queue runner + extractor wrapper, and the public read-side
impls (`entity_search_impl`, `entity_get_impl`, `extract_pending_impl`,
`entity_extractor_health`).

Graph traversal (`_graph_neighbor_ids`, `_session_neighbor_ids`,
`_entity_graph_neighbor_ids`, `_score_extra_rows`) stays in memory_core
— they're part of the graph-traversal subsystem, not entity-CRUD, and
are read by search.py through a separate shim.

## Cycle-breaking policy

Simpler than search.py: this module's only callback into memory_core
is `_track_cost`, lazy-imported inside `_run_entity_extractor` and
`extract_pending_impl` at call time. All other dependencies resolve to
sibling submodules (`memory.db`, `memory.embed`, `memory.util`,
`memory.config`) or third-party libraries. No `_resolve_mc_callbacks`
globals-binding shim needed — function-local lazy import is enough.

## Module-state ownership

`VALID_ENTITY_TYPES` and `VALID_ENTITY_PREDICATES` (frozensets returned
by `load_entity_vocab`) originate HERE and are re-exported through the
memory_core shim. Identity must be preserved across the shim — external
callers do `from memory_core import VALID_ENTITY_TYPES` and expect the
same frozenset object.

`_ENTITY_EXTRACT_SEM` (the per-process concurrency cap on
`_run_entity_extractor`) and `_PENDING_ENTITY_TASKS` (the inflight
tracking set used by `_try_extract_or_enqueue`) also live here. They
are re-exported through the shim for identity preservation.

`_TOKEN_PUNCT_RE` moved here from memory_core because `_token_jaccard`
is its only caller and that function is entity-specific.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path

import yaml

from . import config as _config
from .config import (
    _DEFAULT_VALID_ENTITY_PREDICATES,
    _DEFAULT_VALID_ENTITY_TYPES,
    _ENV_ENTITY_VOCAB_YAML,
    DEFAULT_ENTITY_VOCAB_YAML,
    ENTITY_EXTRACT_MAX_ATTEMPTS,
    ENTITY_RESOLVE_COSINE_MIN,
    ENTITY_RESOLVE_FUZZY_MIN,
)
from .db import _ENTITY_COUNT_QUERY, _db, _gate_active
from .embed import _embed_canonical_cached
from .util import sha256_hex as _sha256_hex


def _enable_entity_graph_gate() -> bool:
    """Auto-activate the entity graph when EITHER the env var is set OR
    enough entity rows exist for the feature to be useful.

    Resolution order (each consulted at call time):
      1. memory_core.ENABLE_ENTITY_GRAPH — set by test monkeypatch.
      2. M3_ENABLE_ENTITY_GRAPH env var — set by tests via setenv.
         Explicit false here vetoes the count-based gate.
      3. Count-based gate: `entities` table size >= threshold.
      4. memory.config.ENABLE_ENTITY_GRAPH — the import-time default.

    Definition lives here (post Phase 7+8 refactor) rather than in
    memory_core because entity.py is the only caller that matters; the
    function was inadvertently dropped during the extraction.
    """
    # (1) env-var explicit override — wins because tests use monkeypatch.setenv
    env = os.environ.get("M3_ENABLE_ENTITY_GRAPH")
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    # (2) monkeypatched attribute on the shim
    try:
        import memory_core as _mc  # type: ignore
        if hasattr(_mc, "ENABLE_ENTITY_GRAPH"):
            return bool(_mc.ENABLE_ENTITY_GRAPH)
    except ImportError:
        pass
    # (3) count-based gate
    if _gate_active("M3_ENABLE_ENTITY_GRAPH", _ENTITY_COUNT_QUERY, threshold=1):
        return True
    # (4) import-time default
    return bool(getattr(_config, "ENABLE_ENTITY_GRAPH", False))


def _read_gate(name: str):
    """Same lazy-lookup pattern as memory.enrich._read_gate.

    Tests patch memory_core.<NAME>; production reads memory.config.<NAME>.
    """
    try:
        import memory_core  # type: ignore
        if hasattr(memory_core, name):
            return getattr(memory_core, name)
    except ImportError:
        pass
    return getattr(_config, name, False)

logger = logging.getLogger("memory.entity")


# Token-set similarity helper. Used by `_resolve_entity` for fuzzy
# canonical-name matching when the exact-match path misses.
# Strips ASCII punctuation before tokenization so that "Alex Johnson,"
# tokenizes the same way as "Alex Johnson" — important when entity
# strings come out of an SLM extractor that occasionally emits trailing
# commas/periods.
_TOKEN_PUNCT_RE = re.compile(r"[^\w\s]")


def _token_jaccard(a: str, b: str) -> float:
    """Token-set Jaccard similarity, lowercased, punctuation-stripped, whitespace-tokenized.

    Strips ASCII punctuation before tokenization so that "Alex Johnson,"
    tokenizes the same way as "Alex Johnson" — important when entity
    strings come out of an SLM extractor that occasionally emits trailing
    commas/periods.
    """
    ta = {t for t in _TOKEN_PUNCT_RE.sub(" ", a.lower()).split() if t}
    tb = {t for t in _TOKEN_PUNCT_RE.sub(" ", b.lower()).split() if t}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# Local cosine for fuzzy-match scoring. Mirrors `memory_core._cosine`
# (Rust-then-numpy-then-pure-Python cascade). Used only inside the
# entity subsystem so duplicating it here is preferable to a
# cross-module call.
def _cosine(v1, v2):
    """Cosine similarity over two float vectors. Mirrors memory_core._cosine.

    Tries m3_core_rs.cosine first (Rust + SIMD), falls back to
    embedding_utils.cosine (numpy). Pure-Python fallback inside
    embedding_utils handles the no-numpy case.
    """
    from . import config
    if config.m3_core_rs is not None:
        return config.m3_core_rs.cosine(v1, v2)
    from embedding_utils import cosine
    return cosine(v1, v2)


# Per-process concurrency cap for `_run_entity_extractor`. Used as a
# semaphore by `_try_extract_or_enqueue` to gate inline-vs-queued
# dispatch; released in `_run_entity_extractor`'s finally block.
# Identity preserved through the shim — external callers may inspect
# `.locked()` or `._value` for queue-depth diagnostics.
from .config import ENTITY_EXTRACT_CONCURRENCY  # noqa: E402 — local to this block

_ENTITY_EXTRACT_SEM = asyncio.Semaphore(ENTITY_EXTRACT_CONCURRENCY)
_PENDING_ENTITY_TASKS: "set[asyncio.Task]" = set()


def load_entity_vocab(yaml_path: str | Path | None = None) -> tuple[frozenset[str], frozenset[str]]:
    """Load entity-graph vocabulary (types + predicates) from YAML.

    Args:
        yaml_path: Path to YAML file. If None, loads default vocabulary.
                   If file's entity_types or entity_predicates is empty list, falls back to defaults.

    Returns:
        (entity_types, entity_predicates) — both frozensets.
    """
    # Resolution order: explicit yaml_path > M3_ENTITY_VOCAB_YAML env > default.
    # Env hook lets bench harnesses override the production VALID_ENTITY_PREDICATES
    # at import-time without code changes. See decision memory (this turn).
    if yaml_path is not None:
        path = Path(yaml_path)
    elif _ENV_ENTITY_VOCAB_YAML:
        path = Path(_ENV_ENTITY_VOCAB_YAML)
    else:
        path = DEFAULT_ENTITY_VOCAB_YAML
    if not path.exists():
        # Hard fallback to in-memory defaults if the file is missing
        return _DEFAULT_VALID_ENTITY_TYPES, _DEFAULT_VALID_ENTITY_PREDICATES

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        # Hard fallback if YAML is malformed
        return _DEFAULT_VALID_ENTITY_TYPES, _DEFAULT_VALID_ENTITY_PREDICATES

    types = data.get("entity_types") or []
    preds = data.get("entity_predicates") or []

    # Empty lists fall back to defaults (e.g., placeholder LME YAML before Task A populates it)
    if not types:
        types = list(_DEFAULT_VALID_ENTITY_TYPES)
    if not preds:
        preds = list(_DEFAULT_VALID_ENTITY_PREDICATES)

    return frozenset(types), frozenset(preds)


# Module-load-time vocabulary resolution. Sets the public frozensets that
# memory_core re-exports through the shim. Externally imported, identity
# must survive the shim re-export.
VALID_ENTITY_TYPES, VALID_ENTITY_PREDICATES = load_entity_vocab(None)




def _resolve_entity(canonical_name: str, entity_type: str, db) -> str | None:
    """3-tier resolution (sync, tiers 1+2 only). Returns existing entity_id if matched, else None.

    Tier 1: exact (canonical_name, entity_type) match.
    Tier 2: fuzzy token-Jaccard >= ENTITY_RESOLVE_FUZZY_MIN within same entity_type.
    Tier 3 (embedding cosine) is handled by the async variant _resolve_entity_async.
    """
    # Tier 1: exact match
    row = db.execute(
        "SELECT id FROM entities WHERE canonical_name = ? AND entity_type = ? LIMIT 1",
        (canonical_name, entity_type),
    ).fetchone()
    if row:
        return row["id"]

    # Tier 2: fuzzy token-Jaccard within same entity_type
    candidates = db.execute(
        "SELECT id, canonical_name FROM entities WHERE entity_type = ?",
        (entity_type,),
    ).fetchall()
    best_score, best_id = 0.0, None
    for c in candidates:
        s = _token_jaccard(canonical_name, c["canonical_name"])
        if s > best_score:
            best_score, best_id = s, c["id"]
    if best_score >= ENTITY_RESOLVE_FUZZY_MIN and best_id is not None:
        return best_id

    return None  # Tiers 1+2 only in sync path


async def _resolve_entity_async(canonical_name: str, entity_type: str, db) -> str | None:
    """Full 3-tier resolution including embedding cosine. Use from async context."""
    sync_id = _resolve_entity(canonical_name, entity_type, db)
    if sync_id is not None:
        return sync_id

    # Tier 3: embedding cosine within same entity_type.
    # Cap candidates to 100 most-recently created to bound the comparison.
    # Each canonical_name is embedded at most once per process via
    # _embed_canonical_cached — successive lookups against the same
    # candidate set hit the cache after warmup, avoiding the O(N) embed
    # calls per new entity that previously dominated wall time.
    candidates = db.execute(
        "SELECT id, canonical_name FROM entities WHERE entity_type = ? ORDER BY created_at DESC LIMIT 100",
        (entity_type,),
    ).fetchall()
    if not candidates:
        return None

    qvec = await _embed_canonical_cached(canonical_name)
    if qvec is None:
        return None

    best_score, best_id = 0.0, None
    for c in candidates:
        cvec = await _embed_canonical_cached(c["canonical_name"])
        if cvec is None:
            continue
        s = _cosine(qvec, cvec)
        if s > best_score:
            best_score, best_id = s, c["id"]

    if best_score >= ENTITY_RESOLVE_COSINE_MIN and best_id is not None:
        return best_id
    return None


def _create_entity(canonical_name: str, entity_type: str, attributes: dict, db) -> str:
    """INSERT new row into entities; return new uuid id."""
    entity_id = str(uuid.uuid4())
    attrs_json = json.dumps(attributes or {})
    content_hash = _sha256_hex(
        f"{canonical_name}|{entity_type}|{attrs_json}".encode("utf-8")
    )
    db.execute(
        "INSERT INTO entities (id, canonical_name, entity_type, attributes_json, content_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        (entity_id, canonical_name, entity_type, attrs_json, content_hash),
    )
    return entity_id


def _link_memory_to_entity(
    memory_id: str,
    entity_id: str,
    mention_text: str,
    mention_offset: int,
    confidence: float,
    db,
) -> None:
    """INSERT OR IGNORE into memory_item_entities."""
    db.execute(
        "INSERT OR IGNORE INTO memory_item_entities "
        "(memory_id, entity_id, mention_text, mention_offset, confidence) "
        "VALUES (?, ?, ?, ?, ?)",
        (memory_id, entity_id, mention_text, mention_offset, confidence),
    )


def _link_entity_relationship(
    from_entity_id: str,
    to_entity_id: str,
    predicate: str,
    confidence: float,
    source_memory_id: str,
    db,
) -> None:
    """INSERT into entity_relationships. Raises ValueError for unknown predicates."""
    if predicate not in VALID_ENTITY_PREDICATES:
        raise ValueError(
            f"Invalid predicate '{predicate}'. "
            f"Valid predicates: {', '.join(sorted(VALID_ENTITY_PREDICATES))}"
        )
    db.execute(
        "INSERT INTO entity_relationships "
        "(from_entity, to_entity, predicate, confidence, source_memory_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (from_entity_id, to_entity_id, predicate, confidence, source_memory_id),
    )


def _enqueue_entity_extraction(memory_id: str, db) -> None:
    """INSERT OR IGNORE into entity_extraction_queue."""
    try:
        db.execute(
            "INSERT OR IGNORE INTO entity_extraction_queue(memory_id) VALUES (?)",
            (memory_id,),
        )
    except Exception as e:
        logger.debug(f"Failed to enqueue entity extraction for {memory_id}: {e}")


async def _run_entity_extractor(
    memory_id: str,
    content: str,
    entity_extractor,
    *,
    valid_types: frozenset | None = None,       # None = use VALID_ENTITY_TYPES
    valid_predicates: frozenset | None = None,  # None = use VALID_ENTITY_PREDICATES
) -> None:
    """Background task. Calls extractor, parses result, resolves+writes entities and
    relationships. Releases _ENTITY_EXTRACT_SEM in finally block.

    Reliability hardening (Phase E1):
    - Vocabulary validation is centralized here against active_types/active_predicates.
      Callers may pass override frozensets; None means use module-level constants.
    - Bitemporal valid_from is inherited from the source memory (not extraction-time now()).
    - entity_relationships idempotency: DELETE old rows with the same
      (from_entity, to_entity, predicate, source_memory_id) before re-inserting, so
      re-extraction of a memory doesn't create duplicate relationship rows.
      We use delete-then-insert rather than a content-hash UNIQUE index so the schema
      stays unchanged (migration 024 is already applied).
    - Failure handling: on any exception, increment attempts in the queue. Items with
      attempts >= ENTITY_EXTRACT_MAX_ATTEMPTS are excluded from the eligible set by
      _select_pending_entity_extraction (poisoned-item guard).
    """
    # Resolve active vocabularies — callers may pass custom lists for bench/experiments.
    active_types: frozenset = valid_types if valid_types is not None else VALID_ENTITY_TYPES
    active_predicates: frozenset = valid_predicates if valid_predicates is not None else VALID_ENTITY_PREDICATES

    try:
        result = await entity_extractor(content)
        entities_raw = result.get("entities", []) if isinstance(result, dict) else []
        relationships_raw = result.get("relationships", []) if isinstance(result, dict) else []

        # Inherit valid_from from the source memory so bitemporal validity is correct.
        # e.g. an entity extracted from a 2024 memory should have valid_from='2024-...',
        # not the extraction-time timestamp.
        with _db() as db:
            src_row = db.execute(
                "SELECT valid_from FROM memory_items WHERE id = ? LIMIT 1",
                (memory_id,),
            ).fetchone()
        source_valid_from: str | None = src_row["valid_from"] if src_row else None

        # Resolve/create entities and record IDs by canonical_name for relationship linking.
        canonical_to_id: dict[str, str] = {}
        with _db() as db:
            for ent in entities_raw:
                cname = (ent.get("canonical_name") or "").strip()
                etype = (ent.get("entity_type") or "").strip()
                # Centralized vocabulary validation — reject unknown entity types.
                if not cname or etype not in active_types:
                    if not cname or etype:
                        logger.debug(
                            f"Entity extractor: rejected entity_type='{etype}' "
                            f"(not in active vocabulary) for memory {memory_id}"
                        )
                    continue
                entity_id = await _resolve_entity_async(cname, etype, db)
                if entity_id is None:
                    try:
                        entity_id = _create_entity(cname, etype, {}, db)
                        # Set valid_from on the newly created entity to inherit from source.
                        if source_valid_from:
                            db.execute(
                                "UPDATE entities SET valid_from = ? WHERE id = ? AND valid_from IS NULL",
                                (source_valid_from, entity_id),
                            )
                    except Exception as e:
                        logger.debug(f"Entity create failed for '{cname}': {e}")
                        continue
                canonical_to_id[cname] = entity_id
                mention_text = ent.get("mention_text") or cname
                confidence = float(ent.get("confidence", 0.85))
                # Read mention_offset from the extractor output; default 0
                # (preserves backward compatibility with extractors that don't
                # report span positions). Coerced via int() because some JSON
                # extractors emit it as a float. GLiNER reports as `start`.
                mention_offset = int(ent.get("mention_offset") or 0)
                try:
                    # _link_memory_to_entity uses INSERT OR IGNORE — idempotent.
                    _link_memory_to_entity(memory_id, entity_id, mention_text, mention_offset, confidence, db)
                except Exception as e:
                    logger.debug(f"Entity link failed for {memory_id}->{entity_id}: {e}")

            # Write relationships — both ends must have been resolved above.
            for rel in relationships_raw:
                from_cname = (rel.get("from_entity") or "").strip()
                to_cname = (rel.get("to_entity") or "").strip()
                predicate = (rel.get("predicate") or "").strip()
                confidence = float(rel.get("confidence", 0.85))
                from_id = canonical_to_id.get(from_cname)
                to_id = canonical_to_id.get(to_cname)
                # Centralized vocabulary validation — reject unknown predicates.
                if not from_id or not to_id:
                    continue
                if predicate not in active_predicates:
                    logger.debug(
                        f"Entity extractor: rejected predicate='{predicate}' "
                        f"(not in active vocabulary) for memory {memory_id}"
                    )
                    continue
                try:
                    # Idempotency for entity_relationships: entity_relationships.id is
                    # AUTOINCREMENT so INSERT OR IGNORE would silently skip on PK conflict
                    # (there is none — autoincrement always inserts a new row). Instead we
                    # use delete-then-insert to ensure re-extraction of the same memory
                    # doesn't accumulate duplicate relationship rows.
                    db.execute(
                        "DELETE FROM entity_relationships "
                        "WHERE from_entity = ? AND to_entity = ? AND predicate = ? "
                        "AND source_memory_id = ?",
                        (from_id, to_id, predicate, memory_id),
                    )
                    rel_valid_from = rel.get("valid_from") or source_valid_from
                    db.execute(
                        "INSERT INTO entity_relationships "
                        "(from_entity, to_entity, predicate, confidence, source_memory_id, valid_from) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (from_id, to_id, predicate, confidence, memory_id, rel_valid_from),
                    )
                except Exception as e:
                    logger.debug(f"Relationship link error for {from_cname}->{to_cname} ({predicate}): {e}")

        # On success, remove any queue entry so the item isn't re-processed.
        try:
            with _db() as db:
                db.execute(
                    "DELETE FROM entity_extraction_queue WHERE memory_id = ?",
                    (memory_id,),
                )
        except Exception as db_err:
            logger.debug(f"Failed to remove queue entry for {memory_id} after success: {db_err}")

    except Exception as e:
        # Record error and bump attempts in queue so the item is retried on next pass.
        # Items with attempts >= ENTITY_EXTRACT_MAX_ATTEMPTS are excluded from the
        # eligible set by _select_pending_entity_extraction (poisoned-item guard).
        try:
            with _db() as db:
                db.execute(
                    """
                    INSERT OR REPLACE INTO entity_extraction_queue
                        (memory_id, attempts, last_error, last_attempt_at)
                    VALUES (
                        ?,
                        COALESCE((SELECT attempts FROM entity_extraction_queue WHERE memory_id=?), 0) + 1,
                        ?,
                        strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                    )
                    """,
                    (memory_id, memory_id, str(e)[:500]),
                )
        except Exception as db_err:
            logger.debug(f"Failed to record entity extraction error for {memory_id}: {db_err}")
    finally:
        _ENTITY_EXTRACT_SEM.release()


async def _try_extract_or_enqueue(
    memory_id: str,
    content: str,
    entity_extractor,
    db,
    variant: str | None = None,
    allowlist: set[str] | None = None,
    *,
    valid_types: frozenset | None = None,       # None = use VALID_ENTITY_TYPES
    valid_predicates: frozenset | None = None,  # None = use VALID_ENTITY_PREDICATES
) -> None:
    """Non-blocking: try entity extraction under semaphore; on miss, enqueue.

    Read ENABLE_ENTITY_GRAPH at call time so tests can monkeypatch.
    Variant-skip rule: if variant is not None and (allowlist is None or variant not in
    allowlist), return without doing anything — mirrors fact_enricher pattern.

    valid_types / valid_predicates are forwarded to _run_entity_extractor unchanged.
    None means use the module-level VALID_ENTITY_TYPES / VALID_ENTITY_PREDICATES constants.
    Bench harnesses and production callers may pass custom frozensets; default keeps
    existing behavior.
    """
    if not _enable_entity_graph_gate():
        return
    if entity_extractor is None:
        return

    # Skip variant rows unless explicitly allowed
    if variant is not None and (allowlist is None or variant not in allowlist):
        return

    # Try non-blocking acquire with very short timeout
    try:
        async with asyncio.timeout(0.001):
            await _ENTITY_EXTRACT_SEM.acquire()
    except (asyncio.TimeoutError, Exception):
        # Semaphore full or error — enqueue and return immediately
        _enqueue_entity_extraction(memory_id, db)
        return

    # Acquired semaphore — spawn task and track it
    task = asyncio.create_task(
        _run_entity_extractor(
            memory_id, content, entity_extractor,
            valid_types=valid_types,
            valid_predicates=valid_predicates,
        )
    )
    _PENDING_ENTITY_TASKS.add(task)
    task.add_done_callback(lambda t: _PENDING_ENTITY_TASKS.discard(t))


def _select_pending_entity_extraction(
    db,
    limit: int | None = None,
    allowed_variants: list[str] | None = None,
) -> list[tuple[str, str, str | None]]:
    """Returns [(memory_id, content, valid_from), ...] eligible for entity extraction.

    Eligibility:
      - type != 'fact_enriched'  (don't extract from derived rows)
      - COALESCE(is_deleted, 0) = 0  (NB: is_deleted, NOT deleted_at — avoids
        the deleted_at-vs-is_deleted confusion that burned us in fact_enriched)
      - variant IS NULL (or in allowed_variants when provided)
      - id NOT IN (SELECT memory_id FROM memory_item_entities)  — not already extracted
      - id NOT IN (SELECT memory_id FROM entity_extraction_queue
                   WHERE attempts >= ENTITY_EXTRACT_MAX_ATTEMPTS)  — poisoned-item guard

    valid_from is included so callers can inherit bitemporal validity from the source
    memory when creating entities and relationships during extraction.
    """
    if allowed_variants:
        variant_clause = (
            f"AND (mi.variant IS NULL OR mi.variant IN "
            f"({','.join(['?'] * len(allowed_variants))}))"
        )
        variant_params = list(allowed_variants)
    else:
        variant_clause = "AND mi.variant IS NULL"
        variant_params = []

    sql = f"""
    WITH eligible AS (
        SELECT mi.id, mi.content, mi.valid_from
        FROM memory_items mi
        WHERE mi.type != 'fact_enriched'
          AND COALESCE(mi.is_deleted, 0) = 0
          {variant_clause}
          AND mi.id NOT IN (SELECT DISTINCT memory_id FROM memory_item_entities)
    ),
    queued AS (
        SELECT mi.id, mi.content, mi.valid_from, q.attempts
        FROM entity_extraction_queue q
        JOIN memory_items mi ON mi.id = q.memory_id
        WHERE q.attempts < ?
    )
    SELECT id, content, valid_from FROM queued
    UNION
    SELECT id, content, valid_from FROM eligible
    WHERE id NOT IN (SELECT memory_id FROM entity_extraction_queue)
    ORDER BY id
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    # Resolve max-attempts via memory_core for legacy tests that
    # monkeypatch the value; production reads the import-time constant.
    _max_attempts = _read_gate("ENTITY_EXTRACT_MAX_ATTEMPTS") or ENTITY_EXTRACT_MAX_ATTEMPTS
    params = variant_params + [_max_attempts]
    return list(db.execute(sql, params).fetchall())


async def extract_pending_impl(
    dry_run: bool = True,
    limit: int = 0,
    allowed_variants: list[str] | None = None,
    *,
    valid_types: frozenset | None = None,       # None = use VALID_ENTITY_TYPES
    valid_predicates: frozenset | None = None,  # None = use VALID_ENTITY_PREDICATES
) -> dict:
    """Entity extraction queue drain. Mirrors enrich_pending_impl shape.

    Returns:
    - dry_run=True: {"count": N, "est_wall_clock_seconds": F, "sample_ids": [...]}
    - dry_run=False: {"processed": N, "succeeded": N, "failed": N,
                      "errors_summary": str, "entities_created": N,
                      "relationships_created": N}

    ETA estimate uses 3.0 sec/item (higher than fact_enriched's 2.0 because
    entity resolution adds DB lookups on top of SLM call).

    valid_types / valid_predicates are forwarded to _run_entity_extractor.
    None means use the module-level VALID_ENTITY_TYPES / VALID_ENTITY_PREDICATES constants.
    Bench harnesses can pass custom frozensets; production callers pass None (default behavior).

    Execute path: placeholder until Wave 3 injects entity_extractor via MCP tool.
    """
    with _db() as db:
        pending = _select_pending_entity_extraction(
            db,
            limit=limit if limit else None,
            allowed_variants=allowed_variants,
        )

    if not pending:
        if dry_run:
            return {"count": 0, "est_wall_clock_seconds": 0.0, "sample_ids": []}
        else:
            return {
                "processed": 0,
                "succeeded": 0,
                "failed": 0,
                "errors_summary": "No pending items",
                "entities_created": 0,
                "relationships_created": 0,
            }

    if dry_run:
        # Dry run: report count + ETA estimate (3.0 sec/item)
        est_secs = len(pending) * 3.0
        sample_ids = [row[0] for row in pending[:3]]
        return {
            "count": len(pending),
            "est_wall_clock_seconds": est_secs,
            "sample_ids": sample_ids,
        }

    # Execute: placeholder until Wave 3 wires entity_extractor injection.
    # The entity_extractor is provided at write-time through memory_write_impl;
    # a drain path that calls the extractor requires it to be injected by the
    # MCP tool layer (same pattern as enrich_pending_impl).
    # valid_types/valid_predicates are accepted here now so callers can pass them
    # when Wave 3 wires the real execution path.
    return {
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "errors_summary": "Execution requires entity_extractor (Wave 3 MCP tool)",
        "entities_created": 0,
        "relationships_created": 0,
    }


def entity_extractor_health() -> dict:
    """Read-only diagnostic for the entity extraction pipeline.

    Returns a dict with 6 keys:
      queue_depth          — COUNT(*) from entity_extraction_queue where
                             attempts < ENTITY_EXTRACT_MAX_ATTEMPTS (eligible to retry)
      poisoned             — COUNT(*) where attempts >= ENTITY_EXTRACT_MAX_ATTEMPTS
                             (excluded from eligible set; kept for diagnostic visibility)
      last_extracted_at    — ISO-8601 string of the most recent entities.created_at,
                             or None if the entities table is empty
      entities_total       — total rows in entities table
      relationships_total  — total rows in entity_relationships table
      memory_item_entities_total — total rows in memory_item_entities table
    """
    with _db() as db:
        q_depth = db.execute(
            "SELECT COUNT(*) FROM entity_extraction_queue WHERE attempts < ?",
            (ENTITY_EXTRACT_MAX_ATTEMPTS,),
        ).fetchone()[0]

        poisoned = db.execute(
            "SELECT COUNT(*) FROM entity_extraction_queue WHERE attempts >= ?",
            (ENTITY_EXTRACT_MAX_ATTEMPTS,),
        ).fetchone()[0]

        last_row = db.execute(
            "SELECT MAX(created_at) AS last_at FROM entities"
        ).fetchone()
        last_extracted_at: str | None = last_row["last_at"] if last_row else None

        entities_total = db.execute(
            "SELECT COUNT(*) FROM entities"
        ).fetchone()[0]

        relationships_total = db.execute(
            "SELECT COUNT(*) FROM entity_relationships"
        ).fetchone()[0]

        mie_total = db.execute(
            "SELECT COUNT(*) FROM memory_item_entities"
        ).fetchone()[0]

    return {
        "queue_depth": q_depth,
        "poisoned": poisoned,
        "last_extracted_at": last_extracted_at,
        "entities_total": entities_total,
        "relationships_total": relationships_total,
        "memory_item_entities_total": mie_total,
    }


def entity_search_impl(
    query: str = "",
    entity_type: str = "",
    limit: int = 10,
    with_neighbors: bool = False,
) -> list[dict]:
    """Search the entities table by canonical_name and optionally by entity_type.

    Returns:
        List of dicts: [{entity_id, canonical_name, entity_type, attributes_json, neighbor_count}]
        neighbor_count only computed if with_neighbors=True.
    """
    with _db() as db:
        # Build the WHERE clause
        where_parts = []
        params = []

        if query:
            # LIKE %query% on canonical_name (case-insensitive)
            where_parts.append("canonical_name LIKE ?")
            params.append(f"%{query}%")

        if entity_type:
            where_parts.append("entity_type = ?")
            params.append(entity_type)

        where_clause = " AND ".join(where_parts) if where_parts else "1=1"

        sql = f"""
        SELECT id, canonical_name, entity_type, attributes_json
        FROM entities
        WHERE {where_clause}
        ORDER BY canonical_name
        LIMIT ?
        """
        params.append(limit)

        rows = list(db.execute(sql, params).fetchall())

        result = []
        for entity_id, canonical_name, entity_type_val, attributes_json in rows:
            neighbor_count = 0
            if with_neighbors:
                # Count relationships where this entity is from_entity OR to_entity
                neighbor_sql = """
                SELECT COUNT(DISTINCT id)
                FROM entity_relationships
                WHERE from_entity = ? OR to_entity = ?
                """
                neighbor_count = db.execute(
                    neighbor_sql, (entity_id, entity_id)
                ).fetchone()[0]

            result.append({
                "entity_id": entity_id,
                "canonical_name": canonical_name,
                "entity_type": entity_type_val,
                "attributes_json": attributes_json or "{}",
                "neighbor_count": neighbor_count,
            })

        return result


def entity_get_impl(entity_id: str, depth: int = 1) -> dict:
    """Load single entity with neighborhood.

    Returns:
        {
            entity: {entity_id, canonical_name, entity_type, attributes_json, created_at},
            predecessors: [{from_entity_id, from_canonical_name, predicate, confidence}],
            successors: [{to_entity_id, to_canonical_name, predicate, confidence}],
            linked_memories: [{memory_id, title, type}]
        }

    Note: depth is accepted but unused beyond depth=1 (multi-hop is future work).
    """
    with _db() as db:
        # Load the entity itself
        entity_sql = """
        SELECT id, canonical_name, entity_type, attributes_json, created_at
        FROM entities
        WHERE id = ?
        """
        entity_row = db.execute(entity_sql, (entity_id,)).fetchone()

        if not entity_row:
            return {
                "entity": None,
                "predecessors": [],
                "successors": [],
                "linked_memories": [],
            }

        entity_id_val, canonical_name, entity_type, attributes_json, created_at = entity_row
        entity = {
            "entity_id": entity_id_val,
            "canonical_name": canonical_name,
            "entity_type": entity_type,
            "attributes_json": attributes_json or "{}",
            "created_at": created_at,
        }

        # Load predecessors (relationships where to_entity = this entity)
        predecessors_sql = """
        SELECT er.from_entity, e.canonical_name, er.predicate, er.confidence
        FROM entity_relationships er
        JOIN entities e ON e.id = er.from_entity
        WHERE er.to_entity = ?
        """
        predecessors = [
            {
                "from_entity_id": row[0],
                "from_canonical_name": row[1],
                "predicate": row[2],
                "confidence": row[3],
            }
            for row in db.execute(predecessors_sql, (entity_id,)).fetchall()
        ]

        # Load successors (relationships where from_entity = this entity)
        successors_sql = """
        SELECT er.to_entity, e.canonical_name, er.predicate, er.confidence
        FROM entity_relationships er
        JOIN entities e ON e.id = er.to_entity
        WHERE er.from_entity = ?
        """
        successors = [
            {
                "to_entity_id": row[0],
                "to_canonical_name": row[1],
                "predicate": row[2],
                "confidence": row[3],
            }
            for row in db.execute(successors_sql, (entity_id,)).fetchall()
        ]

        # Load linked memories
        memories_sql = """
        SELECT DISTINCT mi.id, mi.title, mi.type
        FROM memory_item_entities mie
        JOIN memory_items mi ON mi.id = mie.memory_id
        WHERE mie.entity_id = ?
        """
        linked_memories = [
            {
                "memory_id": row[0],
                "title": row[1] or "",
                "type": row[2],
            }
            for row in db.execute(memories_sql, (entity_id,)).fetchall()
        ]

        return {
            "entity": entity,
            "predecessors": predecessors,
            "successors": successors,
            "linked_memories": linked_memories,
        }
