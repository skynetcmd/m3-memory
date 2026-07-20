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
import contextlib
import contextvars
import json
import logging
import os
import re
import uuid
from pathlib import Path

import yaml  # type: ignore[import-untyped]

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

# Store-once entity-name vectors (migration 032). Tier-3 cosine resolution loads
# candidate vectors from the entity_embeddings table instead of re-embedding the
# candidate canonical_names on every cold resolve. A candidate that has no stored
# vector yet is embedded once and persisted (lazy backfill); subsequent resolves
# read the blob. This keeps tiers 1-2 (exact, fuzzy) entirely embed-free — only
# the cosine tier touches the embedder, and only for the query name + any
# not-yet-stored candidates. (Storing eagerly on entity creation, by contrast,
# would inject an embed call into the previously embed-free create path.)


def _entity_embeddings_available(db) -> bool:
    """True if the entity_embeddings table exists in this DB. Older DBs that
    predate migration 032 won't have it; callers fall back to the
    embed-each-candidate path so behavior is preserved everywhere."""
    try:
        from memory.backends import dialect

        _d = dialect()
        _sql, _params = _d.table_exists("entity_embeddings")
        row = db.execute(_sql, _params).fetchone()
        return row is not None
    except Exception:
        return False


def _load_entity_vectors(db, entity_ids: list[str]) -> dict[str, list[float]]:
    """Bulk-load stored name vectors for the given entity ids. Returns
    {entity_id: vector}; ids with no stored vector are simply absent."""
    if not entity_ids:
        return {}
    from embedding_utils import unpack as _unpack

    from memory.backends import dialect

    _d = dialect()
    out: dict[str, list[float]] = {}
    # Chunk the IN-list to stay under SQLite's variable limit.
    for i in range(0, len(entity_ids), 500):
        chunk = entity_ids[i : i + 500]
        ph = _d.placeholder(len(chunk))
        try:
            rows = db.execute(
                f"SELECT entity_id, embedding FROM entity_embeddings WHERE entity_id IN ({ph})",
                chunk,
            ).fetchall()
        except Exception:
            return out
        for r in rows:
            eid = r["entity_id"] if hasattr(r, "keys") else r[0]
            blob = r["embedding"] if hasattr(r, "keys") else r[1]
            try:
                out[eid] = _unpack(blob)
            except Exception:
                continue
    return out


# Deferred-write buffer for store-once vectors. SQLite is single-writer: when many
# resolve loops run concurrently (e.g. a batched extractor), having each one write
# its newly-embedded candidate vectors mid-resolve makes them all contend for the
# write lock and livelock under busy_timeout. The buffer lets a concurrent caller
# collect new vectors during the parallel resolve phase, then flush them ONCE,
# serially, outside that phase. Default is write-through (buffer inactive) so
# single-threaded callers and the existing tests are unaffected.
_DEFERRED_ENTITY_VECTORS: "contextvars.ContextVar[list[tuple[str, list[float], str | None]] | None]" = (
    contextvars.ContextVar("_DEFERRED_ENTITY_VECTORS", default=None)
)


@contextlib.contextmanager
def deferred_entity_vector_writes():
    """Within this context, _store_entity_vector buffers vectors instead of
    writing them; the buffered rows are returned to the caller to flush serially
    via flush_entity_vectors(). Use around a concurrent batch of resolves so the
    vector writes don't fight SQLite's single writer mid-resolve."""
    buf: list[tuple[str, list[float], str | None]] = []
    token = _DEFERRED_ENTITY_VECTORS.set(buf)
    try:
        yield buf
    finally:
        _DEFERRED_ENTITY_VECTORS.reset(token)


def flush_entity_vectors(db, buffered: list[tuple[str, list[float], str | None]]) -> int:
    """Write buffered (entity_id, vec, model) rows in one transaction. Serial by
    construction — call from a single task after a concurrent resolve phase.
    Returns the number of rows written. Best-effort: never raises."""
    if not buffered:
        return 0
    from embedding_utils import pack as _pack

    written = 0
    try:
        # entity_id is the PK; OR REPLACE semantics = upsert overwriting the
        # other inserted columns. Verb is plain "INSERT INTO" on BOTH backends
        # (SQLite accepts ON CONFLICT DO UPDATE from a bare INSERT — proven
        # against a scratch sqlite table before wiring this in).
        from memory.backends import dialect

        _d = dialect()
        _suffix = _d.on_conflict_update(
            conflict_target="(entity_id)",
            set_columns=["embedding", "embed_model", "dim"],
        )
        db.executemany(
            "INSERT INTO entity_embeddings (entity_id, embedding, embed_model, dim) "
            f"VALUES ({_d.placeholder(4)}) {_suffix}".rstrip(),
            [(eid, _pack(vec), model, len(vec)) for eid, vec, model in buffered if vec],
        )
        written = len(buffered)
        try:
            db.commit()
        except Exception:
            pass
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"entity vector flush failed ({len(buffered)} rows): {e}")
    return written


def _store_entity_vector(db, entity_id: str, vec: list[float], model: str | None) -> None:
    """Persist one entity's name vector (idempotent). Best-effort: never raises
    into the resolution path — a storage failure just means a future resolve
    re-embeds this candidate, same as before store-once.

    If a deferred-write buffer is active (see deferred_entity_vector_writes), the
    vector is appended to it instead of written now, so concurrent resolves don't
    contend for SQLite's write lock; the caller flushes serially afterward."""
    if not vec:
        return
    buf = _DEFERRED_ENTITY_VECTORS.get()
    if buf is not None:
        buf.append((entity_id, vec, model))
        return
    from embedding_utils import pack as _pack

    try:
        # entity_id is the PK; OR REPLACE semantics = upsert overwriting the
        # other inserted columns. Verb is plain "INSERT INTO" on BOTH backends
        # (SQLite accepts ON CONFLICT DO UPDATE from a bare INSERT — proven
        # against a scratch sqlite table before wiring this in).
        from memory.backends import dialect

        _d = dialect()
        _suffix = _d.on_conflict_update(
            conflict_target="(entity_id)",
            set_columns=["embedding", "embed_model", "dim"],
        )
        db.execute(
            "INSERT INTO entity_embeddings (entity_id, embedding, embed_model, dim) "
            f"VALUES ({_d.placeholder(4)}) {_suffix}".rstrip(),
            (entity_id, _pack(vec), model, len(vec)),
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(f"entity vector store failed for {entity_id}: {e}")


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

    Tries m3_core_rs.token_jaccard first (Rust), falls back to pure-Python.
    """
    if _config.m3_core_rs is not None and hasattr(_config.m3_core_rs, "token_jaccard"):
        return _config.m3_core_rs.token_jaccard(a, b)

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

_ENTITY_EXTRACT_SEMS: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}
_ENTITY_EXTRACT_SEM_FALLBACK = asyncio.Semaphore(ENTITY_EXTRACT_CONCURRENCY)

def get_entity_extract_sem() -> asyncio.Semaphore:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _ENTITY_EXTRACT_SEM_FALLBACK

    if loop not in _ENTITY_EXTRACT_SEMS:
        # Cleanup closed loops to prevent memory leak
        for k in list(_ENTITY_EXTRACT_SEMS.keys()):
            try:
                if k.is_closed():
                    del _ENTITY_EXTRACT_SEMS[k]
            except Exception:
                pass
        _ENTITY_EXTRACT_SEMS[loop] = asyncio.Semaphore(ENTITY_EXTRACT_CONCURRENCY)
    return _ENTITY_EXTRACT_SEMS[loop]

class LoopScopedSemaphoreProxy:
    def __getattr__(self, name):
        return getattr(get_entity_extract_sem(), name)

    def __await__(self):
        return get_entity_extract_sem().acquire().__await__()

_ENTITY_EXTRACT_SEM = LoopScopedSemaphoreProxy()
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
    from memory.backends import dialect

    _d = dialect()
    p = _d.param()
    # Tier 1: exact match
    row = db.execute(
        f"SELECT id FROM entities WHERE canonical_name = {p} AND entity_type = {p} LIMIT 1",
        (canonical_name, entity_type),
    ).fetchone()
    if row:
        return row["id"]

    # Tier 2: fuzzy token-Jaccard within same entity_type
    candidates = db.execute(
        f"SELECT id, canonical_name FROM entities WHERE entity_type = {p}",
        (entity_type,),
    ).fetchall()

    if not candidates:
        return None

    # Tier-A perf: try the Rust batch path first (rayon parallel).
    if _config.m3_core_rs is not None and hasattr(_config.m3_core_rs, "token_jaccard_batch"):
        names = [c["canonical_name"] for c in candidates]
        scores = _config.m3_core_rs.token_jaccard_batch(canonical_name, names)
        best_score, best_id = 0.0, None
        for i, s in enumerate(scores):
            if s > best_score:
                best_score, best_id = s, candidates[i]["id"]
        if best_score >= ENTITY_RESOLVE_FUZZY_MIN and best_id is not None:
            return best_id
    else:
        # Python fallback: sequential loop.
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
    from memory.backends import dialect

    _d = dialect()
    p = _d.param()
    sync_id = _resolve_entity(canonical_name, entity_type, db)
    if sync_id is not None:
        return sync_id

    # Tier 3: embedding cosine within same entity_type.
    # Cap candidates to 100 most-recently created to bound the comparison.
    #
    # Store-once (migration 032): candidate name vectors are loaded from the
    # entity_embeddings table rather than re-embedded each cold resolve. A
    # candidate with no stored vector is embedded once and persisted, so the
    # next resolve reads the blob. At scale this turns an ~101-embed cold
    # resolve into a 1-embed one (only the new query name). When the table is
    # absent (DBs predating migration 032) we fall back to embedding each
    # candidate via the in-process cache — identical behavior to before
    # store-once.
    candidates = db.execute(
        f"SELECT id, canonical_name FROM entities WHERE entity_type = {p} ORDER BY created_at DESC LIMIT 100",
        (entity_type,),
    ).fetchall()
    if not candidates:
        return None

    qvec = await _embed_canonical_cached(canonical_name)
    if qvec is None:
        return None

    use_store = _entity_embeddings_available(db)
    stored = _load_entity_vectors(db, [c["id"] for c in candidates]) if use_store else {}

    async def _candidate_vec(c) -> list[float] | None:
        """Vector for a candidate: stored blob if present, else embed once and
        (when the store exists) persist for next time."""
        v = stored.get(c["id"])
        if v is not None:
            return v
        v = await _embed_canonical_cached(c["canonical_name"])
        if v is not None and use_store:
            _store_entity_vector(db, c["id"], v, _config.EMBED_MODEL if hasattr(_config, "EMBED_MODEL") else None)
        return v

    valid_candidates = []
    cvecs = []
    for c in candidates:
        cvec = await _candidate_vec(c)
        if cvec is not None:
            cvecs.append(cvec)
            valid_candidates.append(c)
    if not cvecs:
        return None

    if use_store and _DEFERRED_ENTITY_VECTORS.get() is None:
        # Write-through path: persist newly-embedded candidate vectors in one
        # commit so the work isn't lost if the surrounding transaction rolls back.
        # When deferral is active the vectors are in the caller's buffer instead —
        # skip the commit so concurrent resolves don't contend for the write lock.
        try:
            db.commit()
        except Exception:
            pass

    best_score, best_id = 0.0, None
    if _config.m3_core_rs is not None:
        # Single FFI call to compute all cosine similarities in parallel.
        scores = _config.m3_core_rs.cosine_batch(qvec, cvecs)
        for i, s in enumerate(scores):
            if s > best_score:
                best_score, best_id = s, valid_candidates[i]["id"]
    else:
        from .util import _cosine
        for i, cvec in enumerate(cvecs):
            s = _cosine(qvec, cvec)
            if s > best_score:
                best_score, best_id = s, valid_candidates[i]["id"]

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
    from memory.backends import dialect

    _d = dialect()
    db.execute(
        "INSERT INTO entities (id, canonical_name, entity_type, attributes_json, content_hash) "
        f"VALUES ({_d.placeholder(5)})",
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
    # Dedup on the composite PK (memory_id, entity_id, mention_offset). On
    # SQLite the dialect emits "INSERT OR IGNORE" with an empty suffix
    # (unchanged); on Postgres it emits "INSERT INTO ... ON CONFLICT
    # (memory_id, entity_id, mention_offset) DO NOTHING".
    from memory.backends import dialect

    _d = dialect()
    _ins = _d.insert_or_ignore()
    _suffix = _d.on_conflict_ignore(
        conflict_target="(memory_id, entity_id, mention_offset)"
    )
    db.execute(
        f"{_ins} memory_item_entities "
        "(memory_id, entity_id, mention_text, mention_offset, confidence) "
        f"VALUES ({_d.placeholder(5)}) {_suffix}".rstrip(),
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
    from memory.backends import dialect

    _d = dialect()
    db.execute(
        "INSERT INTO entity_relationships "
        "(from_entity, to_entity, predicate, confidence, source_memory_id) "
        f"VALUES ({_d.placeholder(5)})",
        (from_entity_id, to_entity_id, predicate, confidence, source_memory_id),
    )


def _enqueue_entity_extraction(memory_id: str, db) -> None:
    """INSERT OR IGNORE into entity_extraction_queue."""
    try:
        # Dedup on UNIQUE(memory_id) (idx_eeq_memory_id). SQLite: unchanged
        # "INSERT OR IGNORE" with empty suffix. Postgres: "INSERT INTO ...
        # ON CONFLICT (memory_id) DO NOTHING".
        from memory.backends import dialect

        _d = dialect()
        _ins = _d.insert_or_ignore()
        _suffix = _d.on_conflict_ignore(conflict_target="(memory_id)")
        db.execute(
            f"{_ins} entity_extraction_queue(memory_id) VALUES ({_d.placeholder(1)}) {_suffix}".rstrip(),
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

    from memory.backends import dialect

    _d = dialect()
    p = _d.param()

    try:
        result = await entity_extractor(content)
        entities_raw = result.get("entities", []) if isinstance(result, dict) else []
        relationships_raw = result.get("relationships", []) if isinstance(result, dict) else []

        # Inherit valid_from from the source memory so bitemporal validity is correct.
        # e.g. an entity extracted from a 2024 memory should have valid_from='2024-...',
        # not the extraction-time timestamp.
        with _db() as db:
            src_row = db.execute(
                f"SELECT valid_from FROM memory_items WHERE id = {p} LIMIT 1",
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
                                f"UPDATE entities SET valid_from = {p} WHERE id = {p} AND valid_from IS NULL",
                                (source_valid_from, entity_id),
                            )
                    except Exception as e:
                        logger.debug(f"Entity create failed for '{cname}': {e}")
                        continue
                canonical_to_id[cname] = entity_id
                mention_text = ent.get("mention_text") or cname
                # Coerce raw extractor scalars defensively: a pluggable extractor
                # may emit confidence as null/'high' or a non-numeric offset. An
                # unguarded float()/int() here would abort the whole memory's
                # entity+relationship write and eventually poison the row after
                # retries. Default rather than drop — the entity is already
                # validated/resolved above (§3 fail-safe; mirrors m3_entities.py).
                try:
                    confidence = float(ent.get("confidence", 0.85))
                except (TypeError, ValueError):
                    confidence = 0.85
                # Read mention_offset from the extractor output; default 0
                # (preserves backward compatibility with extractors that don't
                # report span positions). Coerced via int() because some JSON
                # extractors emit it as a float. GLiNER reports as `start`.
                try:
                    mention_offset = int(ent.get("mention_offset") or 0)
                except (TypeError, ValueError):
                    mention_offset = 0
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
                try:
                    confidence = float(rel.get("confidence", 0.85))
                except (TypeError, ValueError):
                    confidence = 0.85
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
                        f"WHERE from_entity = {p} AND to_entity = {p} AND predicate = {p} "
                        f"AND source_memory_id = {p}",
                        (from_id, to_id, predicate, memory_id),
                    )
                    rel_valid_from = rel.get("valid_from") or source_valid_from
                    db.execute(
                        "INSERT INTO entity_relationships "
                        "(from_entity, to_entity, predicate, confidence, source_memory_id, valid_from) "
                        f"VALUES ({_d.placeholder(6)})",
                        (from_id, to_id, predicate, confidence, memory_id, rel_valid_from),
                    )
                except Exception as e:
                    logger.debug(f"Relationship link error for {from_cname}->{to_cname} ({predicate}): {e}")

        # On success, remove any queue entry so the item isn't re-processed.
        try:
            with _db() as db:
                db.execute(
                    f"DELETE FROM entity_extraction_queue WHERE memory_id = {p}",
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
                # Dedup target is UNIQUE(memory_id) (idx_eeq_memory_id). Verb is
                # plain "INSERT INTO" on both backends (SQLite accepts ON
                # CONFLICT DO UPDATE from a bare INSERT). attempts is a
                # correlated-subquery increment computed pre-insert so it works
                # identically whether or not the row already exists; last_error
                # and last_attempt_at are overwritten from the new row on conflict.
                from memory.backends import dialect

                _d = dialect()
                _p = _d.param()
                _suffix = _d.on_conflict_update(
                    conflict_target="(memory_id)",
                    set_columns=["attempts", "last_error", "last_attempt_at"],
                )
                db.execute(
                    f"""
                    INSERT INTO entity_extraction_queue
                        (memory_id, attempts, last_error, last_attempt_at)
                    VALUES (
                        {_p},
                        COALESCE((SELECT attempts FROM entity_extraction_queue WHERE memory_id={_p}), 0) + 1,
                        {_p},
                        {_d.now()}
                    )
                    {_suffix}
                    """.rstrip(),
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
    from memory.backends import dialect

    _d = dialect()
    p = _d.param()
    if allowed_variants:
        variant_clause = (
            f"AND (mi.variant IS NULL OR mi.variant IN "
            f"({_d.placeholder(len(allowed_variants))}))"
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
        WHERE q.attempts < {p}
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

    # Execute: Drain the queue using the pluggable entity extractor
    processed = 0
    succeeded = 0
    failed = 0
    errors = []

    from .extraction import get_configured_extractor
    ext = get_configured_extractor()
    extractor_func = ext.extract

    # We iterate and process each pending item under the semaphore to manage concurrency
    for memory_id, content, valid_from in pending:
        await _ENTITY_EXTRACT_SEM.acquire()
        try:
            # We run the extractor and write the entities/relationships directly
            await _run_entity_extractor(
                memory_id,
                content,
                extractor_func,
                valid_types=valid_types,
                valid_predicates=valid_predicates,
            )
            succeeded += 1
        except Exception as e:
            failed += 1
            errors.append(f"{memory_id}: {str(e)}")
            logger.warning(f"extract_pending_impl: failed to process {memory_id}: {e}")
        finally:
            processed += 1

    return {
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "errors_summary": "; ".join(errors) if errors else "None",
        "entities_created": succeeded,  # simple proxy for success count
        "relationships_created": succeeded,
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
    from memory.backends import dialect

    _d = dialect()
    p = _d.param()
    with _db() as db:
        q_depth = db.execute(
            f"SELECT COUNT(*) FROM entity_extraction_queue WHERE attempts < {p}",
            (ENTITY_EXTRACT_MAX_ATTEMPTS,),
        ).fetchone()[0]

        poisoned = db.execute(
            f"SELECT COUNT(*) FROM entity_extraction_queue WHERE attempts >= {p}",
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
    from memory.backends import dialect

    _d = dialect()
    p = _d.param()
    with _db() as db:
        # Build the WHERE clause
        where_parts: list[str] = []
        params: list[object] = []

        if query:
            # LIKE %query% on canonical_name (case-insensitive)
            where_parts.append(f"canonical_name LIKE {p}")
            params.append(f"%{query}%")

        if entity_type:
            where_parts.append(f"entity_type = {p}")
            params.append(entity_type)

        where_clause = " AND ".join(where_parts) if where_parts else "1=1"

        sql = f"""
        SELECT id, canonical_name, entity_type, attributes_json
        FROM entities
        WHERE {where_clause}
        ORDER BY canonical_name
        LIMIT {p}
        """
        params.append(limit)

        rows = list(db.execute(sql, params).fetchall())

        result = []
        for entity_id, canonical_name, entity_type_val, attributes_json in rows:
            neighbor_count = 0
            if with_neighbors:
                # Count relationships where this entity is from_entity OR to_entity
                neighbor_sql = f"""
                SELECT COUNT(DISTINCT id)
                FROM entity_relationships
                WHERE from_entity = {p} OR to_entity = {p}
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
    from memory.backends import dialect

    _d = dialect()
    p = _d.param()
    with _db() as db:
        # Load the entity itself
        entity_sql = f"""
        SELECT id, canonical_name, entity_type, attributes_json, created_at
        FROM entities
        WHERE id = {p}
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
        predecessors_sql = f"""
        SELECT er.from_entity, e.canonical_name, er.predicate, er.confidence
        FROM entity_relationships er
        JOIN entities e ON e.id = er.from_entity
        WHERE er.to_entity = {p}
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
        successors_sql = f"""
        SELECT er.to_entity, e.canonical_name, er.predicate, er.confidence
        FROM entity_relationships er
        JOIN entities e ON e.id = er.to_entity
        WHERE er.from_entity = {p}
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
        memories_sql = f"""
        SELECT DISTINCT mi.id, mi.title, mi.type
        FROM memory_item_entities mie
        JOIN memory_items mi ON mi.id = mie.memory_id
        WHERE mie.entity_id = {p}
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


# ── Bypass-surface builder (ADR-0001) ────────────────────────────────────────
# Materialize the rank-independent recall surface into the bypass_surface table
# (migration 033). The retrieval path then reads it with one scope-isolated indexed
# seek instead of recomputing surfacing per query (ADR-0001 §0, §8).

# Default per-question cap, overridable via env (ADR-0001 §10 Q4). 300 = the
# validated operating point. A hard MAX guards pathological env input (§4).
_BYPASS_SURFACE_CAP_DEFAULT = 300
_BYPASS_SURFACE_CAP_MAX = 5000

# Coarse question/strategy -> entity-type surfacing rules. Mirrors the validated
# prototype: recall-biased, over-match rather than miss. Strategy gating is applied
# by the CALLER passing `strategy` (ADR-0001 §10 Q1/Q2: core has no router yet, so
# strategy is a passed-in param; a per-strategy on/off + cap multiplier is applied
# here from the policy below). When the router lands, it feeds this same param.
_BYPASS_STRATEGY_POLICY: dict[str, dict] = {
    "COMPUTE":   {"enabled": True,  "cap_frac": 1.0},   # aggressive: every instance
    "FACT":      {"enabled": True,  "cap_frac": 0.25},  # conservative: avoid flooding
    "ASSISTANT": {"enabled": False, "cap_frac": 0.0},   # ranking problem, not surfacing
    "PROSE":     {"enabled": False, "cap_frac": 0.0},   # answer-style, not surfacing
    "VERIFY":    {"enabled": True,  "cap_frac": 0.25},  # production; reserved
}
_BYPASS_DEFAULT_POLICY = {"enabled": True, "cap_frac": 1.0}

# The cap is spent on type-RELEVANT mentions, not arbitrary entity-bearing items (the
# fidelity bug the reproduction bench caught — untyped surfacing crowds out gold turns
# under the cap). HOW types are chosen is the CALLER's policy, supplied as:
#   category_for : {conversation_id: category}   — each scope's category
#   type_rules   : {category: (entity_type, ...)} — category -> entity types to surface
# Core stays generic: it does NOT embed question-text regexes or domain categories (those
# would be deployment/benchmark-specific). A scope whose category resolves to no types
# surfaces NOTHING (conservative — avoids flooding). The wildcard category "*" in
# type_rules, if provided, is the fallback for scopes with no/unknown category.
_BYPASS_WILDCARD_CATEGORY = "*"

# Cap-priority ordering (ADR-0001). When the cap binds, WHICH typed mentions survive
# must be DETERMINISTIC and meaningful — a bare LIMIT is planner-dependent (a silent
# correctness bug: adding an index shifted results 85->81% on the bench). Whitelist
# (never raw SQL — §6); each maps to an ORDER BY over the GROUP BY mi.id aggregate.
# 'confidence' is the real-world-sensible DEFAULT: keep the highest-signal typed
# mentions (GLiNER span score), measured best on the bench (+5pp over arbitrary).
# memory_id tie-break makes every ordering fully deterministic. Callers (e.g. a bench)
# may override via order_by=.
_BYPASS_ORDER_BY: dict[str, str] = {
    "confidence": "ORDER BY MAX(mie.confidence) DESC, mi.id",
    "recency":    "ORDER BY MAX(mi.created_at) DESC, mi.id",
    "stable":     "ORDER BY mi.id",  # deterministic, signal-agnostic
}
_BYPASS_ORDER_DEFAULT = "confidence"


def _resolve_bypass_cap() -> int:
    """Effective base cap from M3_BYPASS_SURFACE_CAP (default 300), bounded by MAX."""
    raw = os.environ.get("M3_BYPASS_SURFACE_CAP")
    try:
        cap = int(raw) if raw else _BYPASS_SURFACE_CAP_DEFAULT
    except (TypeError, ValueError):
        cap = _BYPASS_SURFACE_CAP_DEFAULT
    return max(1, min(cap, _BYPASS_SURFACE_CAP_MAX))


def build_bypass_surface(
    conversation_ids: "list[str] | None" = None,
    *,
    scope: str = "agent",
    user_id: "str | None" = None,
    cap: "int | None" = None,
    strategy_for: "dict[str, str] | str | None" = None,
    category_for: "dict[str, str] | str | None" = None,
    type_rules: "dict[str, tuple] | None" = None,
    order_by: str = "confidence",
) -> dict:
    """Materialize bypass_surface for the given scopes (ADR-0001).

    Modes (ADR-0001 §10 Q2):
      * Full build:        conversation_ids=None -> (re)build every conversation in scope.
      * Incremental:       conversation_ids=[...] -> rebuild ONLY those (DELETE + re-insert).
                           The caller that mutated the entities passes its dirty list; the
                           builder does NOT auto-detect change.

    Scope-correct (ADR-0001 §7): every query is filtered by conversation_id + scope
    (+ user_id when given). An empty conversation_id in the list raises (no global scan).

    `strategy_for` (ADR-0001 §10 Q1): per-conversation strategy gating. Either a
    {conversation_id: strategy} map, a single strategy string applied to all, or None
    (treated as the default policy = surface, full cap). Core has no strategy router yet,
    so the caller supplies this; when a router lands it feeds the same param.

    `category_for` ({conversation_id: category} map or single string) + `type_rules`
    ({category: (entity_type, ...)}): the CALLER's rules-based-rule policy. Each scope's
    category resolves through type_rules to the entity types to surface, so the cap is
    spent on type-RELEVANT mentions (the reproduction-bench fidelity fix). Core embeds no
    question regexes or domain categories — the caller (a future strategy_router, or the
    bench) supplies both. A `"*"` key in type_rules is the fallback for unknown category.
    A scope resolving to no types surfaces nothing (conservative — avoids flooding).

    Returns a structured summary (never a string) — ADR-0001 §3.
    """
    base_cap = cap if cap is not None else _resolve_bypass_cap()
    if isinstance(strategy_for, str):
        default_strategy: "str | None" = strategy_for
        strat_map: dict[str, str] = {}
    else:
        default_strategy = None
        strat_map = strategy_for or {}
    if isinstance(category_for, str):
        default_category: "str | None" = category_for
        cat_map: dict[str, str] = {}
    else:
        default_category = None
        cat_map = category_for or {}
    rules = type_rules or {}
    # Whitelist the ordering — unknown value falls back to the default, never raw SQL (§6).
    order_clause = _BYPASS_ORDER_BY.get(order_by, _BYPASS_ORDER_BY[_BYPASS_ORDER_DEFAULT])

    from memory.backends import dialect

    _d = dialect()
    p = _d.param()

    built_scopes = 0
    rows_written = 0
    skipped_off = 0

    with _db() as db:
        # Resolve the scope set. Full build enumerates conversations present in scope;
        # incremental uses the caller's explicit list (each validated non-empty).
        if conversation_ids is None:
            params: list = [scope]
            uid_clause = ""
            if user_id:
                uid_clause = f" AND user_id = {p}"
                params.append(user_id)
            cids = [
                r[0] for r in db.execute(
                    f"SELECT DISTINCT conversation_id FROM memory_items "
                    f"WHERE conversation_id IS NOT NULL AND conversation_id != '' "
                    f"AND scope = {p}{uid_clause}",
                    params,
                ).fetchall()
            ]
        else:
            cids = []
            for c in conversation_ids:
                if not c:
                    raise ValueError("build_bypass_surface: empty conversation_id "
                                     "(would trigger a cross-scope scan) — ADR-0001 §7")
                cids.append(c)

        for cid in cids:
            strategy = strat_map.get(cid, default_strategy)
            policy = (_BYPASS_STRATEGY_POLICY.get(strategy, _BYPASS_DEFAULT_POLICY)
                      if strategy is not None else _BYPASS_DEFAULT_POLICY)

            # Incremental: clear this scope's existing surface rows before re-inserting.
            db.execute(f"DELETE FROM bypass_surface WHERE conversation_id = {p}", (cid,))
            built_scopes += 1

            if not policy["enabled"]:
                skipped_off += 1
                continue

            eff_cap = max(1, int(base_cap * policy["cap_frac"]))

            # Category -> entity-type filter (caller's rules) so the cap is spent on
            # type-RELEVANT mentions, not arbitrary entity-bearing items (fidelity fix).
            category = cat_map.get(cid, default_category)
            etypes = set(rules.get(category, ()) if category is not None else ())
            if not etypes:
                etypes = set(rules.get(_BYPASS_WILDCARD_CATEGORY, ()))  # caller fallback
            if not etypes:
                # No types resolved for this scope: surface nothing (conservative).
                continue

            # Entity-mention surface: memory_items in THIS scope carrying an entity of a
            # matched type, capped. Scope-isolated by construction (mi.conversation_id=?).
            # The observation surface is added by the caller's enrichment layer separately
            # (ADR-0001 §10 Q3) and is conditional on enrichment having run.
            uid_clause = f" AND mi.user_id = {p}" if user_id else ""
            type_ph = _d.placeholder(len(etypes))
            q_params: list = [cid, scope]
            if user_id:
                q_params.append(user_id)
            q_params.extend(sorted(etypes))
            q_params.append(eff_cap)
            # Deterministic cap priority (whitelisted ORDER BY — never raw SQL, §6).
            # GROUP BY mi.id so one row per item; the aggregate ordering decides which
            # items survive the cap. order_clause is from the whitelist only.
            surfaced = db.execute(
                f"SELECT mi.id FROM memory_item_entities mie "
                f"JOIN memory_items mi ON mi.id = mie.memory_id "
                f"JOIN entities e ON e.id = mie.entity_id "
                f"WHERE mi.conversation_id = {p} AND mi.scope = {p}{uid_clause} "
                f"AND e.entity_type IN ({type_ph}) "
                f"GROUP BY mi.id "
                f"{order_clause} "
                f"LIMIT {p}",
                q_params,
            ).fetchall()

            if surfaced:
                _ins = _d.insert_or_ignore()
                _suffix = _d.on_conflict_ignore(
                    conflict_target="(conversation_id, memory_id)"
                )
                db.executemany(
                    f"{_ins} bypass_surface "
                    "(conversation_id, memory_id, source, strategy, user_id, scope, cap) "
                    f"VALUES ({p}, {p}, 'entity', {p}, {p}, {p}, {p}) {_suffix}".rstrip(),
                    [(cid, r[0], strategy, user_id, scope, eff_cap) for r in surfaced],
                )
                rows_written += len(surfaced)

        try:
            db.commit()
        except Exception:  # pragma: no cover - defensive
            pass
        # Keep planner stats fresh for the new table (ADR-0001 §8 — the prototype
        # showed the planner needs ANALYZE to pick the scope index).
        try:
            db.execute("ANALYZE bypass_surface")
            db.commit()
        except Exception:  # pragma: no cover
            pass

    return {
        "scopes_built": built_scopes,
        "scopes_skipped_off_policy": skipped_off,
        "rows_written": rows_written,
        "cap": base_cap,
        "mode": "incremental" if conversation_ids is not None else "full",
    }
