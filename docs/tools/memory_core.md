---
tool: bin/memory_core.py
sha1: 3affdf175914
mtime_utc: 2026-04-22T01:03:02.045652+00:00
generated_utc: 2026-04-22T01:22:54.613901+00:00
private: false
---

# bin/memory_core.py

## Purpose

Core memory primitives: single + bulk write, search, enrichment, emitters.

Not a CLI — imported by MCP server, bench drivers, and import scripts.

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

## Entry points

_(no conventional entry point detected)_

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `CHROMA_BASE_URL`
- `CONTRADICTION_THRESHOLD`
- `DEDUP_LIMIT`
- `DEDUP_THRESHOLD`
- `EMBED_BULK_CHUNK`
- `EMBED_BULK_CONCURRENCY`
- `EMBED_DIM`
- `EMBED_MODEL`
- `LLM_TIMEOUT`
- `M3_IMPORTANCE_WEIGHT`
- `M3_INGEST_EVENT_ROWS`
- `M3_INGEST_GIST_MIN_TURNS`
- `M3_INGEST_GIST_ROWS`
- `M3_INGEST_GIST_STRIDE`
- `M3_INGEST_WINDOW_CHUNKS`
- `M3_INGEST_WINDOW_SIZE`
- `M3_INTENT_ROUTING`
- `M3_INTENT_USER_FACT_BOOST`
- `M3_QUERY_TYPE_ROUTING`
- `M3_SHORT_TURN_THRESHOLD`
- `M3_SPEAKER_IN_TITLE`
- `M3_TITLE_MATCH_BOOST`
- `ORIGIN_DEVICE`
- `SEARCH_ROW_CAP`

## Calls INTO this repo (intra-repo imports)

- `embedding_utils (batch_cosine)`
- `embedding_utils (cosine)`
- `embedding_utils (infer_change_agent)`
- `embedding_utils (pack)`
- `embedding_utils (unpack)`
- `llm_failover (get_best_embed, get_best_llm, get_smallest_llm)`
- `m3_sdk (M3Context, resolve_db_path)`
- `temporal_utils (extract_referenced_dates, has_temporal_cues)`

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `[sys.executable, migration_script, 'up', '--yes']`` (line 827)


## Notable external imports

- `httpx`
- `platform`

## File dependencies (repo paths referenced)

- `agent_memory.db`
- `agent_memory_archive.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
