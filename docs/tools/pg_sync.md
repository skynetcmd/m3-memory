---
tool: bin/pg_sync.py
sha1: 95d8113142a9
mtime_utc: 2026-04-26T08:00:05.082139+00:00
generated_utc: 2026-04-26T10:12:32.174045+00:00
private: false
---

# bin/pg_sync.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `def main()` (line 1053)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--db` | Path to the SQLite database to sync (default: memory/agent_memory.db) | `os.path.join(BASE_DIR, 'memory', 'agent_memory.db')` |  | str |  |
| `--manifest` | Path to sync manifest YAML. Inferred from --db basename if omitted. | None |  | str |  |
| `--dry-run` | Print what would sync without touching either database. | `False` |  | store_true |  |

## Environment variables read

- `PG_URL`

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (M3Context)`
- `m3_sdk (resolve_venv_python)`
- `migrate_memory`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 1171)
- `sqlite3.connect()  → `db_path`` (line 1187)
- `sqlite3.connect()  → `target.db_path`` (line 1124)


## Notable external imports

- `psycopg2 (Binary)`
- `psycopg2.extras (execute_values)`
- `yaml`

## File dependencies (repo paths referenced)

- `Infer manifest path from db basename: config/sync_manifests/<stem>.yaml`
- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
