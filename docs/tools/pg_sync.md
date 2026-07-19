---
tool: bin/pg_sync.py
sha1: 8c29cfe2f228
mtime_utc: 2026-07-19T06:18:23.186355+00:00
generated_utc: 2026-07-19T20:02:06.274392+00:00
private: false
---

# bin/pg_sync.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 1231)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--db` | Path to the SQLite database to sync. Default: the SDK-resolved canonical path (M3_DATABASE env / engine root / populated legacy store) — never a hardcoded repo-relative guess. | None |  | str |  |
| `--manifest` | Path to sync manifest YAML. Inferred from --db basename if omitted. | None |  | str |  |
| `--dry-run` | Print what would sync without touching either database. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_halt (_pid_is_alive)`
- `m3_sdk (M3Context, resolve_db_path)`
- `m3_sdk (resolve_cdw_pg_dsn, resolve_venv_python)`
- `migrate_memory`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 1358)
- `sqlite3.connect()  → `db_path`` (line 1374)
- `sqlite3.connect()  → `target.db_path`` (line 1310)


---

## Notable external imports

- `psycopg2 (Binary)`
- `psycopg2.extras (execute_values)`
- `yaml`

---

## File dependencies (repo paths referenced)

- `Infer manifest path from db basename: config/sync_manifests/<stem>.yaml`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
