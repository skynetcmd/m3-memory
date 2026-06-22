---
tool: bin/chatlog_status.py
sha1: 7c7c484086e9
mtime_utc: 2026-06-13T11:52:16.549729+00:00
generated_utc: 2026-06-19T20:00:03.798500+00:00
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

- `def main()` (line 746)
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

- `subprocess.run()  → `cmd`` (line 482)

**sqlite**

- `sqlite3.connect()  → `chatlog_db`` (line 80)
- `sqlite3.connect()  → `db`` (line 150)
- `sqlite3.connect()  → `files_db`` (line 112)
- `sqlite3.connect()  → `main_db`` (line 309)
- `sqlite3.connect()  → `main_db`` (line 326)
- `sqlite3.connect()  → `main_db`` (line 66)


---

## Notable external imports

- `memory (doctor)`
- `memory.config (FILES_DB_PATH)`
- `msvcrt`
- `select`
- `termios`
- `tty`

---

## File dependencies (repo paths referenced)

- `files_database.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
