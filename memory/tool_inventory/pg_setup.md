---
tool: bin/pg_setup.py
sha1: 475b5b42496d
mtime_utc: 2026-04-06T00:25:00.986104+00:00
generated_utc: 2026-04-17T04:17:01.754214+00:00
private: false
---

# bin/pg_setup.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `def main()` (line 98)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `PG_URL`

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `psycopg2`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
