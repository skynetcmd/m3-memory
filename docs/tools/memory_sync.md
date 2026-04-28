---
tool: bin/memory_sync.py
sha1: 39f35e44ad08
mtime_utc: 2026-04-28T15:21:54.074361+00:00
generated_utc: 2026-04-28T15:48:17.472837+00:00
private: false
---

# bin/memory_sync.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

_(no conventional entry point detected)_

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `M3_CHROMA_SYNC_QUEUE_MAX`
- `M3_CHROMA_SYNC_QUEUE_SKIP_AT`
- `M3_CHROMA_SYNC_QUEUE_WARN`

## Calls INTO this repo (intra-repo imports)

- `memory_core (ctx, _pack, _unpack, CHROMA_BASE_URL, CHROMA_COLLECTION, CHROMA_V2_PREFIX, CHROMA_CONTENT_MAX, EMBED_DIM)`
- `migrate_memory`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 18)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
