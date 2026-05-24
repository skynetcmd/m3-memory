---
tool: bin/setup_test_db.py
sha1: 9d4c46b13d44
mtime_utc: 2026-05-06T05:08:54.263203+00:00
generated_utc: 2026-05-24T12:09:08.478050+00:00
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

---

## Entry points

- `def main()` (line 38)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--force` | Wipe the target DB file before seeding (default: append to existing). | `False` | Appends migrations to existing DB (idempotent). | store_true | Deletes DB + WAL/SHM files, then seeds fresh. |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes all DB reads/writes against PATH for this run. |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, resolve_db_path)`
- `sqlite_pragmas (apply_pragmas, profile_for_db)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 62)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `.down.sql`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
