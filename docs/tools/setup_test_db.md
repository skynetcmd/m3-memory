---
tool: bin/setup_test_db.py
sha1: 1bf21c5c9711
mtime_utc: 2026-04-22T01:03:02.051328+00:00
generated_utc: 2026-04-22T01:22:54.669067+00:00
private: false
---

# bin/setup_test_db.py

## Purpose

Seed a fresh SQLite DB with the full m3-memory schema for test isolation.

Applies every forward migration in ``memory/migrations/`` (skipping the
``.down.sql`` rollback files) so the resulting DB is schema-complete and can
back the live MCP server, the CLI scripts, and the test suites.

Usage:
    python bin/setup_test_db.py --database memory/_test.db
    M3_DATABASE=memory/_test.db python bin/test_memory_bridge.py

Exits non-zero if any migration fails.

## Entry points

- `def main()` (line 37)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--force` | Wipe the target DB file before seeding (default: append to existing). | `False` |  | store_true |  |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None |  | str |  |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, resolve_db_path)`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 61)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `.down.sql`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
