---
tool: bin/m3_sdk.py
sha1: 63d0764eb65b
mtime_utc: 2026-04-21T20:54:48.397156+00:00
generated_utc: 2026-04-21T21:22:27.086868+00:00
private: false
---

# bin/m3_sdk.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

_(no conventional entry point detected)_

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None |  | str |  |

## Environment variables read

- `DB_POOL_SIZE`
- `DB_POOL_TIMEOUT`
- `M3_DATABASE`
- `M3_MEMORY_ROOT`
- `PG_URL`

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `chatlog_config`

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 210)
- `httpx.AsyncClient()` (line 213)

**sqlite**

- `sqlite3.connect()  → `self.db_path`` (line 155)


## Notable external imports

- `atexit`
- `contextvars`
- `dotenv (load_dotenv)`
- `httpx`
- `psycopg2`

## File dependencies (repo paths referenced)

- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
