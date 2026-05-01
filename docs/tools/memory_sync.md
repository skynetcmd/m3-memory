---
tool: bin/memory_sync.py
sha1: a8828e3c1ee4
mtime_utc: 2026-05-01T09:16:01.584274+00:00
generated_utc: 2026-05-01T13:05:26.995706+00:00
private: false
---

# bin/memory_sync.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `M3_CHROMA_SYNC_QUEUE_MAX`
- `M3_CHROMA_SYNC_QUEUE_SKIP_AT`
- `M3_CHROMA_SYNC_QUEUE_WARN`

---

## Calls INTO this repo (intra-repo imports)

- `memory_core (CHROMA_BASE_URL, CHROMA_COLLECTION, CHROMA_CONTENT_MAX, CHROMA_V2_PREFIX, EMBED_DIM, _pack, _unpack, ctx)`
- `migrate_memory`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 26)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
