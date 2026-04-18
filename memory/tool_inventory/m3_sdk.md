---
tool: bin/m3_sdk.py
sha1: acac09c9d759
mtime_utc: 2026-04-18T16:11:38.046416+00:00
generated_utc: 2026-04-18T16:33:21.643094+00:00
private: false
---

# bin/m3_sdk.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

_(no conventional entry point detected)_

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `DB_POOL_SIZE`
- `DB_POOL_TIMEOUT`
- `M3_MEMORY_ROOT`
- `PG_URL`

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `chatlog_config`

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 123)
- `httpx.AsyncClient()` (line 126)

**sqlite**

- `sqlite3.connect()  → `self.db_path`` (line 68)


## Notable external imports

- `atexit`
- `dotenv (load_dotenv)`
- `httpx`
- `platform`
- `psycopg2`

## File dependencies (repo paths referenced)

- `memory/agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
