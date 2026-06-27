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
import os  # noqa: F401 — re-export
import uuid
from datetime import datetime, timezone

# Legacy back-compat: `_lru_cache` was imported inline in the FTS block
# (used internally by `_compile_fts_query`'s @decorator). Preserved as a
# re-export because the API snapshot captured it as a public symbol —
# something external imports it. Pure alias for functools.lru_cache.
from functools import lru_cache as _lru_cache  # noqa: F401 — re-export
from typing import Any, Awaitable, Callable  # noqa: F401 (used in annotations)

from embedding_utils import infer_change_agent as _infer_change_agent_util  # noqa: F401 — re-export
from llm_failover import get_best_llm
from m3_sdk import M3Context, resolve_db_path

# ── Dynamic Plugin Architecture (Milestone 1) ─────────────────────────────────
# Lazily import submodules only when their attributes are accessed, minimizing
# startup overhead and context initialization footprint.

_LAZY_IMPORTS = {
    # memory.chroma
    "_CHROMA_COLLECTION_ID_CACHE": "memory.chroma",
    "_query_chroma": "memory.chroma",
    "_queue_chroma": "memory.chroma",
    "_resolve_chroma_collection_id": "memory.chroma",
    "_mc_chroma": "memory",

    # memory.config
    "_DEFAULT_VALID_ENTITY_PREDICATES": "memory.config",
    "_DEFAULT_VALID_ENTITY_TYPES": "memory.config",
    "_EMBED_MODEL_OVERRIDE": "memory.config",
    "_EMBED_URL_OVERRIDE": "memory.config",
    "_ENV_ENTITY_VOCAB_YAML": "memory.config",
    "_OXIDATION_DISABLED": "memory.config",
    "ARCHIVE_DB_PATH": "memory.config",
    "AUTO_RELATED_LINK": "memory.config",
    "AUTO_RELATED_LINK_SCOPE_BY_VARIANT": "memory.config",
    "BASE_DIR": "memory.config",
    "CHROMA_BASE_URL": "memory.config",
    "CHROMA_COLLECTION": "memory.config",
    "CHROMA_COLLECTIONS": "memory.config",
    "CHROMA_CONNECT_T": "memory.config",
    "CHROMA_CONTENT_MAX": "memory.config",
    "CHROMA_PULL_PAGE_SIZE": "memory.config",
    "CHROMA_READ_T": "memory.config",
    "CHROMA_V2_PREFIX": "memory.config",
    "CONTRADICTION_THRESHOLD": "memory.config",
    "CONTRADICTION_TITLE_GATE": "memory.config",
    "CONTRADICTION_TYPE_EXCLUSIONS": "memory.config",
    "DB_PATH": "memory.config",
    "DEDUP_LIMIT": "memory.config",
    "DEDUP_THRESHOLD": "memory.config",
    "DEFAULT_CHANGE_AGENT": "memory.config",
    "DEFAULT_ENTITY_VOCAB_YAML": "memory.config",
    "DEFAULT_RERANK_MODEL": "memory.config",
    "ELBOW_ABS_THRESHOLD": "memory.config",
    "ELBOW_MIN_INPUT": "memory.config",
    "ELBOW_MIN_RETURN": "memory.config",
    "EMBED_BREAKER_CLOUD_RESET_SECS": "memory.config",
    "EMBED_BREAKER_CLOUD_THRESHOLD": "memory.config",
    "EMBED_DIM": "memory.config",
    "EMBED_MODEL": "memory.config",
    "EMBED_TIMEOUT_READ": "memory.config",
    "ENABLE_ENTITY_GRAPH": "memory.config",
    "ENABLE_FACT_ENRICHED": "memory.config",
    "ENTITY_EXTRACT_CONCURRENCY": "memory.config",
    "ENTITY_EXTRACT_MAX_ATTEMPTS": "memory.config",
    "ENTITY_RESOLVE_COSINE_MIN": "memory.config",
    "ENTITY_RESOLVE_FUZZY_MIN": "memory.config",
    "ENTITY_SEED_STOPLIST": "memory.config",
    "EXPANSION_DISPLACEMENT_MARGIN": "memory.config",
    "EXPANSION_PROTECTED_RANKS": "memory.config",
    "FACT_ENRICH_CONCURRENCY": "memory.config",
    "FACT_ENRICH_MAX_ATTEMPTS": "memory.config",
    "FEDERATION_LOW_SCORE_THRESHOLD": "memory.config",
    "IMPORTANCE_WEIGHT": "memory.config",
    "INGEST_EVENT_ROWS": "memory.config",
    "INGEST_GIST_MIN_TURNS": "memory.config",
    "INGEST_GIST_ROWS": "memory.config",
    "INGEST_GIST_STRIDE": "memory.config",
    "INGEST_WINDOW_CHUNKS": "memory.config",
    "INGEST_WINDOW_SIZE": "memory.config",
    "INTENT_ROUTING": "memory.config",
    "INTENT_USER_FACT_BOOST": "memory.config",
    "LLM_TIMEOUT": "memory.config",
    "M3_ALLOW_CLOUD_FALLBACK": "memory.config",
    "M3_CLOUD_AUTH_TOKEN_KEYRING": "memory.config",
    "M3_CLOUD_ENCLAVE_URL": "memory.config",
    "M3_CLOUD_MINIMIZATION_LEVEL": "memory.config",
    "ORIGIN_DEVICE": "memory.config",
    "QUERY_TYPE_ROUTING": "memory.config",
    "SEARCH_ROW_CAP": "memory.config",
    "SHORT_TURN_THRESHOLD": "memory.config",
    "SPEAKER_IN_TITLE": "memory.config",
    "SUPERSEDES_PENALTY": "memory.config",
    "TITLE_MATCH_BOOST": "memory.config",
    "m3_core_rs": "memory.config",
    "_mc_config": "memory",

    # memory.db
    "_ACCESS_FLUSH_INTERVAL": "memory.db",
    "_ENTITY_COUNT_QUERY": "memory.db",
    "_GATE_CACHE": "memory.db",
    "_GATE_CACHE_TTL": "memory.db",
    "_OBS_COUNT_QUERY": "memory.db",
    "_access_flusher_task": "memory.db",
    "_access_lock": "memory.db",
    "_access_pending": "memory.db",
    "_access_stamp_flusher": "memory.db",
    "_backfill_change_agent": "memory.db",
    "_conn": "memory.db",
    "_db": "memory.db",
    "_enqueue_access_stamps": "memory.db",
    "_enqueue_write": "memory.db",
    "_ensure_sync_tables": "memory.db",
    "_gate_active": "memory.db",
    "_gate_count_query": "memory.db",
    "_init_lock": "memory.db",
    "_initialized": "memory.db",
    "_initialized_dbs": "memory.db",
    "_lazy_init": "memory.db",
    "_local": "memory.db",
    "_record_history": "memory.db",
    "memory_history_impl": "memory.db",
    "_mc_db": "memory",

    # memory.doctor
    "memory_doctor_impl": "memory.doctor",
    "memory_doctor_fix_impl": "memory.doctor",

    # memory.embed
    "_CPU_FALLBACK_BREAKER": "memory.embed",
    "_DENSE_ERR_RE": "memory.embed",
    "_EMBED_BACKEND_STATS": "memory.embed",
    "_EMBED_BACKEND_STATS_LOCK": "memory.embed",
    "_EMBED_BULK_SEM": "memory.embed",
    "_EMBED_CLIENT": "memory.embed",
    "_EMBED_CLIENT_LOCK": "memory.embed",
    "_EMBED_CLIENT_LOOP_ID": "memory.embed",
    "_EMBED_DIM_VALIDATED": "memory.embed",
    "_EMBED_FALLBACK_URL": "memory.embed",
    "_EMBED_GGUF_MODEL_TAG": "memory.embed",
    "_EMBED_GGUF_PATH": "memory.embed",
    "_EMBED_HTTP_KEEPALIVE_EXPIRY": "memory.embed",
    "_EMBED_HTTP_MAX_CONNS": "memory.embed",
    "_EMBED_HTTP_MAX_KEEPALIVE": "memory.embed",
    "_EMBED_SEM": "memory.embed",
    "_EMBEDDED_BREAKER": "memory.embed",
    "_ENTITY_NAME_EMBED_CACHE": "memory.embed",
    "_PRIMARY_BREAKER": "memory.embed",
    "DENSE_MIN_SUB_CHARS": "memory.embed",
    "DENSE_TARGET_TOKENS": "memory.embed",
    "DENSE_TOKEN_OVERLAP": "memory.embed",
    "EMBED_BULK_CHUNK": "memory.embed",
    "EMBED_BULK_CONCURRENCY": "memory.embed",
    "ENTITY_NAME_EMBED_CACHE_MAX": "memory.embed",
    "MAX_CHARS_PER_CHUNK": "memory.embed",
    "MIN_OVERLAP_CHARS": "memory.embed",
    "STRIDE_CHARS": "memory.embed",
    "EmbeddedBackendError": "memory.embed",
    "EmbedError": "memory.embed",
    "EmbedFallbackError": "memory.embed",
    "EmbedPrimaryError": "memory.embed",
    "EmbedSemaphoreTimeout": "memory.embed",
    "_augment_embed_text_with_anchors": "memory.embed",
    "_chunk_for_sliding_window": "memory.embed",
    "_content_hash": "memory.embed",
    "_embed": "memory.embed",
    "_embed_canonical_cached": "memory.embed",
    "_embed_many": "memory.embed",
    "_embedded_embed_checked": "memory.embed",
    "_embedded_embedder": "memory.embed",
    "_embedded_label": "memory.embed",
    "_get_embed_client": "memory.embed",
    "_get_embedded_embedder": "memory.embed",
    "_record_embed_backend": "memory.embed",
    "_shared_embed_client": "memory.embed",
    "_subdivide_dense_chunk": "memory.embed",
    "embedder_status_impl": "memory.embed",
    "get_embed_backend_stats": "memory.embed",
    "get_embed_breaker_state": "memory.embed",
    "reset_embed_backend_stats": "memory.embed",
    "reset_embed_breakers": "memory.embed",
    "set_embed_override": "memory.embed",
    "_mc_embed": "memory",

    # memory.enrich
    "_select_pending_fact_enrichment": "memory.enrich",
    "_try_enrich_or_enqueue": "memory.enrich",
    "enrich_pending_impl": "memory.enrich",

    # memory.entity
    "_ENTITY_EXTRACT_SEM": "memory.entity",
    "_PENDING_ENTITY_TASKS": "memory.entity",
    "_TOKEN_PUNCT_RE": "memory.entity",
    "VALID_ENTITY_PREDICATES": "memory.entity",
    "VALID_ENTITY_TYPES": "memory.entity",
    "_create_entity": "memory.entity",
    "_enqueue_entity_extraction": "memory.entity",
    "_link_entity_relationship": "memory.entity",
    "_link_memory_to_entity": "memory.entity",
    "_resolve_entity": "memory.entity",
    "_resolve_entity_async": "memory.entity",
    "_run_entity_extractor": "memory.entity",
    "_select_pending_entity_extraction": "memory.entity",
    "_token_jaccard": "memory.entity",
    "_try_extract_or_enqueue": "memory.entity",
    "entity_extractor_health": "memory.entity",
    "entity_get_impl": "memory.entity",
    "entity_search_impl": "memory.entity",
    "extract_pending_impl": "memory.entity",
    "load_entity_vocab": "memory.entity",
    "_mc_entity": "memory",

    # memory.entity_count
    "count_entities_impl": "memory.entity_count",
    "count_mentions_impl": "memory.entity_count",
    "list_mentions_impl": "memory.entity_count",

    # memory.fts
    "_EVENT_DATE_HINT": "memory.fts",
    "_EVENT_PROPER_NOUN": "memory.fts",
    "_EVENT_SENT_SPLIT": "memory.fts",
    "_EVENT_VERB_LIST": "memory.fts",
    "_EVENT_VERB_RE": "memory.fts",
    "_FTS_OPERATORS": "memory.fts",
    "_SEARCHABLE_PUNCT": "memory.fts",
    "_TOKEN_SPLIT": "memory.fts",
    "_augment_title_with_role": "memory.fts",
    "_compile_fts_query": "memory.fts",
    "_query_title_overlap": "memory.fts",
    "_query_title_token_set": "memory.fts",
    "_sanitize_for_searchable": "memory.fts",
    "_sanitize_fts": "memory.fts",
    "_title_overlap_from_qset": "memory.fts",

    # memory.graph
    "_entity_graph_neighbor_ids": "memory.graph",
    "_graph_neighbor_ids": "memory.graph",
    "_score_extra_rows": "memory.graph",
    "_session_neighbor_ids": "memory.graph",
    "memory_graph_impl": "memory.graph",

    # memory.history
    "compute_bitemporal_diffs_impl": "memory.history",
    "get_bitemporal_timeline_impl": "memory.history",

    # memory.search
    "_DATE_MONTHS": "memory.search",
    "_DATE_RE_ISO": "memory.search",
    "_DATE_RE_LONG": "memory.search",
    "_ENTITY_MENTION_PATTERNS": "memory.search",
    "_ENTITY_MENTION_RE": "memory.search",
    "_RERANKER_MODEL": "memory.search",
    "_RERANKER_MODEL_NAME": "memory.search",
    "_TEMPORAL_QUERY_RE": "memory.search",
    "_TEMPORAL_ROUTER_PATTERNS": "memory.search",
    "_TEMPORAL_ROUTER_RE": "memory.search",
    "_UNSET": "memory.search",
    "_apply_auto_layer": "memory.search",
    "_apply_recency_bonus": "memory.search",
    "_apply_rerank": "memory.search",
    "_apply_sharp_trim": "memory.search",
    "_apply_temporal_boost": "memory.search",
    "_cosine_batch_packed": "memory.search",
    "_enforce_expansion_displacement_guard": "memory.search",
    "_extract_caller_overrides": "memory.search",
    "_get_reranker": "memory.search",
    "_hybrid_score_batch": "memory.search",
    "_maybe_expand_routed": "memory.search",
    "_maybe_route_query": "memory.search",
    "_pull_predecessor_turns": "memory.search",
    "_recency_bonus_ranks": "memory.search",
    "_trim_by_elbow": "memory.search",
    "is_temporal_query": "memory.search",
    "memory_search_impl": "memory.search",
    "memory_search_multi_db_impl": "memory.search",
    "memory_search_routed_impl": "memory.search",
    "memory_search_scored_impl": "memory.search",
    "_mc_search": "memory",

    # memory.util
    "_batch_cosine": "memory.util",
    "_cosine": "memory.util",
    "_sha256_hex": "memory.util",

    # memory.write
    "_check_contradictions": "memory.write",
    "memory_link_impl": "memory.write",
    "memory_supersede_impl": "memory.write",
    "memory_write_batch_impl": "memory.write",
    "memory_write_bulk_impl": "memory.write",
    "memory_write_from_file_impl": "memory.write",
    "memory_write_impl": "memory.write",

    # lazy semaphores / variables
    "_FACT_ENRICH_SEM": "lazy_sem",
}

def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        mod_name = _LAZY_IMPORTS[name]
        import importlib
        if mod_name == "memory":
            real_sub = name.replace("_mc_", "")
            val = importlib.import_module(f"memory.{real_sub}")
        elif name == "_sha256_hex":
            val = getattr(importlib.import_module(mod_name), "sha256_hex")
        elif name == "_FACT_ENRICH_SEM":
            import asyncio

            from memory.config import FACT_ENRICH_CONCURRENCY
            val = asyncio.Semaphore(FACT_ENRICH_CONCURRENCY)
        else:
            val = getattr(importlib.import_module(mod_name), name)
        globals()[name] = val
        return val
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + list(_LAZY_IMPORTS.keys()))





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
from embedding_utils import HAS_NUMPY as _HAS_NUMPY
from embedding_utils import (
    pack as _pack,
)
from embedding_utils import (
    unpack as _unpack,  # noqa: F401 — re-export for memory_sync / memory_maintenance
)

if _HAS_NUMPY:
    pass  # type: ignore

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
# _EVENT_PROPER_NOUN moved to bin/memory/search.py in Phase 4.B sub-2
# (its hot reader is _maybe_route_query). _extract_event_sentences below
# imports it via the shim re-export at the top of this file.




# Temporal query regexes (_TEMPORAL_QUERY_RE, _DATE_RE_ISO, _DATE_RE_LONG,
# _DATE_MONTHS), _pull_predecessor_turns, and _maybe_route_query moved to
# bin/memory/search.py in Phase 4.B sub-2. Re-exported via the shim at the top.








# _POISON_PATTERNS + _check_content_safety moved to bin/memory/util.py as the
# single source of truth. Re-exported here so external callers importing from
# memory_core keep working. Do NOT redefine the patterns here — fix the regex
# bug in util.py once and both call sites benefit (CodeQL alert #29 history).
from memory.util import _POISON_PATTERNS, _check_content_safety  # noqa: F401

# DEFAULT_CHANGE_AGENT, CHROMA_*, FEDERATION_LOW_SCORE_THRESHOLD moved to
# bin/memory/config.py in Phase 1. Re-exported via the shim at the top.

# _local / _init_lock / _initialized moved to bin/memory/db.py in Phase 2.B.
# Re-exported via the shim at the top.
# _EMBED_SEM and _EMBED_DIM_VALIDATED moved to bin/memory/embed.py in Phase 3.
# Enrichment-pipeline semaphores stay here until enrich.py is extracted.

_COST_COUNTERS = {"embed_calls": 0, "embed_tokens_est": 0, "search_calls": 0, "write_calls": 0}
_PENDING_FACT_TASKS: set[asyncio.Task] = set()



# ── Ingest-time LLM enrichment (opt-in) ──────────────────────────────────────
# Gated by env vars so behavior matches today's default (no extra LLM calls at
# write time) unless explicitly enabled. Intended for production callers that
# pass blank titles / want entity-tagged metadata without running heuristics
# themselves. All helpers fail-open: on any error, they return the untouched
# input so ingest never fails because LLM enrichment did.


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
    import sys
    mc = sys.modules[__name__]
    return getattr(mc, "_gate_active")("M3_PREFER_OBSERVATIONS", getattr(mc, "_OBS_COUNT_QUERY"), threshold=100)

def _two_stage_observations_gate() -> bool:
    # Paired with PREFER_OBSERVATIONS: same trigger.
    import sys
    mc = sys.modules[__name__]
    return getattr(mc, "_gate_active")("M3_TWO_STAGE_OBSERVATIONS", getattr(mc, "_OBS_COUNT_QUERY"), threshold=100)

def _enable_entity_graph_gate() -> bool:
    import sys
    mc = sys.modules[__name__]
    return getattr(mc, "_gate_active")("M3_ENABLE_ENTITY_GRAPH", getattr(mc, "_ENTITY_COUNT_QUERY"), threshold=1)


_AUTO_ENTITIES_CACHE: dict[str, list[str]] = {}





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
# Legacy back-compat: `_ThreadLock` was imported inline in the original
# embed-stats block. The API snapshot captured it as a public symbol;
# preserve as an explicit re-export. Pure alias for threading.Lock.
from threading import Lock as _ThreadLock  # noqa: F401

# The cascade itself (_embed, _embed_many, EMBED_BULK_CHUNK,
# EMBED_BULK_CONCURRENCY, _EMBED_BULK_SEM, _EMBED_SEM, _EMBED_DIM_VALIDATED)
# moved to bin/memory/embed.py in Phase 3. Re-exported via the shim at the top.




# _queue_chroma moved to bin/memory/chroma.py in Phase 4.A.
# Re-exported via the shim at the top.



# ── Fact enrichment pipeline (Phase 4-5) ──────────────────────────────────────








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
    try:
        from audit_trail import write_audit_entry
        write_audit_entry(
            action="memory_delete",
            target_id=id,
            metadata={"hard": hard}
        )
    except Exception as e:
        logger.warning(f"Failed to write audit trail entry for delete: {e}")
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

    try:
        from audit_trail import write_audit_entry
        write_audit_entry(
            action="memory_delete_bulk",
            target_id="bulk",
            metadata={
                "ids": id_list,
                "hard": hard,
                "succeeded": succeeded,
                "not_found": not_found
            }
        )
    except Exception as e:
        logger.warning(f"Failed to write audit trail entry for bulk delete: {e}")

    return {
        "succeeded": succeeded,
        "not_found": not_found,
        "mode": "hard" if hard else "soft",
    }


_MEMORY_UPDATE_BULK_CHUNK = 500


def memory_update_bulk_impl(updates):
    """Apply a list of metadata-only updates in one transaction per chunk.

    Designed for curation passes that retroactively set retention, importance,
    or supersession metadata on many rows. Does NOT support re-embedding —
    re-embed is a separate concern (use `re_embed_all.py` or per-id
    `memory_update(reembed=True)`).

    Each update entry may set any subset of:
        content, title, importance, metadata, refresh_on, refresh_reason,
        conversation_id

    Field semantics match `memory_update_impl` exactly:
      - empty string for a field → no change to that column
      - `"clear"` for refresh_on/refresh_reason/conversation_id → set to NULL
      - importance < 0 → no change
      - metadata: dict is JSON-encoded; empty string → no change

    Per-id history rows are written for content/title/refresh_on/refresh_reason/
    conversation_id changes, matching `memory_update_impl`'s audit behavior.
    importance/metadata changes are silent (no history row) — same as singleton.

    Args:
        updates: iterable of dicts. Each dict MUST include `id` plus at least
                 one field to change.

    Returns:
        dict: {
          "succeeded": [id, ...],          # updates that applied (≥1 column changed)
          "not_found": [id, ...],
          "no_change": [id, ...],          # row exists but every field was empty/sentinel
          "total":     <int total input rows after dedupe on id>,
        }
    """
    # Dedupe on id while preserving order (last entry per id wins).
    by_id: dict[str, dict] = {}
    for u in updates or []:
        if not isinstance(u, dict):
            continue
        mid = u.get("id")
        if not mid:
            continue
        by_id[mid] = u

    if not by_id:
        return {"succeeded": [], "not_found": [], "no_change": [], "total": 0}

    succeeded: list[str] = []
    not_found: list[str] = []
    no_change: list[str] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    id_list = list(by_id.keys())

    with _db() as db:
        for start in range(0, len(id_list), _MEMORY_UPDATE_BULK_CHUNK):
            chunk_ids = id_list[start : start + _MEMORY_UPDATE_BULK_CHUNK]
            placeholders = ",".join("?" * len(chunk_ids))
            rows = db.execute(
                f"SELECT id, content, title, refresh_on, refresh_reason, conversation_id "
                f"FROM memory_items WHERE id IN ({placeholders})",
                chunk_ids,
            ).fetchall()
            existing_by_id = {row["id"]: row for row in rows}

            for mid in chunk_ids:
                if mid not in existing_by_id:
                    not_found.append(mid)
                    continue
                old = existing_by_id[mid]
                u = by_id[mid]
                changed_any = False

                content = u.get("content", "")
                if content:
                    _record_history(mid, "update", old["content"], content, "content", db=db)
                    db.execute("UPDATE memory_items SET content = ? WHERE id = ?", (content, mid))
                    changed_any = True

                title = u.get("title", "")
                if title:
                    _record_history(mid, "update", old["title"], title, "title", db=db)
                    db.execute("UPDATE memory_items SET title = ? WHERE id = ?", (title, mid))
                    changed_any = True

                importance = u.get("importance", -1.0)
                try:
                    importance = float(importance)
                except (TypeError, ValueError):
                    importance = -1.0
                if importance >= 0:
                    db.execute("UPDATE memory_items SET importance = ? WHERE id = ?", (importance, mid))
                    changed_any = True

                metadata = u.get("metadata", "")
                if isinstance(metadata, dict):
                    metadata = json.dumps(metadata)
                if metadata:
                    db.execute("UPDATE memory_items SET metadata_json = ? WHERE id = ?", (metadata, mid))
                    changed_any = True

                refresh_on = u.get("refresh_on", "")
                if refresh_on:
                    new_val = None if refresh_on == "clear" else refresh_on
                    _record_history(mid, "update", old["refresh_on"], new_val, "refresh_on", db=db)
                    db.execute("UPDATE memory_items SET refresh_on = ? WHERE id = ?", (new_val, mid))
                    changed_any = True

                refresh_reason = u.get("refresh_reason", "")
                if refresh_reason:
                    new_val = None if refresh_reason == "clear" else refresh_reason
                    _record_history(mid, "update", old["refresh_reason"], new_val, "refresh_reason", db=db)
                    db.execute("UPDATE memory_items SET refresh_reason = ? WHERE id = ?", (new_val, mid))
                    changed_any = True

                conversation_id = u.get("conversation_id", "")
                if conversation_id:
                    new_val = None if conversation_id == "clear" else conversation_id
                    _record_history(mid, "update", old["conversation_id"], new_val, "conversation_id", db=db)
                    db.execute("UPDATE memory_items SET conversation_id = ? WHERE id = ?", (new_val, mid))
                    changed_any = True

                if changed_any:
                    db.execute("UPDATE memory_items SET updated_at = ? WHERE id = ?", (now_iso, mid))
                    succeeded.append(mid)
                else:
                    no_change.append(mid)

    return {
        "succeeded": succeeded,
        "not_found": not_found,
        "no_change": no_change,
        "total": len(by_id),
    }


VALID_RELATIONSHIP_TYPES = {"related", "supports", "contradicts", "extends", "supersedes", "references", "message", "consolidates", "handoff", "precedes", "follows"}

_MEMORY_LINK_BULK_CHUNK = 500


def memory_link_bulk_impl(links, relationship_type: str = "related"):
    """Create many memory_relationships rows in one transaction per chunk.

    Behaviorally identical to looping `memory_link_impl` over each pair, but
    with the same two wins as `memory_delete_bulk_impl`:

    1. **Batched SQL.** Validate existence of all referenced memory_ids in one
       `SELECT ... WHERE id IN (?,?,...)` per chunk, then INSERT every valid
       link in one prepared-statement loop inside the same connection. For
       100 links this collapses ~400 statements + 100 connection-opens into
       ~2 statements + 1 connection.
    2. **Structured result.** Returns
       `{created, skipped_missing, skipped_duplicate, total}` instead of N
       text lines, so curation callers can act on the result without parsing.

    Args:
        links: iterable of either:
                 - {"from_id": str, "to_id": str, "relationship_type"?: str}  (preferred)
                 - (from_id, to_id) tuples (uses outer relationship_type)
        relationship_type: default link type if a dict entry omits it (or for
            tuple entries). Must be in VALID_RELATIONSHIP_TYPES.

    Returns:
        dict: {
          "created":           [{from_id, to_id, relationship_type, id}, ...],
          "skipped_missing":   [{from_id, to_id, relationship_type, missing: [id, ...]}, ...],
          "skipped_duplicate": [{from_id, to_id, relationship_type, existing_id}, ...],
          "total":             <int total input rows after dedupe>,
        }
    """
    # Normalize input: every entry becomes a (from_id, to_id, rel_type) triple.
    normalized: list[tuple[str, str, str]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for entry in links or []:
        if isinstance(entry, dict):
            f = entry.get("from_id", "")
            t = entry.get("to_id", "")
            r = entry.get("relationship_type") or relationship_type
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            f, t = entry[0], entry[1]
            r = entry[2] if len(entry) > 2 else relationship_type
        else:
            continue
        if not f or not t:
            continue
        if r not in VALID_RELATIONSHIP_TYPES:
            # Treat invalid relationship_type as a missing-target equivalent —
            # surface it as skipped, don't silently drop or raise mid-batch.
            normalized.append((f, t, r))
            continue
        key = (f, t, r)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        normalized.append(key)

    if not normalized:
        return {"created": [], "skipped_missing": [], "skipped_duplicate": [], "total": 0}

    created: list[dict] = []
    skipped_missing: list[dict] = []
    skipped_duplicate: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        for start in range(0, len(normalized), _MEMORY_LINK_BULK_CHUNK):
            chunk = normalized[start : start + _MEMORY_LINK_BULK_CHUNK]

            # Collect every distinct memory_id referenced in this chunk to do
            # a single existence check.
            all_ids = list({mid for triple in chunk for mid in (triple[0], triple[1])})
            placeholders = ",".join("?" * len(all_ids))
            existing_rows = db.execute(
                f"SELECT id FROM memory_items WHERE id IN ({placeholders})",
                all_ids,
            ).fetchall()
            existing_ids = {row["id"] for row in existing_rows}

            for from_id, to_id, rel_type in chunk:
                if rel_type not in VALID_RELATIONSHIP_TYPES:
                    skipped_missing.append({
                        "from_id": from_id, "to_id": to_id,
                        "relationship_type": rel_type,
                        "missing": [],  # invalid rel_type, not missing ids
                        "reason": f"invalid relationship_type '{rel_type}'",
                    })
                    continue
                missing = [m for m in (from_id, to_id) if m not in existing_ids]
                if missing:
                    skipped_missing.append({
                        "from_id": from_id, "to_id": to_id,
                        "relationship_type": rel_type,
                        "missing": missing,
                    })
                    continue
                # Duplicate check — same (from, to, type) already linked?
                existing = db.execute(
                    "SELECT id FROM memory_relationships "
                    "WHERE from_id=? AND to_id=? AND relationship_type=?",
                    (from_id, to_id, rel_type),
                ).fetchone()
                if existing:
                    skipped_duplicate.append({
                        "from_id": from_id, "to_id": to_id,
                        "relationship_type": rel_type,
                        "existing_id": existing["id"],
                    })
                    continue
                rid = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO memory_relationships "
                    "(id, from_id, to_id, relationship_type, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (rid, from_id, to_id, rel_type, now_iso),
                )
                created.append({
                    "id": rid, "from_id": from_id, "to_id": to_id,
                    "relationship_type": rel_type,
                })

    return {
        "created": created,
        "skipped_missing": skipped_missing,
        "skipped_duplicate": skipped_duplicate,
        "total": len(normalized),
    }


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




# ── Entity extraction queue drain (Phase 5) ──────────────────────────────────



# ── Entity extractor health (Phase E1) ───────────────────────────────────────


# ── Entity search and retrieval (Phase 7) ─────────────────────────────────────


