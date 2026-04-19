---
tool: bin/migrate_memory.py
sha1: 4487fb528ab4
mtime_utc: 2026-04-19T02:44:47.983823+00:00
generated_utc: 2026-04-19T02:53:55.499421+00:00
private: false
---

# bin/migrate_memory.py

## Purpose

Migration runner for the m3-memory SQLite databases.

Supports multiple migration targets:
    - main (agent_memory.db) — always present
    - chatlog — optional, controlled by chatlog_config.chatlog_mode()

Subcommands:
    status                    Show current version and pending migrations
    plan [--to N]             Preview DDL that pending migrations would run (no changes)
    up [--to N] [--dry-run]   Apply pending migrations (prompts for backup + confirmation)
    down --to N [--dry-run]   Roll back to version N (requires .down.sql files)
    backup [--out PATH]       Take a standalone backup
    restore <PATH>            Restore the database from a backup file

All subcommands accept --target {main,chatlog,all} to select which DB(s) to operate on.
Default is "all" (operates on all configured targets).

Migration file formats (both supported, sorted by numeric prefix):
    NNN_name.sql            legacy, treated as up-only
    NNN_name.up.sql         explicit up
    NNN_name.down.sql       explicit down, paired with the same NNN

The schema_versions table records applied migrations. Each up/down operation
takes a filesystem-level backup of the database (including -wal/-shm) before
running, so operations are reversible at the file level even if the in-DB
transaction already committed.

## Entry points

- `def main()` (line 689)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--target` | Which DB target to operate on (default: all configured) | `all` | Displays status for all configured targets (main + chatlog if available). | str | Shows status for only specified target. |
| `--to` | Apply up to this version (default: latest) | None | Applies all pending migrations up to latest available version. | int | Applies migrations up to specified version; stops before newer ones. |
| `--target` | Which DB target to operate on (default: all configured) | `all` | Prompts for backup dir; applies migrations to all configured targets. | str | Applies migrations to only specified target. |
| `-y`, `--yes` | Skip confirmation prompts | `False` | Prompts user to confirm backup dir and migration execution. | store_true | Auto-selects default backup dir (~/.m3-memory/backups) and confirms migration. |
| `--dry-run` | Print the plan + DDL without applying anything | `False` |  | store_true |  |
| `--to` | Roll back to this version | — | Requires explicit version (no default). | int | Reverts migrations above specified version; checks for down files first. |
| `--target` | Which DB target to operate on (default: all configured) | `all` | Prompts for backup dir; rolls back all configured targets. | str | Rolls back only specified target. |
| `-y`, `--yes` | Skip confirmation prompts | `False` | Prompts user to confirm backup dir and rollback execution. | store_true | Auto-selects default backup dir and confirms rollback. |
| `--dry-run` | Print the plan + DDL without reverting anything | `False` |  | store_true |  |
| `--to` | Plan up to this version (default: latest) | None |  | int |  |
| `--target` | Which DB target to operate on (default: all configured) | `all` | Creates backup for all configured targets in backup_dir/<target>/ subdirs. | str | Creates backup for only specified target. |
| `--out` | Backup directory (overrides saved default) | None | Uses saved backup dir from .migrate_config.json; prompts if missing. | str | Uses specified directory instead of saved config; still requires confirmation if not -y. |
| `--target` | Which DB target to operate on (default: all configured) | `all` | Restores main database; warns if --target all is used (ambiguous). | str | Restores only specified target; chatlog for chat log DB. |
| `-y`, `--yes` | Skip interactive prompts | `False` | Prompts user for confirmation before creating backup. | store_true | Skips confirmation; creates backup immediately. |
| `path` | Path to the backup .db file | — | Required positional argument; no default. | str | Restores from specified backup file path. |
| `--target` | Which DB target to restore (default: main; use chatlog for chat log DB) | `main` |  | str |  |
| `-y`, `--yes` | Skip confirmation | `False` | Prompts user to confirm before overwriting current DB. | store_true | Skips confirmation; proceeds directly to restore. |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `chatlog_config (CHATLOG_MIGRATIONS_DIR, chatlog_db_path, chatlog_mode)`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `dst`` (line 181)
- `sqlite3.connect()  → `target.db_path`` (line 179)
- `sqlite3.connect()  → `target.db_path`` (line 215)
- `sqlite3.connect()  → `target.db_path`` (line 400)
- `sqlite3.connect()  → `target.db_path`` (line 468)
- `sqlite3.connect()  → `target.db_path`` (line 528)
- `sqlite3.connect()  → `target.db_path`` (line 585)
- `sqlite3.connect()  → `target.db_path`` (line 625)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `.db`
- `.migrate_config.json`
- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
