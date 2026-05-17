---
tool: bin/memory_core.py
sha1: 73bdd19ced56
mtime_utc: 2026-05-17T15:46:47.318937+00:00
generated_utc: 2026-05-17T15:50:17.756950+00:00
private: false
---

# bin/memory_core.py

## Purpose

Core memory primitives: single + bulk write, search, enrichment, emitters.

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

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `crypto_provider (get_sha256)`
- `embedding_utils (HAS_NUMPY)`
- `embedding_utils (batch_cosine)`
- `embedding_utils (infer_change_agent)`
- `embedding_utils (pack)`
- `embedding_utils (unpack)`
- `embedding_utils (unpack_many)`
- `llm_failover (clear_failover_caches)`
- `llm_failover (get_best_embed, get_best_llm, get_smallest_llm)`
- `m3_sdk (M3Context, resolve_db_path)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `httpx`
- `memory (chroma)`
- `memory (config)`
- `memory (db)`
- `memory (embed)`
- `memory (entity)`
- `memory (search)`
- `memory.chroma (_CHROMA_COLLECTION_ID_CACHE, _queue_chroma, _resolve_chroma_collection_id, _query_chroma)`
- `memory.config (_OXIDATION_DISABLED, m3_core_rs, _EMBED_URL_OVERRIDE, _EMBED_MODEL_OVERRIDE, BASE_DIR, DB_PATH, ARCHIVE_DB_PATH, EMBED_MODEL, EMBED_DIM, EMBED_TIMEOUT_READ, ORIGIN_DEVICE, DEDUP_LIMIT, DEDUP_THRESHOLD, CONTRADICTION_THRESHOLD, SUPERSEDES_PENALTY, CONTRADICTION_TITLE_GATE, CONTRADICTION_TYPE_EXCLUSIONS, AUTO_RELATED_LINK, AUTO_RELATED_LINK_SCOPE_BY_VARIANT, SEARCH_ROW_CAP, LLM_TIMEOUT, SPEAKER_IN_TITLE, SHORT_TURN_THRESHOLD, TITLE_MATCH_BOOST, IMPORTANCE_WEIGHT, ELBOW_MIN_INPUT, ELBOW_MIN_RETURN, ELBOW_ABS_THRESHOLD, EXPANSION_DISPLACEMENT_MARGIN, EXPANSION_PROTECTED_RANKS, ENTITY_SEED_STOPLIST, INGEST_WINDOW_CHUNKS, INGEST_GIST_ROWS, INGEST_EVENT_ROWS, QUERY_TYPE_ROUTING, INTENT_ROUTING, INTENT_USER_FACT_BOOST, INGEST_WINDOW_SIZE, INGEST_GIST_MIN_TURNS, INGEST_GIST_STRIDE, ENABLE_FACT_ENRICHED, FACT_ENRICH_CONCURRENCY, FACT_ENRICH_MAX_ATTEMPTS, ENABLE_ENTITY_GRAPH, ENTITY_EXTRACT_CONCURRENCY, ENTITY_EXTRACT_MAX_ATTEMPTS, ENTITY_RESOLVE_FUZZY_MIN, ENTITY_RESOLVE_COSINE_MIN, _DEFAULT_VALID_ENTITY_TYPES, _DEFAULT_VALID_ENTITY_PREDICATES, DEFAULT_ENTITY_VOCAB_YAML, _ENV_ENTITY_VOCAB_YAML, DEFAULT_RERANK_MODEL, DEFAULT_CHANGE_AGENT, CHROMA_BASE_URL, CHROMA_COLLECTION, CHROMA_COLLECTIONS, CHROMA_V2_PREFIX, CHROMA_CONNECT_T, CHROMA_READ_T, CHROMA_PULL_PAGE_SIZE, CHROMA_CONTENT_MAX, FEDERATION_LOW_SCORE_THRESHOLD)`
- `memory.db (_local, _init_lock, _initialized, _initialized_dbs, _GATE_CACHE, _GATE_CACHE_TTL, _OBS_COUNT_QUERY, _ENTITY_COUNT_QUERY, _ACCESS_FLUSH_INTERVAL, _access_pending, _access_flusher_task, _access_lock, _db, _conn, _ensure_sync_tables, _backfill_change_agent, _lazy_init, _record_history, memory_history_impl, _gate_count_query, _gate_active, _access_stamp_flusher, _enqueue_access_stamps)`
- `memory.embed (_EMBED_GGUF_PATH, _EMBED_GGUF_MODEL_TAG, _embedded_embedder, _embedded_embed_checked, _get_embedded_embedder, MAX_CHARS_PER_CHUNK, MIN_OVERLAP_CHARS, STRIDE_CHARS, _chunk_for_sliding_window, DENSE_TARGET_TOKENS, DENSE_TOKEN_OVERLAP, DENSE_MIN_SUB_CHARS, _DENSE_ERR_RE, _subdivide_dense_chunk, _augment_embed_text_with_anchors, _content_hash, _EMBED_HTTP_MAX_CONNS, _EMBED_HTTP_MAX_KEEPALIVE, _EMBED_HTTP_KEEPALIVE_EXPIRY, _EMBED_CLIENT, _EMBED_CLIENT_LOOP_ID, _EMBED_CLIENT_LOCK, _shared_embed_client, _get_embed_client, _EMBED_FALLBACK_URL, _EMBED_BACKEND_STATS, _EMBED_BACKEND_STATS_LOCK, _record_embed_backend, get_embed_backend_stats, reset_embed_backend_stats, _embedded_label, set_embed_override, _EMBED_SEM, _EMBED_DIM_VALIDATED, EMBED_BULK_CHUNK, EMBED_BULK_CONCURRENCY, _EMBED_BULK_SEM, _embed, _embed_many, _ENTITY_NAME_EMBED_CACHE, ENTITY_NAME_EMBED_CACHE_MAX, _embed_canonical_cached, embedder_status_impl, EmbedError, EmbeddedBackendError, EmbedFallbackError, EmbedPrimaryError, EmbedSemaphoreTimeout, _EMBEDDED_BREAKER, _CPU_FALLBACK_BREAKER, _PRIMARY_BREAKER, get_embed_breaker_state, reset_embed_breakers)`
- `memory.enrich (_CLASSIFY_CACHE, _AUTO_TITLE_CACHE, _ingest_llm_enabled, _auto_classify, _maybe_auto_title, _maybe_auto_entities, _try_enrich_or_enqueue, _enqueue_fact_enrichment, _run_fact_enricher, _write_fact_rows, _select_pending_fact_enrichment, enrich_pending_impl)`
- `memory.entity (load_entity_vocab, VALID_ENTITY_TYPES, VALID_ENTITY_PREDICATES, _TOKEN_PUNCT_RE, _token_jaccard, _ENTITY_EXTRACT_SEM, _PENDING_ENTITY_TASKS, _resolve_entity, _resolve_entity_async, _create_entity, _link_memory_to_entity, _link_entity_relationship, _enqueue_entity_extraction, _run_entity_extractor, _try_extract_or_enqueue, _select_pending_entity_extraction, extract_pending_impl, entity_extractor_health, entity_search_impl, entity_get_impl)`
- `memory.fts (_FTS_OPERATORS, _sanitize_fts, _SEARCHABLE_PUNCT, _sanitize_for_searchable, _compile_fts_query, _TOKEN_SPLIT, _augment_title_with_role, _query_title_token_set, _title_overlap_from_qset, _query_title_overlap)`
- `memory.graph (_graph_neighbor_ids, _session_neighbor_ids, _entity_graph_neighbor_ids, _score_extra_rows, memory_graph_impl)`
- `memory.search (_cosine_batch_packed, _hybrid_score_batch, _recency_bonus_ranks, _EVENT_PROPER_NOUN, _TEMPORAL_QUERY_RE, _DATE_RE_ISO, _DATE_RE_LONG, _DATE_MONTHS, _pull_predecessor_turns, _maybe_route_query, _apply_recency_bonus, _trim_by_elbow, _apply_temporal_boost, _RERANKER_MODEL, _RERANKER_MODEL_NAME, _get_reranker, _enforce_expansion_displacement_guard, _apply_rerank, _TEMPORAL_ROUTER_PATTERNS, _TEMPORAL_ROUTER_RE, _ENTITY_MENTION_PATTERNS, _ENTITY_MENTION_RE, _UNSET, _extract_caller_overrides, _apply_auto_layer, _apply_sharp_trim, is_temporal_query, memory_search_scored_impl, memory_search_routed_impl, _maybe_expand_routed, memory_search_multi_db_impl, memory_search_impl)`
- `memory.util (_batch_cosine)`
- `memory.util (sha256_hex, _cosine)`
- `memory.write (memory_write_impl, memory_write_bulk_impl, _check_contradictions)`
- `numpy`
- `platform`
- `yaml`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
