---
tool: bin/memory_core.py
sha1: 17c5e2c3482e
mtime_utc: 2026-05-31T16:08:17.251062+00:00
generated_utc: 2026-05-31T18:42:52.886545+00:00
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

- `audit_trail (write_audit_entry)`
- `embedding_utils (HAS_NUMPY)`
- `embedding_utils (pack)`
- `embedding_utils (unpack)`
- `llm_failover (clear_failover_caches)`
- `llm_failover (get_best_llm)`
- `m3_sdk (M3Context, resolve_db_path)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `memory (chroma)`
- `memory (config)`
- `memory (db)`
- `memory (embed)`
- `memory (entity)`
- `memory (search)`
- `memory.chroma (_CHROMA_COLLECTION_ID_CACHE, _query_chroma, _queue_chroma, _resolve_chroma_collection_id)`
- `memory.config (_DEFAULT_VALID_ENTITY_PREDICATES, _DEFAULT_VALID_ENTITY_TYPES, _EMBED_MODEL_OVERRIDE, _EMBED_URL_OVERRIDE, _ENV_ENTITY_VOCAB_YAML, _OXIDATION_DISABLED, ARCHIVE_DB_PATH, AUTO_RELATED_LINK, AUTO_RELATED_LINK_SCOPE_BY_VARIANT, BASE_DIR, CHROMA_BASE_URL, CHROMA_COLLECTION, CHROMA_COLLECTIONS, CHROMA_CONNECT_T, CHROMA_CONTENT_MAX, CHROMA_PULL_PAGE_SIZE, CHROMA_READ_T, CHROMA_V2_PREFIX, CONTRADICTION_THRESHOLD, CONTRADICTION_TITLE_GATE, CONTRADICTION_TYPE_EXCLUSIONS, DB_PATH, DEDUP_LIMIT, DEDUP_THRESHOLD, DEFAULT_CHANGE_AGENT, DEFAULT_ENTITY_VOCAB_YAML, DEFAULT_RERANK_MODEL, ELBOW_ABS_THRESHOLD, ELBOW_MIN_INPUT, ELBOW_MIN_RETURN, EMBED_BREAKER_CLOUD_RESET_SECS, EMBED_BREAKER_CLOUD_THRESHOLD, EMBED_DIM, EMBED_MODEL, EMBED_TIMEOUT_READ, ENABLE_ENTITY_GRAPH, ENABLE_FACT_ENRICHED, ENTITY_EXTRACT_CONCURRENCY, ENTITY_EXTRACT_MAX_ATTEMPTS, ENTITY_RESOLVE_COSINE_MIN, ENTITY_RESOLVE_FUZZY_MIN, ENTITY_SEED_STOPLIST, EXPANSION_DISPLACEMENT_MARGIN, EXPANSION_PROTECTED_RANKS, FACT_ENRICH_CONCURRENCY, FACT_ENRICH_MAX_ATTEMPTS, FEDERATION_LOW_SCORE_THRESHOLD, IMPORTANCE_WEIGHT, INGEST_EVENT_ROWS, INGEST_GIST_MIN_TURNS, INGEST_GIST_ROWS, INGEST_GIST_STRIDE, INGEST_WINDOW_CHUNKS, INGEST_WINDOW_SIZE, INTENT_ROUTING, INTENT_USER_FACT_BOOST, LLM_TIMEOUT, M3_ALLOW_CLOUD_FALLBACK, M3_CLOUD_AUTH_TOKEN_KEYRING, M3_CLOUD_ENCLAVE_URL, M3_CLOUD_MINIMIZATION_LEVEL, ORIGIN_DEVICE, QUERY_TYPE_ROUTING, SEARCH_ROW_CAP, SHORT_TURN_THRESHOLD, SPEAKER_IN_TITLE, SUPERSEDES_PENALTY, TITLE_MATCH_BOOST, m3_core_rs)`
- `memory.db (_ACCESS_FLUSH_INTERVAL, _ENTITY_COUNT_QUERY, _GATE_CACHE, _GATE_CACHE_TTL, _OBS_COUNT_QUERY, _access_flusher_task, _access_lock, _access_pending, _access_stamp_flusher, _backfill_change_agent, _conn, _db, _enqueue_access_stamps, _ensure_sync_tables, _gate_active, _gate_count_query, _init_lock, _initialized, _initialized_dbs, _lazy_init, _local, _record_history, memory_history_impl)`
- `memory.doctor (memory_doctor_impl)`
- `memory.embed (_CPU_FALLBACK_BREAKER, _DENSE_ERR_RE, _EMBED_BACKEND_STATS, _EMBED_BACKEND_STATS_LOCK, _EMBED_BULK_SEM, _EMBED_CLIENT, _EMBED_CLIENT_LOCK, _EMBED_CLIENT_LOOP_ID, _EMBED_DIM_VALIDATED, _EMBED_FALLBACK_URL, _EMBED_GGUF_MODEL_TAG, _EMBED_GGUF_PATH, _EMBED_HTTP_KEEPALIVE_EXPIRY, _EMBED_HTTP_MAX_CONNS, _EMBED_HTTP_MAX_KEEPALIVE, _EMBED_SEM, _EMBEDDED_BREAKER, _ENTITY_NAME_EMBED_CACHE, _PRIMARY_BREAKER, DENSE_MIN_SUB_CHARS, DENSE_TARGET_TOKENS, DENSE_TOKEN_OVERLAP, EMBED_BULK_CHUNK, EMBED_BULK_CONCURRENCY, ENTITY_NAME_EMBED_CACHE_MAX, MAX_CHARS_PER_CHUNK, MIN_OVERLAP_CHARS, STRIDE_CHARS, EmbeddedBackendError, EmbedError, EmbedFallbackError, EmbedPrimaryError, EmbedSemaphoreTimeout, _augment_embed_text_with_anchors, _chunk_for_sliding_window, _content_hash, _embed, _embed_canonical_cached, _embed_many, _embedded_embed_checked, _embedded_embedder, _embedded_label, _get_embed_client, _get_embedded_embedder, _record_embed_backend, _shared_embed_client, _subdivide_dense_chunk, embedder_status_impl, get_embed_backend_stats, get_embed_breaker_state, reset_embed_backend_stats, reset_embed_breakers, set_embed_override)`
- `memory.enrich (_select_pending_fact_enrichment, _try_enrich_or_enqueue, enrich_pending_impl)`
- `memory.entity (_ENTITY_EXTRACT_SEM, _PENDING_ENTITY_TASKS, _TOKEN_PUNCT_RE, VALID_ENTITY_PREDICATES, VALID_ENTITY_TYPES, _create_entity, _enqueue_entity_extraction, _link_entity_relationship, _link_memory_to_entity, _resolve_entity, _resolve_entity_async, _run_entity_extractor, _select_pending_entity_extraction, _token_jaccard, _try_extract_or_enqueue, entity_extractor_health, entity_get_impl, entity_search_impl, extract_pending_impl, load_entity_vocab)`
- `memory.entity_count (count_entities_impl, count_mentions_impl, list_mentions_impl)`
- `memory.fts (_EVENT_DATE_HINT, _EVENT_PROPER_NOUN, _EVENT_SENT_SPLIT, _EVENT_VERB_LIST, _EVENT_VERB_RE, _FTS_OPERATORS, _SEARCHABLE_PUNCT, _TOKEN_SPLIT, _augment_title_with_role, _compile_fts_query, _query_title_overlap, _query_title_token_set, _sanitize_for_searchable, _sanitize_fts, _title_overlap_from_qset)`
- `memory.graph (_entity_graph_neighbor_ids, _graph_neighbor_ids, _score_extra_rows, _session_neighbor_ids, memory_graph_impl)`
- `memory.search (_DATE_MONTHS, _DATE_RE_ISO, _DATE_RE_LONG, _ENTITY_MENTION_PATTERNS, _ENTITY_MENTION_RE, _RERANKER_MODEL, _RERANKER_MODEL_NAME, _TEMPORAL_QUERY_RE, _TEMPORAL_ROUTER_PATTERNS, _TEMPORAL_ROUTER_RE, _UNSET, _apply_auto_layer, _apply_recency_bonus, _apply_rerank, _apply_sharp_trim, _apply_temporal_boost, _cosine_batch_packed, _enforce_expansion_displacement_guard, _extract_caller_overrides, _get_reranker, _hybrid_score_batch, _maybe_expand_routed, _maybe_route_query, _pull_predecessor_turns, _recency_bonus_ranks, _trim_by_elbow, is_temporal_query, memory_search_impl, memory_search_multi_db_impl, memory_search_routed_impl, memory_search_scored_impl)`
- `memory.util (_POISON_PATTERNS, _check_content_safety)`
- `memory.util (_batch_cosine, _cosine)`
- `memory.util (sha256_hex)`
- `memory.write (_check_contradictions, memory_link_impl, memory_supersede_impl, memory_write_batch_impl, memory_write_bulk_impl, memory_write_from_file_impl, memory_write_impl)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
