---
tool: bin/migrate_memory.py
sha1: 6f5d18e700e9
mtime_utc: 2026-04-17T03:42:11.732199+00:00
generated_utc: 2026-04-17T04:17:01.749666+00:00
private: false
---

# bin/migrate_memory.py

## Purpose

Migration runner for the m3-memory SQLite database.

Subcommands:
    status                    Show current version and pending migrations
    plan [--to N]             Preview DDL that pending migrations would run (no changes)
    up [--to N] [--dry-run]   Apply pending migrations (prompts for backup + confirmation)
    down --to N [--dry-run]   Roll back to version N (requires .down.sql files)
    backup [--out PATH]       Take a standalone backup
    restore <PATH>            Restore the database from a backup file

Migration file formats (both supported, sorted by numeric prefix):
    NNN_name.sql            legacy, treated as up-only
    NNN_name.up.sql         explicit up
    NNN_name.down.sql       explicit down, paired with the same NNN

The schema_versions table records applied migrations. Each up/down operation
takes a filesystem-level backup of the database (including -wal/-shm) before
running, so operations are reversible at the file level even if the in-DB
transaction already committed.

## Entry points

- `def main()` (line 544)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--to` | Apply up to this version (default: latest) | — |  | int |  |
| `-y`, `--yes` | Skip confirmation prompts | — |  | store_true |  |
| `--dry-run` | Print the plan + DDL without applying anything | — |  | store_true |  |
| `--to` | Roll back to this version | — |  | int |  |
| `-y`, `--yes` | Skip confirmation prompts | — |  | store_true |  |
| `--dry-run` | Print the plan + DDL without reverting anything | — |  | store_true |  |
| `--to` | Plan up to this version (default: latest) | — |  | int |  |
| `--out` | Backup directory (overrides saved default) | — |  | str |  |
| `-y`, `--yes` | Skip interactive prompts | — |  | store_true |  |
| `path` | Path to the backup .db file | — |  |  |  |
| `-y`, `--yes` | Skip confirmation | — |  | store_true |  |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 109)
- `sqlite3.connect()  → `DB_PATH`` (line 147)
- `sqlite3.connect()  → `DB_PATH`` (line 321)
- `sqlite3.connect()  → `DB_PATH`` (line 375)
- `sqlite3.connect()  → `DB_PATH`` (line 424)
- `sqlite3.connect()  → `DB_PATH`` (line 475)
- `sqlite3.connect()  → `DB_PATH`` (line 492)
- `sqlite3.connect()  → `dst`` (line 111)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `.migrate_config.json`
- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
