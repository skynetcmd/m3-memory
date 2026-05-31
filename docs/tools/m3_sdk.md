---
tool: bin/m3_sdk.py
sha1: 8f9c407c10e5
mtime_utc: 2026-05-31T16:08:17.248463+00:00
generated_utc: 2026-05-31T18:42:52.826381+00:00
private: false
---

# bin/m3_sdk.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | m3_sdk itself has no CLI; this row documents the add_database_arg(parser) helper shape. | str | Every CLI that calls add_database_arg(parser) gains this flag with identical semantics. |

---

## Environment variables read

- `DB_POOL_SIZE`
- `DB_POOL_TIMEOUT`
- `LM_READ_TIMEOUT`
- `LM_STUDIO_BASE`
- `M3_CONFIG_ROOT`
- `M3_CONTEXT_CACHE_SIZE`
- `M3_CORE_RS_DISABLE`
- `M3_DATABASE`
- `M3_ENGINE_ROOT`
- `M3_GOVERNOR_INITIAL_THRESHOLD`
- `M3_GOVERNOR_LIMIT_THRESHOLD`
- `M3_MEMORY_ROOT`
- `PG_URL`
- `PYTHONUTF8`
- `_M3_UTF8_REEXEC`

---

## Calls INTO this repo (intra-repo imports)

- `audit_trail (log_event)`
- `auth_utils (get_api_key)`
- `auth_utils (get_salt_path)`
- `chatlog_config`
- `crypto_provider (provider)`
- `sqlite_pragmas (apply_pragmas, profile_for_db)`
- `thermal_utils (get_thermal_status)`

---

## Calls OUT (external side-channels)

**subprocess**

- `os.execv()  → `sys.executable`` (line 173)

**http**

- `httpx.AsyncClient()` (line 627)
- `httpx.AsyncClient()` (line 630)

**sqlite**

- `sqlite3.connect()  → `path`` (line 290)
- `sqlite3.connect()  → `self.db_path`` (line 466)


---

## Notable external imports

- `atexit`
- `contextvars`
- `dotenv (load_dotenv)`
- `httpx`
- `m3_core_rs (format_log)`
- `psutil`
- `psycopg2`

---

## File dependencies (repo paths referenced)

- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
