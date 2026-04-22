---
tool: bin/pg_sync.py
sha1: d635f68b735e
mtime_utc: 2026-04-22T01:03:02.049263+00:00
generated_utc: 2026-04-22T01:22:54.658851+00:00
private: false
---

# bin/pg_sync.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `def main()` (line 707)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `PG_URL`

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (M3Context)`
- `m3_sdk (resolve_venv_python)`
- `migrate_memory`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `target.db_path`` (line 720)


## Notable external imports

- `psycopg2 (Binary)`
- `psycopg2.extras (execute_values)`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
