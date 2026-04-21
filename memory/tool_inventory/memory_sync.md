---
tool: bin/memory_sync.py
sha1: 0c5e147c130f
mtime_utc: 2026-04-21T20:02:02.936766+00:00
generated_utc: 2026-04-21T21:26:01.920096+00:00
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

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `memory_core (ctx, _pack, _unpack, CHROMA_BASE_URL, CHROMA_COLLECTION, CHROMA_V2_PREFIX, CHROMA_CONTENT_MAX, EMBED_DIM)`
- `migrate_memory`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 17)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
