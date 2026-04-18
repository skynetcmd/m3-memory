---
tool: bin/pg_sync.py
sha1: 96af2ded0020
mtime_utc: 2026-04-18T03:18:29.585754+00:00
generated_utc: 2026-04-18T16:33:21.739134+00:00
private: false
---

# bin/pg_sync.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `def main()` (line 677)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `PG_URL`

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (M3Context)`
- `m3_sdk (resolve_venv_python)`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `psycopg2 (Binary)`
- `psycopg2.extras (execute_values)`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
