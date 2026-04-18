---
tool: bin/migrate_memory.py
sha1: e66a7a36e8a2
mtime_utc: 2026-04-18T03:45:31.264360+00:00
generated_utc: 2026-04-18T05:16:53.204637+00:00
private: false
---

# bin/migrate_memory.py

## Purpose

Migration runner for the m3-memory SQLite database.

Subcommands:
    status              Show current version and pending migrations
    up [--to N]         Apply pending migrations (prompts for backup dir + confirmation)
    down [--to N]       Roll back to version N (requires .down.sql files)
    backup [--out PATH] Take a standalone backup
    restore <PATH>      Restore the database from a backup file

Migration file formats (both supported, sorted by numeric prefix):
    NNN_name.sql            legacy, treated as up-only
    NNN_name.up.sql         explicit up
    NNN_name.down.sql       explicit down, paired with the same NNN

The schema_versions table records applied migrations. Each up/down operation
takes a filesystem-level backup of the database (including -wal/-shm) before
running, so operations are reversible at the file level even if the in-DB
transaction already committed.

## Entry points

- `def main()` (line 401)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--to` | Apply up to this version (default: latest) | — |  | int |  |
| `-y`, `--yes` | Skip confirmation prompts | — |  | store_true |  |
| `--to` | Roll back to this version | — |  | int |  |
| `-y`, `--yes` | Skip confirmation prompts | — |  | store_true |  |
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

- `sqlite3.connect()  → `DB_PATH`` (line 229)
- `sqlite3.connect()  → `DB_PATH`` (line 266)
- `sqlite3.connect()  → `DB_PATH`` (line 310)
- `sqlite3.connect()  → `DB_PATH`` (line 356)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `.migrate_config.json`
- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
