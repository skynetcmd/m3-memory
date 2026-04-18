---
tool: bin/memory_core.py
sha1: 145273f21c6f
mtime_utc: 2026-04-18T15:35:57.277842+00:00
generated_utc: 2026-04-18T16:33:21.708862+00:00
private: false
---

# bin/memory_core.py

## Purpose

_(no module docstring — update the source file.)_

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
- `M3_QUERY_TYPE_ROUTING`
- `M3_SHORT_TURN_THRESHOLD`
- `M3_SPEAKER_IN_TITLE`
- `M3_TITLE_MATCH_BOOST`
- `ORIGIN_DEVICE`
- `SEARCH_ROW_CAP`

## Calls INTO this repo (intra-repo imports)

- `embedding_utils (cosine)`
- `embedding_utils (pack, unpack, batch_cosine, infer_change_agent)`
- `llm_failover (get_best_embed, get_best_llm, get_smallest_llm)`
- `m3_sdk (M3Context)`

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `[sys.executable, migration_script]`` (line 674)


## Notable external imports

- `httpx`
- `platform`

## File dependencies (repo paths referenced)

- `agent_memory.db`
- `agent_memory_archive.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
