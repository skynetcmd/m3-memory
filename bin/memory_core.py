"""Core memory primitives: single + bulk write, search, enrichment, emitters.

Not a CLI — imported by MCP server, bench drivers, and import scripts.

---

## Public async API (relevant to ingest)

`memory_write_impl(...)` — single-item insert with full enrichment chain.
Exposed as the `memory_write` MCP tool; accepts `variant` and `embed_text`.

`memory_write_bulk_impl(items, *, enrich=None, check_contradictions=None,
emit_conversation=None, variant=None)` — batch insert for benchmarks / imports.
Routes embeddings through `_embed_many`. Per-item fields (type, content,
metadata, conversation_id, variant, embed, embed_text, auto_classify) are
honored. Kwargs:

| Kwarg | Default | Default behavior |
|---|---|---|
| `enrich` | `None` | Inherit env gates `M3_INGEST_AUTO_TITLE` and `M3_INGEST_AUTO_ENTITIES`. `True` forces both on, `False` forces both off. |
| `check_contradictions` | `None` | OFF (bulk default differs from single-insert to protect throughput on large imports). `True` enables bounded contradiction check (Semaphore(8)), `False` explicit off. |
| `emit_conversation` | `None` | ON when items carry `conversation_id` and `type=='message'`. `False` disables event/window/gist emitters. Sub-emitters are additionally gated by env vars `M3_INGEST_EVENT_ROWS`, `M3_INGEST_WINDOW_CHUNKS`, `M3_INGEST_GIST_ROWS`. |
| `variant` | `None` | No default variant tag. When set, acts as fallback when an item doesn't carry its own `variant`. Per-item `variant` always wins. |

Of these, only `variant` is exposed on the MCP `memory_write` schema and via
`--variant` on bench CLIs. `enrich` / `check_contradictions` /
`emit_conversation` are kwarg-only perf knobs for bulk ingest drivers.

---

## Env-var gates read

Ingest: `M3_INGEST_AUTO_TITLE`, `M3_INGEST_AUTO_ENTITIES`,
`M3_INGEST_EVENT_ROWS`, `M3_INGEST_WINDOW_CHUNKS`, `M3_INGEST_GIST_ROWS`,
`M3_INGEST_WINDOW_SIZE`, `M3_INGEST_GIST_MIN_TURNS`, `M3_INGEST_GIST_STRIDE`.

Retrieval / ranking: `M3_QUERY_TYPE_ROUTING`, `M3_TITLE_MATCH_BOOST`,
`M3_SHORT_TURN_THRESHOLD`, `M3_SPEAKER_IN_TITLE`, `M3_IMPORTANCE_WEIGHT`,
`SEARCH_ROW_CAP`.

Embeddings: `EMBED_MODEL`, `EMBED_DIM`, `EMBED_BULK_CHUNK`,
`EMBED_BULK_CONCURRENCY`, `CHROMA_BASE_URL`.

Other: `CONTRADICTION_THRESHOLD`, `DEDUP_LIMIT`, `DEDUP_THRESHOLD`,
`LLM_TIMEOUT`, `ORIGIN_DEVICE`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import sqlite3
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable  # noqa: F401 (used in annotations)

import yaml
from crypto_provider import get_sha256 as _sha256_hex_py
from llm_failover import get_best_embed, get_best_llm, get_smallest_llm
from m3_sdk import M3Context, resolve_db_path

# ── Modularization shim (Phase 1) ─────────────────────────────────────────────
# The constants and Rust-core reference below now live in bin/memory/config.py.
# This module re-exports them to preserve back-compat for callers that import
# directly from memory_core. See docs/MEMORY_CORE_MODULARIZATION.md.
#
# Mutable config-shapes (`_EMBED_URL_OVERRIDE`, `_EMBED_MODEL_OVERRIDE`) live
# on `memory.config` as module attributes. Code that WRITES them must do so
# through the module attribute (e.g. `config._EMBED_URL_OVERRIDE = url`), not
# via a local binding here, or the writes won't be observable to other
# modules importing through `memory.config`.
from memory import config as _mc_config  # noqa: F401 — used for mutable attrs
from memory.config import (  # noqa: F401 — re-exports
    _OXIDATION_DISABLED,
    m3_core_rs,
    _EMBED_URL_OVERRIDE,
    _EMBED_MODEL_OVERRIDE,
    BASE_DIR,
    DB_PATH,
    ARCHIVE_DB_PATH,
    EMBED_MODEL,
    EMBED_DIM,
    EMBED_TIMEOUT_READ,
    ORIGIN_DEVICE,
    DEDUP_LIMIT,
    DEDUP_THRESHOLD,
    CONTRADICTION_THRESHOLD,
    SUPERSEDES_PENALTY,
    CONTRADICTION_TITLE_GATE,
    CONTRADICTION_TYPE_EXCLUSIONS,
    AUTO_RELATED_LINK,
    AUTO_RELATED_LINK_SCOPE_BY_VARIANT,
    SEARCH_ROW_CAP,
    LLM_TIMEOUT,
    SPEAKER_IN_TITLE,
    SHORT_TURN_THRESHOLD,
    TITLE_MATCH_BOOST,
    IMPORTANCE_WEIGHT,
    ELBOW_MIN_INPUT,
    ELBOW_MIN_RETURN,
    ELBOW_ABS_THRESHOLD,
    EXPANSION_DISPLACEMENT_MARGIN,
    EXPANSION_PROTECTED_RANKS,
    ENTITY_SEED_STOPLIST,
    INGEST_WINDOW_CHUNKS,
    INGEST_GIST_ROWS,
    INGEST_EVENT_ROWS,
    QUERY_TYPE_ROUTING,
    INTENT_ROUTING,
    INTENT_USER_FACT_BOOST,
    INGEST_WINDOW_SIZE,
    INGEST_GIST_MIN_TURNS,
    INGEST_GIST_STRIDE,
    ENABLE_FACT_ENRICHED,
    FACT_ENRICH_CONCURRENCY,
    FACT_ENRICH_MAX_ATTEMPTS,
    ENABLE_ENTITY_GRAPH,
    ENTITY_EXTRACT_CONCURRENCY,
    ENTITY_EXTRACT_MAX_ATTEMPTS,
    ENTITY_RESOLVE_FUZZY_MIN,
    ENTITY_RESOLVE_COSINE_MIN,
    _DEFAULT_VALID_ENTITY_TYPES,
    _DEFAULT_VALID_ENTITY_PREDICATES,
    DEFAULT_ENTITY_VOCAB_YAML,
    _ENV_ENTITY_VOCAB_YAML,
    DEFAULT_RERANK_MODEL,
    DEFAULT_CHANGE_AGENT,
    CHROMA_BASE_URL,
    CHROMA_COLLECTION,
    CHROMA_COLLECTIONS,
    CHROMA_V2_PREFIX,
    CHROMA_CONNECT_T,
    CHROMA_READ_T,
    CHROMA_PULL_PAGE_SIZE,
    CHROMA_CONTENT_MAX,
    FEDERATION_LOW_SCORE_THRESHOLD,
)
from memory.util import sha256_hex as _sha256_hex  # noqa: F401 — re-export
from memory.fts import (  # noqa: F401 — re-exports
    _FTS_OPERATORS,
    _sanitize_fts,
    _SEARCHABLE_PUNCT,
    _sanitize_for_searchable,
    _compile_fts_query,
    _TOKEN_SPLIT,
    _augment_title_with_role,
    _query_title_token_set,
    _title_overlap_from_qset,
    _query_title_overlap,
)
# Legacy back-compat: `_lru_cache` was imported inline in the FTS block
# (used internally by `_compile_fts_query`'s @decorator). Preserved as a
# re-export because the API snapshot captured it as a public symbol —
# something external imports it. Pure alias for functools.lru_cache.
from functools import lru_cache as _lru_cache  # noqa: F401 — re-export
# Phase 2.B: SQLite primitives, schema lifecycle, history, gate cache, and
# the access-stamp batcher live on bin/memory/db.py. The mutable sets/dicts
# (_initialized_dbs, _GATE_CACHE, _access_pending) are externally imported;
# the shim preserves their identity by re-exporting the references rather
# than re-assigning them.
from memory import db as _mc_db  # noqa: F401
from memory.db import (  # noqa: F401 — re-exports
    _local,
    _init_lock,
    _initialized,
    _initialized_dbs,
    _GATE_CACHE,
    _GATE_CACHE_TTL,
    _OBS_COUNT_QUERY,
    _ENTITY_COUNT_QUERY,
    _ACCESS_FLUSH_INTERVAL,
    _access_pending,
    _access_flusher_task,
    _access_lock,
    _db,
    _conn,
    _ensure_sync_tables,
    _backfill_change_agent,
    _lazy_init,
    _record_history,
    memory_history_impl,
    _gate_count_query,
    _gate_active,
    _access_stamp_flusher,
    _enqueue_access_stamps,
)
# Phase 3: The embedding pipeline (cascade, in-process embedder lazy-init,
# sliding-window chunking, dense recovery, anchor augmentation, HTTP-client
# singleton, backend stats, canonical-name cache) lives in bin/memory/embed.py.
# The mutable containers (_EMBED_BACKEND_STATS, _ENTITY_NAME_EMBED_CACHE,
# _embedded_embedder/_checked) preserve identity through these re-exports.
from memory import embed as _mc_embed  # noqa: F401
from memory.embed import (  # noqa: F401 — re-exports
    _EMBED_GGUF_PATH,
    _EMBED_GGUF_MODEL_TAG,
    _embedded_embedder,
    _embedded_embed_checked,
    _get_embedded_embedder,
    MAX_CHARS_PER_CHUNK,
    MIN_OVERLAP_CHARS,
    STRIDE_CHARS,
    _chunk_for_sliding_window,
    DENSE_TARGET_TOKENS,
    DENSE_TOKEN_OVERLAP,
    DENSE_MIN_SUB_CHARS,
    _DENSE_ERR_RE,
    _subdivide_dense_chunk,
    _augment_embed_text_with_anchors,
    _content_hash,
    _EMBED_HTTP_MAX_CONNS,
    _EMBED_HTTP_MAX_KEEPALIVE,
    _EMBED_HTTP_KEEPALIVE_EXPIRY,
    _EMBED_CLIENT,
    _EMBED_CLIENT_LOOP_ID,
    _EMBED_CLIENT_LOCK,
    _shared_embed_client,
    _get_embed_client,
    _EMBED_FALLBACK_URL,
    _EMBED_BACKEND_STATS,
    _EMBED_BACKEND_STATS_LOCK,
    _record_embed_backend,
    get_embed_backend_stats,
    reset_embed_backend_stats,
    _embedded_label,
    set_embed_override,
    _EMBED_SEM,
    _EMBED_DIM_VALIDATED,
    EMBED_BULK_CHUNK,
    EMBED_BULK_CONCURRENCY,
    _EMBED_BULK_SEM,
    _embed,
    _embed_many,
    _ENTITY_NAME_EMBED_CACHE,
    ENTITY_NAME_EMBED_CACHE_MAX,
    _embed_canonical_cached,
    embedder_status_impl,
    # Typed exceptions for log-line clarity (D in the perf audit). Cascade
    # contract unchanged — callers still see (None, model) on total failure;
    # these classes only surface in log lines via their type names.
    EmbedError,
    EmbeddedBackendError,
    EmbedFallbackError,
    EmbedPrimaryError,
    EmbedSemaphoreTimeout,
)
# Phase 4.A: Chroma federation helpers (queue insert, collection-id cache,
# federated query) moved to bin/memory/chroma.py. _CHROMA_COLLECTION_ID_CACHE
# preserves identity through this re-export.
from memory import chroma as _mc_chroma  # noqa: F401
from memory.chroma import (  # noqa: F401
    _CHROMA_COLLECTION_ID_CACHE,
    _queue_chroma,
    _resolve_chroma_collection_id,
    _query_chroma,
)
# Phase 4.B (in progress): search/retrieval moves to bin/memory/search.py.
# Sub-commit 1 brings just the scoring helpers; search-impls land in later
# sub-commits. _batch_cosine moved to memory.util (write-path + search-path
# co-tenant).
from memory.util import _batch_cosine  # noqa: F401 — re-export
from memory import search as _mc_search  # noqa: F401
from memory.search import (  # noqa: F401
    _cosine_batch_packed,
    _hybrid_score_batch,
    _recency_bonus_ranks,
    _EVENT_PROPER_NOUN,
    _TEMPORAL_QUERY_RE,
    _DATE_RE_ISO,
    _DATE_RE_LONG,
    _DATE_MONTHS,
    _pull_predecessor_turns,
    _maybe_route_query,
    _apply_recency_bonus,
    _trim_by_elbow,
    _apply_temporal_boost,
    _RERANKER_MODEL,
    _RERANKER_MODEL_NAME,
    _get_reranker,
    _enforce_expansion_displacement_guard,
    _apply_rerank,
    _TEMPORAL_ROUTER_PATTERNS,
    _TEMPORAL_ROUTER_RE,
    _ENTITY_MENTION_PATTERNS,
    _ENTITY_MENTION_RE,
    _UNSET,
    _extract_caller_overrides,
    _apply_auto_layer,
    _apply_sharp_trim,
    is_temporal_query,
    memory_search_scored_impl,
    memory_search_routed_impl,
    _maybe_expand_routed,
    memory_search_multi_db_impl,
    memory_search_impl,
)
# Phase 6: entity-extraction subsystem (vocab loading, canonical-name
# resolution, entity CRUD, queue runner, MCP read-side impls) lives in
# bin/memory/entity.py. Externally-imported state — VALID_ENTITY_TYPES,
# VALID_ENTITY_PREDICATES, _ENTITY_EXTRACT_SEM, _PENDING_ENTITY_TASKS —
# is re-exported via `from .entity import ...` so object identity
# survives the shim (callers comparing `id(mc.VALID_ENTITY_TYPES)`
# against `id(memory.entity.VALID_ENTITY_TYPES)` stay equal).
from memory import entity as _mc_entity  # noqa: F401
from memory.entity import (  # noqa: F401 — re-exports
    load_entity_vocab,
    VALID_ENTITY_TYPES,
    VALID_ENTITY_PREDICATES,
    _TOKEN_PUNCT_RE,
    _token_jaccard,
    _ENTITY_EXTRACT_SEM,
    _PENDING_ENTITY_TASKS,
    _resolve_entity,
    _resolve_entity_async,
    _create_entity,
    _link_memory_to_entity,
    _link_entity_relationship,
    _enqueue_entity_extraction,
    _run_entity_extractor,
    _try_extract_or_enqueue,
    _select_pending_entity_extraction,
    extract_pending_impl,
    entity_extractor_health,
    entity_search_impl,
    entity_get_impl,
)


# In-process llama.cpp embedding backend. Opt-in: set M3_EMBED_GGUF to the
# bge-m3 GGUF path. Unset (default) -> the HTTP embed path is used unchanged.
# Guarded on EMBED_DIM: if the GGUF's dimension doesn't match, the embedded
# path is disabled and HTTP is used, rather than writing incompatible vectors
# into the index. M3_CORE_RS_DISABLE forces HTTP regardless.
#
# Vectors from the embedded path are tagged with M3_EMBED_GGUF_MODEL_TAG
# (default 'bge-m3-GGUF-Q4_K_M.gguf' — the llama.cpp-served bge-m3 tag the
# embedded backend is parity-verified against, cosine ~0.996 vs stored rows
# with that tag). This is a distinct cache namespace from LM Studio's
# 'text-embedding-bge-m3' rows; the embedded backend IS llama.cpp, so it
# belongs with the llama.cpp-tagged vectors.
# In-process Rust embedder (_EMBED_GGUF_PATH, _EMBED_GGUF_MODEL_TAG,
# _embedded_embedder, _embedded_embed_checked, _get_embedded_embedder) moved
# to bin/memory/embed.py in Phase 3. Re-exported via the shim at the top.


# Scoring helpers (_batch_cosine -> memory.util; _cosine_batch_packed,
# _hybrid_score_batch, _recency_bonus_ranks -> memory.search) moved in
# Phase 4.B sub-commit 1. Re-exported via the shim at the top.




async def conversation_summarize_impl(conversation_id: str, threshold: int = 20) -> str:
    """Summarizes a conversation into key points using the local LLM."""
    # 1. Fetch all messages for the conversation
    with _db() as db:
        rows = db.execute(
            """SELECT mi.title AS role, mi.content
               FROM memory_relationships mr
               JOIN memory_items mi ON mr.to_id = mi.id
               WHERE mr.from_id = ? AND mr.relationship_type = 'message' AND mi.is_deleted = 0
               ORDER BY mi.created_at ASC""",
            (conversation_id,)
        ).fetchall()

    # 2. Threshold check
    if len(rows) < threshold:
        return f"Conversation too short to summarize ({len(rows)} messages, threshold={threshold})"

    # 3. Concatenate messages
    messages_text = "\n".join(f"{row['role']}: {row['content']}" for row in rows)

    # 4. Call the local LLM via failover logic
    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    client = _get_embed_client()
    result = await get_best_llm(client, token)
    if not result:
        return "Error: No local LLM available for summarization."

    base_url, model = result
    prompt = f"Summarize this conversation into 3-5 key points. Preserve facts, decisions, and action items.\n\n{messages_text}"

    try:
        resp = await client.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=LLM_TIMEOUT
        )
        resp.raise_for_status()
        summary_text = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Summarization failed: {e}")
        from llm_failover import clear_failover_caches
        clear_failover_caches()
        return f"Error during LLM summarization: {type(e).__name__}: {e}"

    # 5. Store the summary as a new memory item
    summary_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        db.execute(
            "INSERT INTO memory_items (id, type, title, content, created_at, content_hash) VALUES (?, 'summary', ?, ?, ?, ?)",
            (summary_id, f"Summary of {conversation_id[:8]}", summary_text, now, _content_hash(summary_text))
        )

    # 6. Link it to the conversation
    memory_link_impl(summary_id, conversation_id, "references")

    _record_history(summary_id, "create", None, summary_text, "content", "system")
    return summary_text


# embedder_status_impl moved to bin/memory/embed.py in Phase 3.
# Re-exported via the shim at the top.
from embedding_utils import (
    batch_cosine as _batch_cosine_py,
)
from embedding_utils import (
    infer_change_agent as _infer_change_agent_util,
)
from embedding_utils import (
    pack as _pack,
)
from embedding_utils import (
    unpack as _unpack,
)
from embedding_utils import (
    unpack_many as _unpack_many,
)
from embedding_utils import HAS_NUMPY as _HAS_NUMPY

if _HAS_NUMPY:
    import numpy as _np  # type: ignore

logger = logging.getLogger("memory_core")
# Default context (memory/agent_memory.db unless M3_DATABASE overrides at
# import time). Per-call DB overrides flow through the active_database
# ContextVar and _current_ctx() below — this attribute is kept for legacy
# callers that reference ctx.get_secret(), ctx.get_async_client(), etc.
ctx = M3Context.for_db(None)


def _current_ctx() -> M3Context:
    """Return the M3Context for the currently active DB path.

    Honors (in order): active_database() ContextVar > M3_DATABASE env > default.
    Cached per path so the hot path avoids repeat pool construction.
    """
    return M3Context.for_db(resolve_db_path(None))

# ── Constants ─────────────────────────────────────────────────────────────────
# All ~25 constants formerly defined here moved to bin/memory/config.py in
# Phase 1; the re-export shim at the top of this file makes them available
# under their legacy names (BASE_DIR, EMBED_MODEL, DEDUP_LIMIT,
# CONTRADICTION_*, AUTO_RELATED_LINK*, SEARCH_ROW_CAP, LLM_TIMEOUT,
# SPEAKER_IN_TITLE, SHORT_TURN_THRESHOLD, TITLE_MATCH_BOOST,
# IMPORTANCE_WEIGHT, ELBOW_*).

# Expansion/ingest/intent/fact-enrich/entity-graph constants formerly defined
# here moved to bin/memory/config.py in Phase 1 (EXPANSION_*, ENTITY_SEED_STOPLIST,
# INGEST_*, INTENT_*, ENABLE_FACT_ENRICHED, FACT_ENRICH_*, ENABLE_ENTITY_GRAPH,
# ENTITY_EXTRACT_*, ENTITY_RESOLVE_*). The re-export shim at the top of this
# file makes them available under their legacy names.

# Entity vocab defaults (`_DEFAULT_VALID_ENTITY_TYPES`,
# `_DEFAULT_VALID_ENTITY_PREDICATES`, `DEFAULT_ENTITY_VOCAB_YAML`,
# `_ENV_ENTITY_VOCAB_YAML`) and the reranker default model name
# (`DEFAULT_RERANK_MODEL`) moved to bin/memory/config.py in Phase 1. The
# re-export shim at the top of this file makes them available under their
# legacy names.
#
# `_RERANKER_MODEL` / `_RERANKER_MODEL_NAME` are search-state mutables that
# `_get_reranker` writes to. They stay here until Phase 4 extracts search.py.
# Reranker family (_RERANKER_MODEL, _RERANKER_MODEL_NAME, _get_reranker,
# _enforce_expansion_displacement_guard, _apply_rerank) moved to
# bin/memory/search.py in Phase 4.B sub-4. Re-exported via the shim at the top.




# Module-level: load defaults at import. Existing callers see same contents as before.

VALID_CHANGE_AGENTS = {"claude", "gemini", "aider", "openclaw", "deepseek", "grok", "manual", "system", "unknown", "legacy"}

# FTS5 query helpers and title-overlap math moved to bin/memory/fts.py in
# Phase 2 (_FTS_OPERATORS, _sanitize_fts, _SEARCHABLE_PUNCT, _sanitize_for_searchable,
# _compile_fts_query, _TOKEN_SPLIT, _augment_title_with_role, _query_title_token_set,
# _title_overlap_from_qset, _query_title_overlap). Re-exported via the shim at
# the top of this file.


# Sliding-window chunking, dense-content recovery, and anchor augmentation
# (MAX_CHARS_PER_CHUNK, MIN_OVERLAP_CHARS, STRIDE_CHARS,
# _chunk_for_sliding_window, DENSE_*, _DENSE_ERR_RE, _subdivide_dense_chunk,
# _augment_embed_text_with_anchors) moved to bin/memory/embed.py in Phase 3.
# Re-exported via the shim at the top.


# Heuristic event extraction. Matches "<Name> <verb> ... <date-ish>" patterns
# in a single turn. Returns a list of (sentence, verb) pairs. Emitted as
# type='event_extraction' rows by _maybe_emit_event_rows.
_EVENT_VERB_LIST = (
    "went", "visited", "met", "started", "joined", "attended", "bought",
    "moved", "celebrated", "finished", "began", "saw", "watched", "played",
    "traveled", "arrived", "left", "returned", "called", "texted", "married",
    "graduated", "quit", "hired", "adopted", "painted",
)
# _EVENT_PROPER_NOUN moved to bin/memory/search.py in Phase 4.B sub-2
# (its hot reader is _maybe_route_query). _extract_event_sentences below
# imports it via the shim re-export at the top of this file.
_EVENT_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_EVENT_DATE_HINT = re.compile(
    r"\b(yesterday|today|tomorrow|last|this|next|ago|on\s+\d|"
    r"january|february|march|april|may|june|july|august|"
    r"september|october|november|december|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|\d{4})\b",
    re.IGNORECASE,
)
_EVENT_VERB_RE = re.compile(
    r"\b(" + "|".join(_EVENT_VERB_LIST) + r")\b", re.IGNORECASE
)


def _extract_event_sentences(content: str) -> list[tuple[str, str]]:
    """Return list of (sentence, verb) for sentences that mention a proper
    noun, one of the event verbs, and a date-ish token. Cheap regex only."""
    if not content:
        return []
    out: list[tuple[str, str]] = []
    for sent in _EVENT_SENT_SPLIT.split(content):
        s = sent.strip()
        if len(s) < 12 or len(s) > 400:
            continue
        if not _EVENT_PROPER_NOUN.search(s):
            continue
        m = _EVENT_VERB_RE.search(s)
        if not m:
            continue
        if not _EVENT_DATE_HINT.search(s):
            continue
        out.append((s, m.group(1).lower()))
        if len(out) >= 4:
            break
    return out


# Temporal query regexes (_TEMPORAL_QUERY_RE, _DATE_RE_ISO, _DATE_RE_LONG,
# _DATE_MONTHS), _pull_predecessor_turns, and _maybe_route_query moved to
# bin/memory/search.py in Phase 4.B sub-2. Re-exported via the shim at the top.


async def _maybe_emit_event_rows(
    content: str,
    metadata: str | dict | None,
    conversation_id: str,
    user_id: str,
    parent_id: str,
) -> None:
    """Extract event-like sentences from a message and emit one
    type='event_extraction' row per match, linked back to the parent via
    `references`. Embed_text includes resolved temporal anchors so date
    queries can hit these rows directly. Idempotent: skipped if the caller
    did not provide a conversation_id."""
    if not conversation_id:
        return
    events = _extract_event_sentences(content)
    if not events:
        return
    meta_dict: dict[str, Any] = {}
    if metadata:
        try:
            meta_dict = metadata if isinstance(metadata, dict) else json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            meta_dict = {}
    session_id = meta_dict.get("session_id", "")
    for sent, verb in events:
        ev_meta = {
            "source_message_id": parent_id,
            "verb": verb,
            "session_id": session_id,
            "temporal_anchors": meta_dict.get("temporal_anchors") or [],
        }
        try:
            created = await memory_write_impl(
                type="event_extraction",
                content=sent,
                title=f"event:{verb}",
                metadata=json.dumps(ev_meta),
                user_id=user_id,
                source="event_extraction",
                conversation_id=conversation_id,
                embed=True,
            )
            m = re.search(r"Created:\s*([a-f0-9-]+)", created or "")
            if m:
                try:
                    memory_link_impl(m.group(1), parent_id, "references")
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"event_extraction emit failed: {e}")


async def _maybe_emit_window_chunk(conversation_id: str, user_id: str) -> None:
    """Emit a sliding 3-turn (INGEST_WINDOW_SIZE) summary row that embeds the
    concatenated text of the most recent N message rows in a conversation.
    Fires only on turns whose count is a multiple of the window size, so a
    conversation of 9 turns emits 3 window rows rather than 9 overlapping
    ones. Does not fire until at least INGEST_WINDOW_SIZE turns exist."""
    if not conversation_id:
        return
    try:
        with _db() as db:
            rows = db.execute(
                "SELECT id, content, title FROM memory_items "
                "WHERE conversation_id = ? AND type = 'message' "
                "AND is_deleted = 0 ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
    except Exception as e:
        logger.debug(f"window chunk query failed: {e}")
        return
    n = len(rows)
    if n < INGEST_WINDOW_SIZE or (n % INGEST_WINDOW_SIZE) != 0:
        return
    window_rows = rows[-INGEST_WINDOW_SIZE:]
    joined = "\n".join((r["content"] or "") for r in window_rows if r["content"])
    if not joined.strip():
        return
    try:
        await memory_write_impl(
            type="summary",
            content=joined,
            title=f"window:{conversation_id}:{n}",
            metadata=json.dumps({
                "kind": "window_chunk",
                "window_end_turn": n,
                "window_size": INGEST_WINDOW_SIZE,
                "source_message_ids": [r["id"] for r in window_rows],
            }),
            user_id=user_id,
            source="window_chunk",
            conversation_id=conversation_id,
            embed=True,
        )
    except Exception as e:
        logger.debug(f"window chunk emit failed: {e}")


async def _maybe_emit_gist_row(conversation_id: str, user_id: str) -> None:
    """Emit a heuristic gist row for a conversation once it has passed
    INGEST_GIST_MIN_TURNS turns, and every INGEST_GIST_STRIDE additional
    turns thereafter. The gist concatenates the first sentence of each
    message and a deduped list of capitalized tokens seen across the
    conversation — cheap, deterministic, no LLM."""
    if not conversation_id:
        return
    try:
        with _db() as db:
            rows = db.execute(
                "SELECT id, content FROM memory_items "
                "WHERE conversation_id = ? AND type = 'message' "
                "AND is_deleted = 0 ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
    except Exception as e:
        logger.debug(f"gist query failed: {e}")
        return
    n = len(rows)
    if n < INGEST_GIST_MIN_TURNS:
        return
    if ((n - INGEST_GIST_MIN_TURNS) % INGEST_GIST_STRIDE) != 0:
        return
    sentences: list[str] = []
    entities: list[str] = []
    seen_ent: set[str] = set()
    for r in rows:
        c = (r["content"] or "").strip()
        if not c:
            continue
        first = _EVENT_SENT_SPLIT.split(c, maxsplit=1)[0]
        if first:
            sentences.append(first[:200])
        for m in _EVENT_PROPER_NOUN.findall(c):
            if m not in seen_ent:
                seen_ent.add(m)
                entities.append(m)
            if len(entities) >= 16:
                break
    if not sentences:
        return
    gist = " | ".join(sentences[:12])
    if entities:
        gist = f"[{', '.join(entities[:16])}] {gist}"
    try:
        await memory_write_impl(
            type="summary",
            content=gist,
            title=f"gist:{conversation_id}:{n}",
            metadata=json.dumps({
                "kind": "conversation_gist",
                "turn_count": n,
                "entities": entities[:16],
            }),
            user_id=user_id,
            source="conversation_gist",
            conversation_id=conversation_id,
            embed=True,
        )
    except Exception as e:
        logger.debug(f"gist emit failed: {e}")


_POISON_PATTERNS = [
    re.compile(r'<script\b', re.I),
    re.compile(r'(?:DROP|DELETE|ALTER)\s+TABLE', re.I),
    re.compile(r'__import__|\bexec\s*\(|\beval\s*\(', re.I),
    re.compile(r'(?:ignore|disregard)\s+(?:all\s+)?(?:previous|prior)\s+instructions', re.I),
]

def _check_content_safety(content: str) -> str | None:
    """Returns error message if content appears malicious, None if safe."""
    if not content:
        return None
    for pattern in _POISON_PATTERNS:
        if pattern.search(content):
            return f"Error: content rejected — matches safety pattern: {pattern.pattern[:50]}"
    return None

# DEFAULT_CHANGE_AGENT, CHROMA_*, FEDERATION_LOW_SCORE_THRESHOLD moved to
# bin/memory/config.py in Phase 1. Re-exported via the shim at the top.

# _local / _init_lock / _initialized moved to bin/memory/db.py in Phase 2.B.
# Re-exported via the shim at the top.
# _EMBED_SEM and _EMBED_DIM_VALIDATED moved to bin/memory/embed.py in Phase 3.
# Enrichment-pipeline semaphores stay here until enrich.py is extracted.
_FACT_ENRICH_SEM = asyncio.Semaphore(FACT_ENRICH_CONCURRENCY)

_COST_COUNTERS = {"embed_calls": 0, "embed_tokens_est": 0, "search_calls": 0, "write_calls": 0}
_PENDING_FACT_TASKS: set[asyncio.Task] = set()
_CLASSIFY_CACHE = {}

async def _auto_classify(content: str, title: str) -> str:
    """Uses the local LLM to classify a memory into a valid type."""
    c_hash = _content_hash(content + title)
    if c_hash in _CLASSIFY_CACHE:
        return _CLASSIFY_CACHE[c_hash]

    # Localized copy of mcp_tool_catalog.VALID_MEMORY_TYPES minus "auto"
    # (auto is the sentinel that requests classification, not a classifier output).
    # Kept local to avoid circular import: mcp_tool_catalog imports memory_core.
    # Keep this list in sync with mcp_tool_catalog.VALID_MEMORY_TYPES.
    valid_types = {
        "note", "fact", "decision", "preference", "conversation", "message",
        "task", "code", "config", "observation", "plan", "summary", "snippet",
        "reference", "log", "home", "user_fact", "scratchpad", "knowledge",
        "event_extraction", "fact_enriched", "chat_log",
        "local_device", "network_config", "infrastructure", "home_automation",
        "migration-log", "security",
        "windows_only", "macos_only", "linux_only", "to_do",
    }

    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
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


# ── Ingest-time LLM enrichment (opt-in) ──────────────────────────────────────
# Gated by env vars so behavior matches today's default (no extra LLM calls at
# write time) unless explicitly enabled. Intended for production callers that
# pass blank titles / want entity-tagged metadata without running heuristics
# themselves. All helpers fail-open: on any error, they return the untouched
# input so ingest never fails because LLM enrichment did.

def _ingest_llm_enabled(flag: str) -> bool:
    return os.environ.get(flag, "0").strip().lower() in ("1", "true", "yes", "on")

# ── Phase L: auto-activation of retrieval gates by data presence ───────────
# Phase J added M3_PREFER_OBSERVATIONS / M3_TWO_STAGE_OBSERVATIONS /
# M3_ENABLE_ENTITY_GRAPH as default-off env gates for back-compat. Phase L
# auto-flips them ON when the underlying tables have meaningful population,
# so users don't have to remember to flip env vars + restart after enrichment
# data lands. Escape hatch: M3_DISABLE_AUTO_ACTIVATION=1 falls back to
# explicit-env-only (used by bench harnesses for reproducibility).
# Gate cache (_GATE_CACHE, _GATE_CACHE_TTL, _gate_count_query, _gate_active,
# _OBS_COUNT_QUERY, _ENTITY_COUNT_QUERY) moved to bin/memory/db.py in Phase 2.B.
# Re-exported via the shim at the top.

def _prefer_observations_gate() -> bool:
    return _gate_active("M3_PREFER_OBSERVATIONS", _OBS_COUNT_QUERY, threshold=100)

def _two_stage_observations_gate() -> bool:
    # Paired with PREFER_OBSERVATIONS: same trigger.
    return _gate_active("M3_TWO_STAGE_OBSERVATIONS", _OBS_COUNT_QUERY, threshold=100)

def _enable_entity_graph_gate() -> bool:
    return _gate_active("M3_ENABLE_ENTITY_GRAPH", _ENTITY_COUNT_QUERY, threshold=1)


_AUTO_TITLE_CACHE: dict[str, str] = {}
_AUTO_ENTITIES_CACHE: dict[str, list[str]] = {}

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
        token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
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
        token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
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


def _track_cost(operation: str, tokens_est: int = 0):
    _COST_COUNTERS[operation] = _COST_COUNTERS.get(operation, 0) + 1
    if tokens_est:
        _COST_COUNTERS["embed_tokens_est"] += tokens_est

# Phase 2.B: _ensure_sync_tables, _backfill_change_agent, _initialized_dbs,
# _lazy_init, _db, _conn, _record_history, memory_history_impl all moved to
# bin/memory/db.py. Re-exported via the shim at the top.

# _content_hash moved to bin/memory/embed.py in Phase 3. Re-exported via the shim.

# HTTP-client singleton, fallback URL, backend stats, _embedded_label,
# and set_embed_override moved to bin/memory/embed.py in Phase 3.
# Re-exported via the shim at the top.
import httpx as _httpx  # kept; other memory_core code may reference _httpx
# Legacy back-compat: `_ThreadLock` was imported inline in the original
# embed-stats block. The API snapshot captured it as a public symbol;
# preserve as an explicit re-export. Pure alias for threading.Lock.
from threading import Lock as _ThreadLock  # noqa: F401


# The cascade itself (_embed, _embed_many, EMBED_BULK_CHUNK,
# EMBED_BULK_CONCURRENCY, _EMBED_BULK_SEM, _EMBED_SEM, _EMBED_DIM_VALIDATED)
# moved to bin/memory/embed.py in Phase 3. Re-exported via the shim at the top.


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

        unique_vecs = await _embed_many(unique_texts)
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


# _queue_chroma moved to bin/memory/chroma.py in Phase 4.A.
# Re-exported via the shim at the top.

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
    """
    Detects contradictions with existing memories of the same type.
    Returns (superseded_ids, related_candidates) where related_candidates
    are (id, score) pairs with cosine > 0.7 that are NOT contradictions.

    When `variant` is non-None and `AUTO_RELATED_LINK_SCOPE_BY_VARIANT` is on
    (default), candidate scan is restricted to memories of the same variant.
    This prevents cross-variant contamination during obs INSERT.
    """
    superseded = []
    related = []
    try:
        with _db() as db:
            # Find top-5 similar memories of the same type
            where = "mi.is_deleted = 0 AND mi.type = ? AND mi.id != ?"
            params = [type_, item_id]
            if agent_id:
                where += " AND mi.agent_id = ?"
                params.append(agent_id)
            if variant is not None and AUTO_RELATED_LINK_SCOPE_BY_VARIANT:
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
                    # Contradiction detected — supersede old memory.
                    # Bi-temporal validity (Zep/Graphiti pattern, 2026-04-27):
                    # close the older memory's validity interval at new memory's
                    # valid_from. Falls back to now() when caller didn't supply
                    # a valid_from. Lets retrieval that filters by `as_of` see
                    # the older fact as still-valid before the supersession point.
                    _now_iso = datetime.now(timezone.utc).isoformat()
                    _close_at = new_valid_from or _now_iso
                    with _db() as db:
                        db.execute(
                            "UPDATE memory_items SET is_deleted = 1, "
                            "valid_to = COALESCE(valid_to, ?), updated_at = ? "
                            "WHERE id = ?",
                            (_close_at, _now_iso, row["id"]),
                        )
                        db.execute(
                            "INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?,?,?,?,?)",
                            (str(uuid.uuid4()), item_id, row["id"], "supersedes", _now_iso)
                        )
                    _record_history(row["id"], "supersede", row["content"], item_id, "content")
                    superseded.append(row["id"])
                    logger.info(f"Memory {item_id} supersedes {row['id']} (contradiction detected, valid_to={_close_at})")
            elif score > 0.7:
                related.append((row["id"], score))
    except Exception as e:
        logger.debug(f"Contradiction check failed: {e}")
    return superseded, related


# ── Fact enrichment pipeline (Phase 4-5) ──────────────────────────────────────
async def _try_enrich_or_enqueue(memory_id: str, content: str, fact_enricher, db, variant: str | None = None, allowlist: set[str] | None = None) -> None:
    """Non-blocking: try enrichment under semaphore; on miss, enqueue.

    Variant-skip rule: if variant is not None and (allowlist is None or variant not in allowlist),
    return without doing anything.
    """
    if not ENABLE_FACT_ENRICHED or fact_enricher is None:
        return

    # Skip variant rows unless explicitly allowed
    if variant is not None and (allowlist is None or variant not in allowlist):
        return

    # Try non-blocking acquire with very short timeout
    try:
        async with asyncio.timeout(0.001):  # try-acquire only
            await _FACT_ENRICH_SEM.acquire()
    except (asyncio.TimeoutError, Exception):
        # Semaphore full or error — enqueue and return immediately
        _enqueue_fact_enrichment(memory_id, db)
        return

    # Acquired semaphore — spawn task and track it
    task = asyncio.create_task(_run_fact_enricher(memory_id, content, fact_enricher))
    _PENDING_FACT_TASKS.add(task)
    task.add_done_callback(lambda t: _PENDING_FACT_TASKS.discard(t))


def _enqueue_fact_enrichment(memory_id: str, db) -> None:
    """INSERT OR IGNORE into fact_enrichment_queue."""
    try:
        db.execute(
            "INSERT OR IGNORE INTO fact_enrichment_queue(memory_id) VALUES (?)",
            (memory_id,)
        )
    except Exception as e:
        logger.debug(f"Failed to enqueue fact enrichment for {memory_id}: {e}")


async def _run_fact_enricher(memory_id: str, content: str, fact_enricher) -> None:
    """Run the actual fact extractor with error handling and retries."""
    try:
        facts = await fact_enricher(content)
        if facts:
            await _write_fact_rows(memory_id, facts)
    except Exception as e:
        # Record error and bump attempts in queue
        try:
            with _db() as db:
                db.execute("""
                    INSERT OR REPLACE INTO fact_enrichment_queue(memory_id, attempts, last_error, last_attempt_at)
                    VALUES (?, COALESCE((SELECT attempts FROM fact_enrichment_queue WHERE memory_id=?),0)+1, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                """, (memory_id, memory_id, str(e)[:500]))
        except Exception as db_err:
            logger.debug(f"Failed to record enrichment error for {memory_id}: {db_err}")
    finally:
        _FACT_ENRICH_SEM.release()


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
            with _db() as db:
                # Insert the fact row
                db.execute(
                    "INSERT INTO memory_items (id, type, title, content, metadata_json, change_agent, source, origin_device, scope, created_at, content_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
                    "INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?,?,?,?,?)",
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


# ── Entity-relation graph pipeline (Phase 4-5) ───────────────────────────────






# Process-global cache for canonical_name embeddings used in Tier-3 cosine
# resolution. Key: canonical_name (text). Value: list[float] embedding.
# Bounded by ENTITY_NAME_EMBED_CACHE_MAX (env, default 50000); on overflow
# the cache is dropped wholesale (rare in normal usage; cap is defensive).
# The cache is process-local and not invalidated when a row is updated/
# deleted because canonical_name → embedding is a stable function (the
# embedder is deterministic at temperature 0). Persisting to disk is a
# v2-class improvement; for now in-memory is sufficient to convert
# Tier-3 from O(N) embed calls per new entity to O(1) after warmup.
# _ENTITY_NAME_EMBED_CACHE, ENTITY_NAME_EMBED_CACHE_MAX, and
# _embed_canonical_cached moved to bin/memory/embed.py in Phase 3.
# Re-exported via the shim at the top.
















# _CHROMA_COLLECTION_ID_CACHE, _resolve_chroma_collection_id, _query_chroma
# moved to bin/memory/chroma.py in Phase 4.A. Re-exported via the shim.

# _apply_recency_bonus, _trim_by_elbow, _apply_temporal_boost moved to
# bin/memory/search.py in Phase 4.B sub-3. Re-exported via the shim at the top.


# ── Fire-and-forget access-stamp batcher ─────────────────────────────────────
# Updating last_accessed_at / access_count on every search hit used to add a
# WAL-fsync write transaction to the read path. We now buffer the ids per event
# loop and flush them in a single UPDATE every _ACCESS_FLUSH_INTERVAL seconds.
# Telemetry drift (a few seconds of latency on last_accessed_at) is acceptable;
# the read path's median latency is not.
# Access-stamp batcher (_ACCESS_FLUSH_INTERVAL, _access_pending,
# _access_flusher_task, _access_lock, _access_stamp_flusher,
# _enqueue_access_stamps) moved to bin/memory/db.py in Phase 2.B.
# Re-exported via the shim at the top.


# Phase 4.B sub-6+7: memory_search_scored_impl moved to bin/memory/search.py.
# Re-exported via the shim at the top.



# Route helpers (_TEMPORAL_ROUTER_*, _ENTITY_MENTION_*, _UNSET,
# _extract_caller_overrides, _apply_auto_layer, _apply_sharp_trim,
# is_temporal_query) moved to bin/memory/search.py in Phase 4.B sub-5.
# Re-exported via the shim at the top.


def _graph_neighbor_ids(seed_ids: list, depth: int) -> set:
    """Return the set of memory_item ids reachable within `depth` hops from any
    item in `seed_ids` via memory_relationships, excluding the seeds themselves.

    Used by memory_search_routed_impl when graph_depth > 0. Returns set[str].

    SQL note: `WHERE from_id IN (...) OR to_id IN (...)` defeats SQLite's
    per-column indexes (idx_mr_from / idx_mr_to in migration 001) and forces a
    table scan. The UNION form below lets the planner use each index
    independently, which scales with `len(frontier)` rather than with table
    size.
    """
    if depth <= 0 or not seed_ids:
        return set()
    depth = min(int(depth), 3)
    seen: set = set(seed_ids)
    frontier: set = set(seed_ids)
    with _db() as db:
        for _ in range(depth):
            if not frontier:
                break
            frontier_list = list(frontier)
            placeholders = ",".join("?" * len(frontier_list))
            rows = db.execute(
                f"SELECT to_id AS nid FROM memory_relationships "
                f"WHERE from_id IN ({placeholders}) "
                f"UNION "
                f"SELECT from_id AS nid FROM memory_relationships "
                f"WHERE to_id IN ({placeholders})",
                frontier_list + frontier_list,
            ).fetchall()
            next_frontier: set = set()
            for r in rows:
                nid = r["nid"]
                if nid not in seen:
                    seen.add(nid)
                    next_frontier.add(nid)
            frontier = next_frontier
    seen.difference_update(seed_ids)
    return seen


def _session_neighbor_ids(seed_ids: list, session_cap: int = 12) -> dict:
    """For each conversation_id present in `seed_ids`' rows, return up to
    session_cap turns from that conversation (excluding seeds themselves).

    Returns dict[memory_id -> row_dict]. Used by memory_search_routed_impl
    when expand_sessions=True. The session_cap is applied per session.
    """
    if not seed_ids:
        return {}
    out: dict = {}
    with _db() as db:
        placeholders = ",".join(["?"] * len(seed_ids))
        seed_rows = db.execute(
            f"SELECT id, conversation_id FROM memory_items WHERE id IN ({placeholders})",
            seed_ids,
        ).fetchall()
        seed_set = set(seed_ids)
        seen_conv: set = set()
        for sr in seed_rows:
            cid = sr["conversation_id"]
            if not cid or cid in seen_conv:
                continue
            seen_conv.add(cid)
            cap = max(1, int(session_cap))
            rows = db.execute(
                "SELECT id, type, title, content, metadata_json, conversation_id, "
                "valid_from, user_id FROM memory_items "
                "WHERE conversation_id = ? AND COALESCE(is_deleted, 0) = 0 "
                "ORDER BY valid_from LIMIT ?",
                (cid, cap),
            ).fetchall()
            for r in rows:
                if r["id"] in seed_set or r["id"] in out:
                    continue
                out[r["id"]] = dict(r)
    return out


async def _entity_graph_neighbor_ids(
    query: str, depth: int, max_neighbors: int, db,
    valid_types: list = None,
    valid_predicates: list = None,
    entity_stoplist: list = None,
    _capture_dict: dict = None,
) -> set:
    """Parse query for entity mentions, traverse entity_relationships up to `depth`
    hops, and return a set of memory_id values linked to the discovered entities.

    Algorithm (Phase 6, regex-only — no SLM):
      1. Extract candidate mentions from query via _ENTITY_MENTION_RE.
      2. Lookup each candidate in `entities` table (exact then LIKE, cap 5/candidate).
         If valid_types is given, restrict entity lookup to those entity_type values.
         Stoplisted canonical_names (case-insensitive) are excluded.
      3. BFS over `entity_relationships` up to min(depth, 3) hops,
         capped at min(max_neighbors, 100) total entity nodes.
         If valid_predicates is given, only traverse edges with matching predicate.
         Stoplisted entities are dropped from the frontier.
      4. Fetch memory_ids from `memory_item_entities` for all discovered entities.

    valid_types: list of allowed entity_type strings; None = use VALID_ENTITY_TYPES defaults.
    valid_predicates: list of allowed predicate strings; None = use VALID_ENTITY_PREDICATES defaults.
    entity_stoplist: list of canonical_name strings (case-insensitive) to never seed
      from or expand to. None = use M3_ENTITY_SEED_STOPLIST env default.
      Pass [] to explicitly disable filtering.

    Returns set[str] of memory_ids. Returns empty set on any early-exit condition.
    """
    if not query or not query.strip():
        return set()

    # Clamp to safe limits (mirrors memory_graph_impl clamp for depth)
    depth = min(int(depth), 3)
    max_neighbors = min(int(max_neighbors), 100)

    # Step 1 — extract candidate mention strings
    candidates: list[str] = []
    seen_cands: set[str] = set()
    for m in _ENTITY_MENTION_RE.finditer(query):
        text = m.group(0).strip("\"'")
        if text and text not in seen_cands:
            seen_cands.add(text)
            candidates.append(text)

    if not candidates:
        return set()

    # Step 2 — entity lookup: collect matched entity_ids
    try:
        # Quick check: is the entities table populated at all?
        count_row = db.execute("SELECT COUNT(*) AS cnt FROM entities").fetchone()
        if count_row["cnt"] == 0:
            return set()
    except Exception:  # noqa: BLE001
        return set()

    # Resolve entity stoplist: caller list (incl. explicit []) > env default.
    _stoplist_lower: tuple = ()
    if entity_stoplist is None:
        _stoplist_lower = ENTITY_SEED_STOPLIST
    else:
        _stoplist_lower = tuple(s.strip().lower() for s in entity_stoplist if s and s.strip())
    _stop_clause = ""
    _stop_params: list = []
    if _stoplist_lower:
        _stop_ph = ",".join(["?"] * len(_stoplist_lower))
        _stop_clause = f" AND LOWER(canonical_name) NOT IN ({_stop_ph})"
        _stop_params = list(_stoplist_lower)

    # Pre-compute stoplisted entity IDs so we can drop them from the BFS
    # frontier even if a non-stoplisted seed has them as a 1-hop neighbor.
    _stoplisted_eids: set[str] = set()
    if _stoplist_lower:
        try:
            sl_rows = db.execute(
                f"SELECT id FROM entities WHERE LOWER(canonical_name) IN ({','.join(['?']*len(_stoplist_lower))})",
                list(_stoplist_lower),
            ).fetchall()
            _stoplisted_eids = {r["id"] for r in sl_rows}
        except Exception:  # noqa: BLE001
            _stoplisted_eids = set()

    # Build optional entity_type filter clause (caller-provided list overrides core defaults)
    _type_clause = ""
    _type_params: list = []
    if valid_types:
        _type_ph = ",".join(["?"] * len(valid_types))
        _type_clause = f" AND entity_type IN ({_type_ph})"
        _type_params = list(valid_types)

    # Pre-compute stoplisted-candidate count for telemetry. A candidate is
    # "dropped at seed" if its lowercased form matches the stoplist exactly —
    # that's the case the LIKE-tier filter wouldn't redeem either, so it's a
    # true seed-rejection rather than a "no exact match, fell through to LIKE"
    # event. Cheap O(N) set check; no extra SQL.
    seeds_dropped = (
        sum(1 for c in candidates if c.lower() in _stoplist_lower)
        if _stoplist_lower else 0
    )

    matched_entity_ids: set[str] = set()
    # Tier 1 (batched): one query for all candidate exact-matches.
    # idx_entities_canonical_type covers the equality predicate. We learn which
    # candidates resolved so we know which need the Tier-2 LIKE fallback.
    resolved_cands: set[str] = set()
    try:
        cand_ph = ",".join("?" * len(candidates))
        tier1_rows = db.execute(
            f"SELECT id, canonical_name FROM entities "
            f"WHERE canonical_name IN ({cand_ph}){_type_clause}{_stop_clause}",
            list(candidates) + _type_params + _stop_params,
        ).fetchall()
        for r in tier1_rows:
            matched_entity_ids.add(r["id"])
            resolved_cands.add(r["canonical_name"])
    except Exception:  # noqa: BLE001
        pass

    # Tier 2 (per-candidate LIKE): only run for candidates that didn't resolve
    # in Tier 1, capped at 5 hits each — matches the legacy LIMIT 5.
    for candidate in candidates:
        if candidate in resolved_cands:
            continue
        try:
            rows = db.execute(
                f"SELECT id FROM entities WHERE LOWER(canonical_name) LIKE LOWER(?){_type_clause}{_stop_clause} LIMIT 5",
                [f"%{candidate}%"] + _type_params + _stop_params,
            ).fetchall()
            for r in rows:
                matched_entity_ids.add(r["id"])
        except Exception:  # noqa: BLE001
            continue

    if _capture_dict is not None:
        _capture_dict["entity_seeds_dropped"] = seeds_dropped
        _capture_dict["entity_stoplist_size"] = len(_stoplist_lower)

    if not matched_entity_ids:
        return set()

    # Build optional predicate filter clause for BFS (caller-provided list overrides core defaults)
    _pred_clause = ""
    _pred_params: list = []
    if valid_predicates:
        _pred_ph = ",".join(["?"] * len(valid_predicates))
        _pred_clause = f" AND predicate IN ({_pred_ph})"
        _pred_params = list(valid_predicates)

    # Step 3 — BFS over entity_relationships up to `depth` hops.
    # SQL note: same OR-of-IN antipattern fix as `_graph_neighbor_ids`. The
    # idx_er_from / idx_er_to indexes are (from_entity, predicate) and
    # (to_entity, predicate); the UNION form lets each index serve its half.
    seen_entities: set[str] = set(matched_entity_ids)
    frontier: set[str] = set(matched_entity_ids)
    frontier_dropped = 0
    for _ in range(depth):
        if not frontier or len(seen_entities) >= max_neighbors:
            break
        frontier_list = list(frontier)
        placeholders = ",".join("?" * len(frontier_list))
        try:
            rel_rows = db.execute(
                f"SELECT to_entity AS neighbor FROM entity_relationships "
                f"WHERE from_entity IN ({placeholders}){_pred_clause} "
                f"UNION "
                f"SELECT from_entity AS neighbor FROM entity_relationships "
                f"WHERE to_entity IN ({placeholders}){_pred_clause}",
                frontier_list + _pred_params + frontier_list + _pred_params,
            ).fetchall()
        except Exception:  # noqa: BLE001
            break
        next_frontier: set[str] = set()
        for r in rel_rows:
            eid = r["neighbor"]
            if eid in _stoplisted_eids:
                if eid not in seen_entities:
                    frontier_dropped += 1
                continue
            if eid not in seen_entities:
                seen_entities.add(eid)
                next_frontier.add(eid)
                if len(seen_entities) >= max_neighbors:
                    break
        frontier = next_frontier

    if _capture_dict is not None:
        _capture_dict["entity_frontier_dropped"] = frontier_dropped

    # Step 4 — memory_item lookup
    if not seen_entities:
        return set()
    try:
        placeholders = ",".join(["?"] * len(seen_entities))
        mie_rows = db.execute(
            f"SELECT DISTINCT memory_id FROM memory_item_entities "
            f"WHERE entity_id IN ({placeholders})",
            list(seen_entities),
        ).fetchall()
        return {r["memory_id"] for r in mie_rows}
    except Exception:  # noqa: BLE001
        return set()


async def _score_extra_rows(query: str, rows_by_id: dict, base_score: float = 0.0) -> list:
    """Score additional rows (from graph or session expansion) against the query.

    Reuses the standard embedding path. Each returned tuple is (score, item_dict)
    matching memory_search_scored_impl's shape. Items are scored by cosine vs
    query embedding. If embedding lookup fails for a row, it gets `base_score`.
    """
    if not rows_by_id:
        return []
    out: list = []
    qvec, _ = await _embed(query)
    if qvec is None:
        # No embedding model available — fall back to base_score for all
        for rid, item in rows_by_id.items():
            out.append((base_score, item))
        return out
    with _db() as db:
        ids = list(rows_by_id.keys())
        placeholders = ",".join("?" * len(ids))
        emb_rows = db.execute(
            f"SELECT memory_id, embedding FROM memory_embeddings "
            f"WHERE memory_id IN ({placeholders})",
            ids,
        ).fetchall()
    # Batched packed-cosine: aligned by id so scoring is one parallel pass.
    fetched_ids: list = [er["memory_id"] for er in emb_rows]
    fetched_blobs: list = [er["embedding"] for er in emb_rows]
    fetched_scores = _cosine_batch_packed(qvec, fetched_blobs, EMBED_DIM) if fetched_blobs else []
    score_by_id: dict = dict(zip(fetched_ids, fetched_scores))
    for rid, item in rows_by_id.items():
        s = score_by_id.get(rid)
        if s is None:
            out.append((base_score, item))
        else:
            out.append((float(s), item))
    return out


# Phase 4.B sub-6+7: memory_search_routed_impl, _maybe_expand_routed,
# memory_search_multi_db_impl, memory_search_impl moved to bin/memory/search.py.
# Re-exported via the shim at the top.


async def memory_suggest_impl(query: str, k: int = 5, variant: str = "__none__") -> str:
    """Returns which memories would be retrieved for a query and explains why."""
    return await memory_search_impl(query, k=k, explain=True, variant=variant)

def memory_get_impl(id):
    # Accept either a 36-char UUID (existing path) or an 8-char prefix
    # (resume-guides and conversations routinely cite memories by their
    # first 8 hex chars). Anything else is a length error — we don't try
    # to be clever about other prefix lengths because the index only
    # covers SUBSTR(id,1,8).
    ident = (id or "").strip()
    if len(ident) == 36:
        with _db() as db:
            row = db.execute("SELECT * FROM memory_items WHERE id = ?", (ident,)).fetchone()
            if not row:
                # Fall back to chroma_mirror for items pulled from remote
                mirror = db.execute("SELECT * FROM chroma_mirror WHERE id = ?", (ident,)).fetchone()
                if mirror:
                    return json.dumps(dict(mirror), indent=2, default=str)
                return "Error: not found"
        return json.dumps(dict(row), indent=2, default=str)
    if len(ident) == 8:
        with _db() as db:
            rows = db.execute(
                "SELECT * FROM memory_items WHERE SUBSTR(id,1,8) = ?",
                (ident,),
            ).fetchall()
            if not rows:
                # Fall back to chroma_mirror by prefix as well, for symmetry
                # with the full-UUID path above.
                mirror_rows = db.execute(
                    "SELECT * FROM chroma_mirror WHERE SUBSTR(id,1,8) = ?",
                    (ident,),
                ).fetchall()
                if len(mirror_rows) == 1:
                    return json.dumps(dict(mirror_rows[0]), indent=2, default=str)
                if len(mirror_rows) > 1:
                    ids = ", ".join(r["id"] for r in mirror_rows)
                    return f"Error: ambiguous prefix '{ident}': matches {ids}"
                return "Error: not found"
            if len(rows) > 1:
                ids = ", ".join(r["id"] for r in rows)
                return f"Error: ambiguous prefix '{ident}': matches {ids}"
        return json.dumps(dict(rows[0]), indent=2, default=str)
    return "Error: id must be 36-char UUID or 8-char prefix"

def memory_verify_impl(memory_id: str) -> str:
    """Verify content integrity by comparing stored hash with computed hash."""
    with _db() as db:
        row = db.execute("SELECT content, content_hash FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return f"Error: memory {memory_id} not found"
        stored_hash = row["content_hash"] or ""
        computed_hash = _sha256_hex((row["content"] or "").encode("utf-8"))
        if not stored_hash:
            return f"Warning: no content hash stored for {memory_id}. Computed: {computed_hash}"
        if stored_hash == computed_hash:
            return f"Integrity OK: {memory_id} (hash: {computed_hash[:16]}...)"
        return f"INTEGRITY VIOLATION: {memory_id} — stored hash {stored_hash[:16]}... != computed {computed_hash[:16]}..."

def memory_cost_report_impl() -> str:
    """Returns current session cost/usage counters."""
    lines = ["Memory Operation Costs (this session):"]
    for key, val in sorted(_COST_COUNTERS.items()):
        lines.append(f"  {key}: {val}")
    return "\n".join(lines)

async def memory_update_impl(id, content="", title="", metadata="", importance=-1.0, reembed=False, refresh_on="", refresh_reason="", conversation_id=""):
    if isinstance(metadata, dict):
        metadata = json.dumps(metadata)
    elif not isinstance(metadata, str):
        metadata = ""
    now = datetime.now(timezone.utc).isoformat()
    try:
        importance = float(importance)
    except (TypeError, ValueError):
        importance = -1.0
    with _db() as db:
        # Read old values for audit trail
        old = db.execute(
            "SELECT content, title, refresh_on, refresh_reason, conversation_id FROM memory_items WHERE id = ?",
            (id,)
        ).fetchone()
        if content:
            _record_history(id, "update", old["content"] if old else None, content, "content", db=db)
            db.execute("UPDATE memory_items SET content = ? WHERE id = ?", (content, id))
        if title:
            _record_history(id, "update", old["title"] if old else None, title, "title", db=db)
            db.execute("UPDATE memory_items SET title = ? WHERE id = ?", (title, id))
        if importance >= 0: db.execute("UPDATE memory_items SET importance = ? WHERE id = ?", (importance, id))
        if metadata: db.execute("UPDATE memory_items SET metadata_json = ? WHERE id = ?", (metadata, id))
        # Refresh lifecycle: empty string leaves unchanged, "clear" clears, anything
        # else is treated as a new ISO timestamp. Using the explicit sentinel "clear"
        # lets callers distinguish "no change" from "mark as refreshed, remove reminder".
        if refresh_on:
            new_val = None if refresh_on == "clear" else refresh_on
            _record_history(id, "update", old["refresh_on"] if old else None, new_val, "refresh_on", db=db)
            db.execute("UPDATE memory_items SET refresh_on = ? WHERE id = ?", (new_val, id))
        if refresh_reason:
            new_val = None if refresh_reason == "clear" else refresh_reason
            _record_history(id, "update", old["refresh_reason"] if old else None, new_val, "refresh_reason", db=db)
            db.execute("UPDATE memory_items SET refresh_reason = ? WHERE id = ?", (new_val, id))
        if conversation_id:
            new_val = None if conversation_id == "clear" else conversation_id
            _record_history(id, "update", old["conversation_id"] if old else None, new_val, "conversation_id", db=db)
            db.execute("UPDATE memory_items SET conversation_id = ? WHERE id = ?", (new_val, id))
        db.execute("UPDATE memory_items SET updated_at = ? WHERE id = ?", (now, id))
    if reembed and content:
        vec, m = await _embed(content)
        if vec:
            with _db() as db:
                db.execute("UPDATE memory_embeddings SET embedding = ?, embed_model = ? WHERE memory_id = ?", (_pack(vec), m, id))
    return f"Updated: {id}"

def _cosine(v1: list[float], v2: list[float]) -> float:
    """Cosine similarity. Routes through the Rust core when available."""
    if m3_core_rs is not None and len(v1) == len(v2):
        return m3_core_rs.cosine(v1, v2)
    from embedding_utils import cosine
    return cosine(v1, v2)

def memory_delete_impl(id, hard=False):
    """Deletes a MemoryItem (soft or hard). Implements cascade for hard delete (C5)."""
    with _db() as db:
        row = db.execute("SELECT id, content FROM memory_items WHERE id = ?", (id,)).fetchone()
        if not row:
            return f"Error: item {id} not found"
        _record_history(id, "delete", row["content"], None, "content", db=db)
        if hard:
            db.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (id,))
            db.execute("DELETE FROM memory_relationships WHERE from_id = ? OR to_id = ?", (id, id))
            db.execute("DELETE FROM chroma_sync_queue WHERE memory_id = ?", (id,))
            db.execute("DELETE FROM memory_items WHERE id = ?", (id,))
        else:
            db.execute("UPDATE memory_items SET is_deleted = 1, updated_at = ? WHERE id = ?",
                       (datetime.now(timezone.utc).isoformat(), id))
            # Drop any pending upsert in chroma_sync_queue — the row is no
            # longer eligible for sync. The tombstone enqueue (if the caller
            # uses _queue_chroma(..., 'delete') downstream) is unaffected.
            db.execute(
                "DELETE FROM chroma_sync_queue WHERE memory_id = ? AND operation = 'upsert'",
                (id,),
            )
    return f"{'Hard' if hard else 'Soft'}-deleted: {id}"


# SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds, 32766
# on 3.32.0+. We chunk well below the conservative cap so bulk deletes work
# on every shipped Python/SQLite combo without per-host probing.
_MEMORY_DELETE_BULK_CHUNK = 500


def memory_delete_bulk_impl(ids, hard=False):
    """Delete a list of MemoryItems in one transaction per chunk.

    Behaviorally identical to looping `memory_delete_impl` over `ids`, but
    with two key differences that make it suitable for curation passes:

    1. **Batched SQL.** Each chunk of up to _MEMORY_DELETE_BULK_CHUNK ids runs
       a single `IN (?,?,...)` per affected table (memory_items,
       memory_embeddings, memory_relationships, chroma_sync_queue), inside
       one `_db()` connection. For 178 deletes this collapses ~712 individual
       statements + 178 connection-opens into ~8 statements + 1 connection.

    2. **Structured result.** Returns `{succeeded, not_found, mode}` instead
       of N string lines, so curation callers don't have to parse text.

    History rows are still written per-id via `_record_history` so the
    audit trail matches `memory_delete_impl` exactly.

    Args:
        ids: iterable of memory_item UUID strings.
        hard: if True, cascade-delete (memory_embeddings, memory_relationships,
              chroma_sync_queue, memory_items). If False (default), soft-delete
              with the same chroma_sync_queue upsert-pending cleanup that
              memory_delete_impl performs.

    Returns:
        dict: {
          "succeeded": [list of ids successfully deleted],
          "not_found": [list of ids that did not exist],
          "mode": "hard" | "soft",
        }

    Note: this is a destructive tool (default_allowed=False in the MCP
    catalog). The MCP proxy only exposes it when MCP_PROXY_ALLOW_DESTRUCTIVE
    is set, matching `memory_delete`'s gating.
    """
    id_list = list(dict.fromkeys(ids or []))  # dedupe while preserving order
    if not id_list:
        return {"succeeded": [], "not_found": [], "mode": "hard" if hard else "soft"}

    succeeded: list[str] = []
    not_found: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        for start in range(0, len(id_list), _MEMORY_DELETE_BULK_CHUNK):
            chunk = id_list[start : start + _MEMORY_DELETE_BULK_CHUNK]
            placeholders = ",".join("?" * len(chunk))

            # Discover which ids exist in this chunk. content is needed for
            # the history record; the rest is just existence.
            existing = db.execute(
                f"SELECT id, content FROM memory_items WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            existing_ids = [row["id"] for row in existing]
            existing_set = set(existing_ids)
            missing = [i for i in chunk if i not in existing_set]
            not_found.extend(missing)

            if not existing_ids:
                continue

            # Per-id history rows. Same call shape memory_delete_impl uses.
            for row in existing:
                _record_history(row["id"], "delete", row["content"], None, "content", db=db)

            ph_existing = ",".join("?" * len(existing_ids))
            if hard:
                db.execute(
                    f"DELETE FROM memory_embeddings WHERE memory_id IN ({ph_existing})",
                    existing_ids,
                )
                # memory_relationships matches on EITHER from_id or to_id.
                db.execute(
                    f"DELETE FROM memory_relationships "
                    f"WHERE from_id IN ({ph_existing}) OR to_id IN ({ph_existing})",
                    existing_ids + existing_ids,
                )
                db.execute(
                    f"DELETE FROM chroma_sync_queue WHERE memory_id IN ({ph_existing})",
                    existing_ids,
                )
                db.execute(
                    f"DELETE FROM memory_items WHERE id IN ({ph_existing})",
                    existing_ids,
                )
            else:
                db.execute(
                    f"UPDATE memory_items SET is_deleted = 1, updated_at = ? "
                    f"WHERE id IN ({ph_existing})",
                    [now_iso, *existing_ids],
                )
                # Mirror memory_delete_impl: drop pending upserts so soft-deleted
                # rows don't get re-published to chroma after the tombstone.
                db.execute(
                    f"DELETE FROM chroma_sync_queue "
                    f"WHERE memory_id IN ({ph_existing}) AND operation = 'upsert'",
                    existing_ids,
                )

            succeeded.extend(existing_ids)

    return {
        "succeeded": succeeded,
        "not_found": not_found,
        "mode": "hard" if hard else "soft",
    }


VALID_RELATIONSHIP_TYPES = {"related", "supports", "contradicts", "extends", "supersedes", "references", "message", "consolidates", "handoff", "precedes", "follows"}

def _memory_link_inner(from_id: str, to_id: str, relationship_type: str, db) -> str:
    # Verify both items exist
    for mid in (from_id, to_id):
        if not db.execute("SELECT id FROM memory_items WHERE id = ?", (mid,)).fetchone():
            return f"Error: memory {mid} not found"
    # Check for duplicate link
    existing = db.execute(
        "SELECT id FROM memory_relationships WHERE from_id = ? AND to_id = ? AND relationship_type = ?",
        (from_id, to_id, relationship_type)
    ).fetchone()
    if existing:
        return f"Link already exists: {existing['id']}"
    rid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?,?,?,?,?)",
        (rid, from_id, to_id, relationship_type, datetime.now(timezone.utc).isoformat())
    )
    return f"Linked: {from_id} --[{relationship_type}]--> {to_id} (id: {rid})"


def memory_link_impl(from_id: str, to_id: str, relationship_type: str = "related", db=None) -> str:
    """Creates a directional link between two memory items."""
    if relationship_type not in VALID_RELATIONSHIP_TYPES:
        return f"Error: invalid relationship type '{relationship_type}'. Valid: {', '.join(sorted(VALID_RELATIONSHIP_TYPES))}"

    if db is not None:
        return _memory_link_inner(from_id, to_id, relationship_type, db)

    with _db() as db:
        return _memory_link_inner(from_id, to_id, relationship_type, db)

def memory_graph_impl(memory_id: str, depth: int = 1) -> str:
    """Returns the local graph neighborhood of a memory item up to N hops."""
    depth = min(max(int(depth), 1), 3)  # Clamp to 1-3
    with _db() as db:
        # Verify item exists
        root = db.execute("SELECT id, title, type FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
        if not root:
            return f"Error: memory {memory_id} not found"

        # Recursive CTE to traverse relationships up to `depth` hops
        rows = db.execute("""
            WITH RECURSIVE graph(node_id, hop) AS (
                SELECT ?, 0
                UNION ALL
                SELECT CASE WHEN mr.from_id = g.node_id THEN mr.to_id ELSE mr.from_id END, g.hop + 1
                FROM memory_relationships mr
                JOIN graph g ON (mr.from_id = g.node_id OR mr.to_id = g.node_id)
                WHERE g.hop < ?
            )
            SELECT DISTINCT mi.id, mi.title, mi.type, g.hop
            FROM graph g
            JOIN memory_items mi ON g.node_id = mi.id
            WHERE mi.is_deleted = 0
            ORDER BY g.hop, mi.type
        """, (memory_id, depth)).fetchall()

        # Also get the edges
        node_ids = [r["id"] for r in rows]
        if not node_ids:
            return f"No graph neighborhood for {memory_id}"
        placeholders = ",".join(["?"] * len(node_ids))
        edges = db.execute(
            f"SELECT from_id, to_id, relationship_type FROM memory_relationships "
            f"WHERE from_id IN ({placeholders}) OR to_id IN ({placeholders})",
            node_ids + node_ids
        ).fetchall()

    lines = [f"Graph for {root['title'] or root['id']} (type={root['type']}, depth={depth}):"]
    lines.append(f"\nNodes ({len(rows)}):")
    for r in rows:
        hop_label = "ROOT" if r["id"] == memory_id else f"hop {r['hop']}"
        lines.append(f"  [{r['id'][:8]}] {r['title'] or '(untitled)'} (type={r['type']}, {hop_label})")

    # Filter edges to only those connecting our nodes
    node_set = set(node_ids)
    relevant_edges = [e for e in edges if e["from_id"] in node_set and e["to_id"] in node_set]
    if relevant_edges:
        lines.append(f"\nEdges ({len(relevant_edges)}):")
        for e in relevant_edges:
            lines.append(f"  {e['from_id'][:8]} --[{e['relationship_type']}]--> {e['to_id'][:8]}")

    return "\n".join(lines)

def memory_handoff_impl(from_agent: str, to_agent: str, task: str,
                        context_ids: list, note: str = "",
                        task_id: str = "") -> str:
    """Creates a handoff memory for inter-agent task transfer."""
    # 0. Validate agents are registered
    if not _agent_exists(to_agent):
        return f"Error: to_agent '{to_agent}' is not registered. Call agent_register first."
    if not _agent_exists(from_agent):
        return f"Error: from_agent '{from_agent}' is not registered. Call agent_register first."

    # 1. Generate new UUID
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # 2. Insert handoff memory directly via raw SQL
    with _db() as db:
        meta = {"from_agent": from_agent, "note": note}
        if task_id:
            meta["task_id"] = task_id
        metadata_json = json.dumps(meta)
        db.execute(
            "INSERT INTO memory_items (id, type, title, content, agent_id, scope, metadata_json, created_at, updated_at, is_deleted) "
            "VALUES (?, 'handoff', ?, ?, ?, 'agent', ?, ?, ?, 0)",
            (new_id, f"Handoff from {from_agent}", task, to_agent, metadata_json, now, now)
        )

    # 3. Link context items (each opens its own _db() context)
    for ctx_id in context_ids:
        try:
            memory_link_impl(new_id, ctx_id, "handoff")
        except Exception as e:
            logger.debug(f"Failed to link context {ctx_id}: {e}")

    # 4. Record history
    _record_history(new_id, "handoff_create", None, task, "content", from_agent)

    # 5. Fire-and-forget notify to to_agent
    try:
        notify_impl(to_agent, "handoff", {
            "memory_id": new_id,
            "from_agent": from_agent,
            "task": (task or "")[:200],
            "task_id": task_id or None,
        })
    except Exception as e:
        logger.warning(f"handoff notify failed for {to_agent}: {e}")

    # 6. Return status
    return f"Handoff created: {new_id} ({from_agent} -> {to_agent}, {len(context_ids)} context links)"

def memory_inbox_impl(agent_id: str, unread_only: bool = True, limit: int = 20) -> str:
    """Retrieves handoff messages for an agent, optionally filtered to unread."""
    # Build WHERE clause dynamically
    where_clause = "WHERE agent_id = ? AND type = 'handoff' AND is_deleted = 0"
    if unread_only:
        where_clause += " AND read_at IS NULL"

    # Query the inbox
    with _db() as db:
        rows = db.execute(
            f"SELECT id, title, content, metadata_json, created_at, read_at FROM memory_items "
            f"{where_clause} ORDER BY created_at DESC LIMIT ?",
            (agent_id, limit)
        ).fetchall()

    # Format result
    if not rows:
        return f"Inbox for {agent_id}: (empty)"

    lines = [f"Inbox for {agent_id} ({len(rows)} {'unread' if unread_only else 'total'}):"]
    for row in rows:
        # Parse from_agent from metadata_json
        from_agent = "?"
        try:
            meta = json.loads(row["metadata_json"] or "{}")
            from_agent = meta.get("from_agent", "?")
        except Exception:
            pass

        # Truncate task (content) to 60 chars
        task_truncated = (row["content"] or "")[:60]
        lines.append(f"  [{row['id'][:8]}] from={from_agent} task={task_truncated} created={row['created_at']}")

    return "\n".join(lines)

def memory_inbox_ack_impl(memory_id: str) -> str:
    """Marks a handoff memory as read."""
    # 1. Compute current timestamp
    now = datetime.now(timezone.utc).isoformat()

    # 2. Update read_at and updated_at
    with _db() as db:
        db.execute(
            "UPDATE memory_items SET read_at = ?, updated_at = ? WHERE id = ? AND type = 'handoff' AND is_deleted = 0",
            (now, now, memory_id)
        )

        # Verify update actually happened
        verify = db.execute(
            "SELECT id FROM memory_items WHERE id = ? AND type = 'handoff' AND is_deleted = 0 AND read_at IS NOT NULL",
            (memory_id,)
        ).fetchone()

    # 3. Check result
    if not verify:
        return f"Error: memory {memory_id} not found or not a handoff"

    # 4. Record history and return
    _record_history(memory_id, "handoff_ack", None, now, "read_at", "")
    return f"Acked: {memory_id}"

def _count_refresh_backlog(agent_id: str = "") -> int:
    """Cheap count of memories whose refresh_on has arrived. Used by lifecycle
    hooks (agent_register / agent_offline) and maintenance to surface the
    backlog without expanding the full list. Backed by the partial index
    idx_mi_refresh_on, so this is O(index-size-of-flagged-rows).
    """
    now = datetime.now(timezone.utc).isoformat()
    where = ["is_deleted = 0", "refresh_on IS NOT NULL", "refresh_on <= ?"]
    params: list = [now]
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    try:
        with _db() as db:
            row = db.execute(
                f"SELECT COUNT(*) FROM memory_items WHERE {' AND '.join(where)}",
                params,
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        # refresh_on column may not exist on an un-migrated DB — fail quiet.
        return 0

def _refresh_hint(agent_id: str = "") -> str:
    """One-line hint suitable for appending to lifecycle response strings.
    Returns empty string when there is no backlog, so callers can concatenate
    unconditionally.
    """
    n = _count_refresh_backlog(agent_id)
    if n <= 0:
        return ""
    noun = "memory" if n == 1 else "memories"
    scope = "of yours" if agent_id else "in the store"
    return f" | {n} {noun} {scope} due for refresh (see memory_refresh_queue)"

def memory_refresh_queue_impl(agent_id: str = "", limit: int = 50, include_future: bool = False) -> str:
    """Lists memories whose refresh_on timestamp has arrived (or all with refresh_on set
    if include_future=True). Read-only — actual refresh goes through memory_update.

    Surfaces memories flagged for periodic review via the refresh_on lifecycle.
    Scope to an agent with agent_id, or leave empty to see everything.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 500))

    where = ["is_deleted = 0", "refresh_on IS NOT NULL"]
    params: list = []
    if not include_future:
        where.append("refresh_on <= ?")
        params.append(now)
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    where_sql = " AND ".join(where)

    with _db() as db:
        rows = db.execute(
            f"SELECT id, type, title, refresh_on, refresh_reason, agent_id, updated_at "
            f"FROM memory_items WHERE {where_sql} ORDER BY refresh_on ASC LIMIT ?",
            (*params, limit)
        ).fetchall()

    if not rows:
        scope_label = f" for {agent_id}" if agent_id else ""
        when = "with refresh_on set" if include_future else "due for refresh"
        return f"Refresh queue{scope_label}: (empty — no memories {when})"

    scope_label = f" for {agent_id}" if agent_id else ""
    lines = [f"Refresh queue{scope_label} ({len(rows)} item{'s' if len(rows) != 1 else ''}):"]
    for row in rows:
        title = (row["title"] or "")[:60]
        reason = row["refresh_reason"] or "(no reason)"
        lines.append(
            f"  [{row['id'][:8]}] {row['type']:<12} due={row['refresh_on']} "
            f"reason={reason} title={title}"
        )
    return "\n".join(lines)

async def conversation_start_impl(title, agent_id="", model_id="", tags=""):
    cid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    metadata = json.dumps({"tags": [t.strip() for t in tags.split(",") if t.strip()]}) if tags else "{}"
    with _db() as db:
        db.execute(
            "INSERT INTO memory_items (id, type, title, agent_id, model_id, metadata_json, created_at) VALUES (?, 'conversation', ?, ?, ?, ?, ?)",
            (cid, title, agent_id, model_id, metadata, now)
        )
    return f"Conversation started: {cid}"

async def conversation_append_impl(conversation_id, role, content, agent_id="", model_id="", embed=True):
    with _db() as db:
        exists = db.execute(
            "SELECT id FROM memory_items WHERE id = ? AND type = 'conversation' AND is_deleted = 0",
            (conversation_id,)
        ).fetchone()
    if not exists:
        return f"Error: conversation {conversation_id} not found"
    mid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        db.execute(
            "INSERT INTO memory_items (id, type, title, content, agent_id, model_id, created_at) VALUES (?, 'message', ?, ?, ?, ?, ?)",
            (mid, role, content, agent_id, model_id, now)
        )
        db.execute("INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?, ?, ?, 'message', ?)",
                   (str(uuid.uuid4()), conversation_id, mid, now))
    if embed:
        vec, m = await _embed(content)
        if vec:
            with _db() as db:
                db.execute("INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at) VALUES (?,?,?,?,?,?)",
                          (str(uuid.uuid4()), mid, _pack(vec), m, len(vec), now))
    return f"Appended: {mid}"


def observation_enqueue_impl(
    conversation_id: str,
    user_id: str = "",
) -> str:
    """Phase D Mastra Observer enqueue.

    Inserts a row into observation_queue keyed on conversation_id. The
    drainer (bin/run_observer.py) pops these rows, builds the multi-turn
    JSON block from memory_items rows belonging to the conversation, calls
    the Observer SLM, and writes type='observation' rows back.

    UNIQUE on conversation_id means re-enqueue is a no-op — useful for
    idempotent close-of-conversation triggers.

    Returns "Enqueued" / "Already queued" / error string.
    """
    if not conversation_id:
        return "Error: conversation_id required"
    try:
        with _db() as db:
            db.execute(
                "INSERT OR IGNORE INTO observation_queue (conversation_id, user_id) "
                "VALUES (?, ?)",
                (conversation_id, user_id or None),
            )
            db.commit()
            row = db.execute(
                "SELECT id, attempts FROM observation_queue WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row:
            return f"Enqueued (queue_id={row[0]}, attempts={row[1]})"
        return "Error: enqueue failed silently"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


def reflector_enqueue_impl(
    conversation_id: str,
    user_id: str = "",
    obs_count: int | None = None,
) -> str:
    """Phase D Reflector enqueue.

    Triggered when the per-(user_id, conversation_id) observation count
    exceeds M3_REFLECTOR_THRESHOLD (default 50, env-tunable). Drained by
    bin/run_reflector.py.
    """
    if not conversation_id:
        return "Error: conversation_id required"
    try:
        with _db() as db:
            db.execute(
                "INSERT OR IGNORE INTO reflector_queue "
                "(conversation_id, user_id, obs_count_at_enqueue) VALUES (?, ?, ?)",
                (conversation_id, user_id or None, obs_count),
            )
            db.commit()
            row = db.execute(
                "SELECT id, attempts FROM reflector_queue "
                "WHERE conversation_id=? AND COALESCE(user_id,'')=COALESCE(?,'')",
                (conversation_id, user_id or None),
            ).fetchone()
        if row:
            return f"Enqueued (queue_id={row[0]}, attempts={row[1]})"
        return "Error: enqueue failed silently"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


VALID_SCOPES = {"user", "session", "agent", "org"}

async def memory_write_impl(type, content, title="", metadata="{}", agent_id="", model_id="", change_agent="", importance=0.5, source="agent", embed=True, user_id="", scope="agent", valid_from="", valid_to="", auto_classify=False, conversation_id="", refresh_on="", refresh_reason="", variant=None, embed_text=None, fact_enricher: "Callable[[str], Awaitable[list[dict]]] | None" = None, fact_enricher_variant_allowlist: "set[str] | None" = None, entity_extractor: "Callable[[str], Awaitable[dict]] | None" = None, entity_extractor_variant_allowlist: "set[str] | None" = None):
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
                        sv = await asyncio.to_thread(
                            lambda s=sub: _direct_embedder.embed([s])[0]
                        )
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
    superseded_ids = []
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

# ── Task Orchestration: State Machine + Helper Functions ─────────────────────────

TASK_STATE_TRANSITIONS = {
    "pending":     {"in_progress", "blocked", "cancelled"},
    "in_progress": {"blocked", "completed", "failed", "cancelled"},
    "blocked":     {"in_progress", "cancelled"},
    "completed":   set(),
    "failed":      set(),
    "cancelled":   set(),
}
VALID_TASK_STATES = frozenset(TASK_STATE_TRANSITIONS.keys())
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})
VALID_AGENT_STATUSES = frozenset({"active", "idle", "offline"})

def _validate_task_transition(prev: str, new: str):
    """Validates task state transitions. Returns None if valid, error string if invalid."""
    if new not in VALID_TASK_STATES:
        return f"Error: invalid task state '{new}'. Valid: {', '.join(sorted(VALID_TASK_STATES))}"
    if prev == new:
        return None
    allowed = TASK_STATE_TRANSITIONS.get(prev, set())
    if new not in allowed:
        return (f"Error: cannot transition task from '{prev}' to '{new}'. "
                f"Allowed from '{prev}': {sorted(allowed) or '(terminal)'}")
    return None

def _agent_exists(agent_id: str) -> bool:
    """Checks if an agent is registered in the agents table."""
    with _db() as db:
        row = db.execute("SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        return row is not None

# ── Agent Registry (5 functions) ──────────────────────────────────────────────────

def agent_register_impl(agent_id: str, role: str, capabilities: list, metadata: dict) -> str:
    """Registers or updates an agent in the registry."""
    if not agent_id:
        return "Error: agent_id cannot be empty"

    now = datetime.now(timezone.utc).isoformat()
    caps_json = json.dumps(capabilities or [])
    meta_json = json.dumps(metadata or {})

    with _db() as db:
        db.execute(
            """INSERT INTO agents (agent_id, role, capabilities, metadata_json, status, last_seen, created_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)
               ON CONFLICT(agent_id) DO UPDATE SET
                 role=excluded.role,
                 capabilities=excluded.capabilities,
                 metadata_json=excluded.metadata_json,
                 status='active',
                 last_seen=excluded.last_seen""",
            (agent_id, role, caps_json, meta_json, now, now)
        )

    return f"Registered: {agent_id} (role={role}, status=active)" + _refresh_hint(agent_id)

def agent_heartbeat_impl(agent_id: str) -> str:
    """Updates agent's last_seen timestamp and status to active."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        cur = db.execute(
            "UPDATE agents SET last_seen = ?, status = 'active' WHERE agent_id = ?",
            (now, agent_id)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: agent '{agent_id}' not registered"

    return f"Heartbeat: {agent_id} (last_seen={now})"

def agent_list_impl(status: str = "", role: str = "") -> str:
    """Lists agents, optionally filtered by status and/or role."""
    where_clauses = []
    params = []

    if status:
        where_clauses.append("status = ?")
        params.append(status)
    if role:
        where_clauses.append("role = ?")
        params.append(role)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with _db() as db:
        rows = db.execute(
            f"SELECT agent_id, role, status, last_seen FROM agents {where} ORDER BY last_seen DESC",
            params
        ).fetchall()

    if not rows:
        return "(no agents)"

    lines = [f"Agents ({len(rows)}):"]
    for row in rows:
        lines.append(f"  [{row['agent_id']}] role={row['role']} status={row['status']} last_seen={row['last_seen']}")

    return "\n".join(lines)

def agent_get_impl(agent_id: str) -> str:
    """Retrieves detailed information about a single agent."""
    with _db() as db:
        row = db.execute(
            "SELECT * FROM agents WHERE agent_id = ?",
            (agent_id,)
        ).fetchone()

    if not row:
        return f"Error: agent '{agent_id}' not found"

    caps = json.loads(row["capabilities"] or "[]")
    meta = json.loads(row["metadata_json"] or "{}")

    lines = [
        f"Agent: {row['agent_id']}",
        f"  Role: {row['role']}",
        f"  Status: {row['status']}",
        f"  Capabilities: {caps}",
        f"  Metadata: {meta}",
        f"  Last Seen: {row['last_seen']}",
        f"  Created At: {row['created_at'] if 'created_at' in row.keys() else 'N/A'}",
    ]

    return "\n".join(lines)

def agent_offline_impl(agent_id: str) -> str:
    """Marks an agent as offline."""
    with _db() as db:
        cur = db.execute(
            "UPDATE agents SET status = 'offline' WHERE agent_id = ?",
            (agent_id,)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: agent '{agent_id}' not found"

    return f"Agent {agent_id} marked offline" + _refresh_hint(agent_id)

# ── Notifications (4 functions) ───────────────────────────────────────────────────

def notify_impl(agent_id: str, kind: str, payload: dict = None) -> str:
    """Sends a notification to an agent."""
    now = datetime.now(timezone.utc).isoformat()
    payload_json = json.dumps(payload or {})

    with _db() as db:
        db.execute(
            "INSERT INTO notifications (agent_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (agent_id, kind, payload_json, now)
        )
        # Get the ID of the newly inserted row
        new_id = db.execute(
            "SELECT last_insert_rowid() as id"
        ).fetchone()["id"]

    return f"Notified {agent_id}: {kind} (id={new_id})"

def notifications_poll_impl(agent_id: str, unread_only: bool = True, limit: int = 20) -> str:
    """Retrieves notifications for an agent."""
    where_clause = "WHERE agent_id = ?"
    params = [agent_id]

    if unread_only:
        where_clause += " AND read_at IS NULL"

    with _db() as db:
        rows = db.execute(
            f"SELECT id, kind, payload_json, created_at, read_at FROM notifications {where_clause} ORDER BY created_at DESC LIMIT ?",
            params + [limit]
        ).fetchall()

    if not rows:
        return f"Notifications for {agent_id}: (empty)"

    read_type = "unread" if unread_only else "total"
    lines = [f"Notifications for {agent_id} ({len(rows)} {read_type}):"]
    for row in rows:
        lines.append(f"  [{row['id']}] kind={row['kind']} payload={row['payload_json']} created={row['created_at']}")

    return "\n".join(lines)

def notifications_ack_impl(notification_id: int) -> str:
    """Marks a notification as read."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        cur = db.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
            (now, notification_id)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: notification {notification_id} not found or already acked"

    return f"Acked notification {notification_id}"

def notifications_ack_all_impl(agent_id: str) -> str:
    """Marks all unread notifications for an agent as read."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        cur = db.execute(
            "UPDATE notifications SET read_at = ? WHERE agent_id = ? AND read_at IS NULL",
            (now, agent_id)
        )
        rowcount = cur.rowcount

    return f"Acked {rowcount} notifications for {agent_id}"

# ── Tasks (7 functions) ───────────────────────────────────────────────────────────

def task_create_impl(title: str, created_by: str, description: str = "", owner_agent: str = "", parent_task_id: str = "", metadata: dict = None) -> str:
    """Creates a new task."""
    if not title:
        return "Error: title cannot be empty"
    if not created_by:
        return "Error: created_by cannot be empty"

    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        db.execute(
            """INSERT INTO tasks (id, title, description, state, created_by, owner_agent, parent_task_id, metadata_json, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
            (task_id, title, description, created_by, owner_agent or None, parent_task_id or None, json.dumps(metadata or {}), now, now)
        )

    return f"Task created: {task_id}"

def task_assign_impl(task_id: str, owner_agent: str) -> str:
    """Assigns a task to an agent and transitions state to in_progress."""
    with _db() as db:
        row = db.execute(
            "SELECT state, created_by FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (task_id,)
        ).fetchone()

    if not row:
        return f"Error: task '{task_id}' not found"

    prev_state = row["state"]
    err = _validate_task_transition(prev_state, "in_progress")
    if err:
        return err

    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        db.execute(
            "UPDATE tasks SET owner_agent = ?, state = 'in_progress', updated_at = ? WHERE id = ?",
            (owner_agent, now, task_id)
        )

    _record_history(task_id, "task_state", prev_state, "in_progress", "state", owner_agent)

    # Fire-and-forget notification
    try:
        notify_impl(owner_agent, "task_assigned", {"task_id": task_id})
    except Exception as e:
        logger.warning(f"task_assigned notify failed for {owner_agent}: {e}")

    return f"Task {task_id} assigned to {owner_agent} (state=in_progress)"

def task_update_impl(task_id: str, state: str = "", description: str = "", metadata: dict = None, actor: str = "") -> str:
    """Updates a task's state, description, and/or metadata."""
    with _db() as db:
        row = db.execute(
            "SELECT state, description, metadata_json, created_by FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (task_id,)
        ).fetchone()

    if not row:
        return f"Error: task '{task_id}' not found"

    prev_state = row["state"]
    new_state = state if state else prev_state

    if state:
        err = _validate_task_transition(prev_state, new_state)
        if err:
            return err

    now = datetime.now(timezone.utc).isoformat()
    updates = ["updated_at = ?"]
    params = [now]

    if state:
        updates.append("state = ?")
        params.append(new_state)

    if description:
        updates.append("description = ?")
        params.append(description)

    if metadata is not None:
        updates.append("metadata_json = ?")
        params.append(json.dumps(metadata))

    if new_state in TERMINAL_TASK_STATES:
        updates.append("completed_at = ?")
        params.append(now)

    params.append(task_id)

    with _db() as db:
        db.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
            params
        )

    if state and prev_state != new_state:
        _record_history(task_id, "task_state", prev_state, new_state, "state", actor or "system")

        # Fire-and-forget notification if completed
        if new_state == "completed":
            try:
                notify_impl(row["created_by"], "task_completed", {"task_id": task_id})
            except Exception as e:
                logger.warning(f"task_completed notify failed for {row['created_by']}: {e}")

        return f"Task {task_id} updated: state={new_state}"
    else:
        return f"Task {task_id} updated"

def task_set_result_impl(task_id: str, result_memory_id: str) -> str:
    """Sets the result memory for a task (without changing state)."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        cur = db.execute(
            "UPDATE tasks SET result_memory_id = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (result_memory_id, now, task_id)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: task '{task_id}' not found"

    return f"Task {task_id} result={result_memory_id}"

def task_get_impl(task_id: str, include_deleted: bool = False) -> str:
    """Retrieves detailed information about a task."""
    sql = "SELECT * FROM tasks WHERE id = ?"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    with _db() as db:
        row = db.execute(sql, (task_id,)).fetchone()

    if not row:
        return f"Error: task '{task_id}' not found"

    lines = [
        f"Task: {row['id']}",
        f"  Title: {row['title']}",
        f"  Description: {row['description']}",
        f"  State: {row['state']}",
        f"  Created By: {row['created_by']}",
        f"  Owner: {row['owner_agent'] or '(unassigned)'}",
        f"  Parent Task: {row['parent_task_id'] or '(none)'}",
        f"  Result Memory: {row['result_memory_id'] or '(none)'}",
        f"  Created At: {row['created_at']}",
        f"  Updated At: {row['updated_at']}",
        f"  Completed At: {row['completed_at'] or '(not completed)'}",
        f"  Deleted At: {row['deleted_at'] or '(not deleted)'}",
    ]

    return "\n".join(lines)

def task_delete_impl(task_id: str, hard: bool = False, actor: str = "") -> str:
    """Delete a task.

    Soft-delete (default): sets `deleted_at` so pg_sync propagates the
    tombstone to the warehouse and peers on the next run. The row stays
    in local SQLite and is filtered out of reads.

    Hard-delete: only allowed once the row is already tombstoned. Removes
    the row from local SQLite. Note that sync is UPSERT-only, so a hard
    delete on one peer does NOT remove the row on other peers — they
    converge via the soft-delete tombstone.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        row = db.execute(
            "SELECT state, deleted_at FROM tasks WHERE id = ?",
            (task_id,)
        ).fetchone()

        if not row:
            return f"Error: task '{task_id}' not found"

        if hard:
            if row["deleted_at"] is None:
                return (
                    f"Error: task '{task_id}' must be soft-deleted before hard-delete. "
                    "Call task_delete with hard=False first."
                )
            db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            _record_history(task_id, "task_deleted", row["state"], "hard_deleted", "deleted_at", actor or "system")
            return f"Task {task_id} hard-deleted"

        if row["deleted_at"] is not None:
            return f"Task {task_id} already soft-deleted at {row['deleted_at']}"

        db.execute(
            "UPDATE tasks SET deleted_at = ?, updated_at = ? WHERE id = ?",
            (now, now, task_id)
        )

    _record_history(task_id, "task_deleted", row["state"], "soft_deleted", "deleted_at", actor or "system")
    return f"Task {task_id} soft-deleted (tombstone will sync on next pg_sync run)"

def task_list_impl(owner_agent: str = "", state: str = "", parent_task_id: str = "", limit: int = 20, include_deleted: bool = False) -> str:
    """Lists tasks, optionally filtered by owner, state, and/or parent."""
    where_clauses = []
    params = []

    if not include_deleted:
        where_clauses.append("deleted_at IS NULL")
    if owner_agent:
        where_clauses.append("owner_agent = ?")
        params.append(owner_agent)
    if state:
        where_clauses.append("state = ?")
        params.append(state)
    if parent_task_id:
        where_clauses.append("parent_task_id = ?")
        params.append(parent_task_id)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with _db() as db:
        rows = db.execute(
            f"SELECT id, title, state, owner_agent FROM tasks {where} ORDER BY updated_at DESC LIMIT ?",
            params + [limit]
        ).fetchall()

    if not rows:
        return "Tasks: (empty)"

    lines = [f"Tasks ({len(rows)}):"]
    for row in rows:
        lines.append(f"  [{row['id'][:8]}] {row['title']} state={row['state']} owner={row['owner_agent']}")

    return "\n".join(lines)

def task_tree_impl(root_task_id: str, max_depth: int = 10) -> str:
    """Displays a task and its subtasks in a tree structure. Tombstoned tasks are hidden."""
    max_depth = max(1, min(max_depth, 20))

    with _db() as db:
        row = db.execute(
            "SELECT id, title, state, owner_agent FROM tasks WHERE id = ? AND deleted_at IS NULL",
            (root_task_id,)
        ).fetchone()

        if not row:
            return f"Error: task '{root_task_id}' not found"

        rows = db.execute(
            """WITH RECURSIVE subtree(id, title, state, owner_agent, parent_task_id, depth) AS (
                SELECT id, title, state, owner_agent, parent_task_id, 0
                  FROM tasks WHERE id = ? AND deleted_at IS NULL
                UNION ALL
                SELECT t.id, t.title, t.state, t.owner_agent, t.parent_task_id, s.depth + 1
                  FROM tasks t JOIN subtree s ON t.parent_task_id = s.id
                 WHERE s.depth + 1 <= ? AND t.deleted_at IS NULL
            )
            SELECT * FROM subtree ORDER BY depth, id""",
            (root_task_id, max_depth)
        ).fetchall()

    if not rows:
        return f"Error: task '{root_task_id}' not found"

    lines = [f"Task tree from {root_task_id[:8]} (max_depth={max_depth}):"]
    for row in rows:
        indent = "  " * row["depth"]
        owner_str = row["owner_agent"] or "-"
        lines.append(f"{indent}[{row['id'][:8]}] {row['title']} ({row['state']}, owner={owner_str})")

    return "\n".join(lines)


# ── Fact enrichment queue drain (Phase 5) ────────────────────────────────────
def _select_pending_fact_enrichment(db, limit: int | None = None, allowed_variants: list[str] | None = None) -> list[tuple[str, str]]:
    """Returns [(memory_id, content), ...] eligible for enrichment.

    Eligibility: type != fact_enriched, variant IS NULL (or in allowed_variants),
    no existing fact_enriched child via references edge, attempts < max_attempts.

    When allowed_variants is provided, loosen the variant filter from strict NULL
    to (variant IS NULL OR variant IN (...)).
    """
    # Build the variant clause
    if allowed_variants:
        variant_clause = f"AND (mi.variant IS NULL OR mi.variant IN ({','.join(['?'] * len(allowed_variants))}))"
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
        WHERE q.attempts < ?
    )
    SELECT id, content FROM queued
    UNION
    SELECT id, content FROM eligible
    WHERE id NOT IN (SELECT memory_id FROM fact_enrichment_queue)
    ORDER BY id
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    params = variant_params + [FACT_ENRICH_MAX_ATTEMPTS]
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


# ── Entity extraction queue drain (Phase 5) ──────────────────────────────────



# ── Entity extractor health (Phase E1) ───────────────────────────────────────


# ── Entity search and retrieval (Phase 7) ─────────────────────────────────────


