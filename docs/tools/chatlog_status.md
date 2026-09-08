---
tool: bin/chatlog_status.py
sha1: 705f0731dcea
mtime_utc: 2026-09-08T23:52:09.421909+00:00
generated_utc: 2026-09-08T23:52:42.539066+00:00
private: false
---

# bin/chatlog_status.py

## Purpose

chatlog_status.py — single-call summary of the chat log subsystem state.

Exports:
- chatlog_status_impl() -> str : returns JSON summary
- CLI: python bin/chatlog_status.py [--json]

Returns row counts from SQLite; everything else from state file + config.
Cold call <50ms (no full table scans).

---

## Entry points

- `def main()` (line 1088)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--json` | Output JSON format | `False` |  | store_true |  |
| `--live` | Run live status monitor | `False` |  | store_true |  |
| `-i`, `--interval` | Refresh interval for live monitor in seconds (default: 5.0) | `5.0` |  | float |  |

---

## Environment variables read

- `M3_FILES_DB_PATH`

---

## Calls INTO this repo (intra-repo imports)

- `chatlog_config`
- `m3_sdk (get_m3_root)`
- `m3_sdk (resolve_db_path)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `cmd`` (line 823)

**sqlite**

- `sqlite3.connect()  → `chatlog_db`` (line 136)
- `sqlite3.connect()  → `files_db`` (line 204)
- `sqlite3.connect()  → `main_db`` (line 122)
- `sqlite3.connect()  → `uri`` (line 302)


---

## Notable external imports

- `doctor (environment_probe)`
- `files_memory.config (files_table)`
- `files_memory.db (_db)`
- `files_memory.db (_is_postgres)`
- `m3_core.paths (seam_backend)`
- `memory (doctor)`
- `memory.backends (resolve_backend_name)`
- `memory.backends.sqlite_backend (SqliteDialect)`
- `memory.config (FILES_DB_PATH)`
- `msvcrt`
- `select`
- `termios`
- `tty`

---

## File dependencies (repo paths referenced)

- `.db`
- `files_database.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
