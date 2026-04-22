---
tool: bin/memory_doctor.py
sha1: 0d8fb2bfca62
mtime_utc: 2026-04-21T20:43:15.424601+00:00
generated_utc: 2026-04-21T21:26:01.909396+00:00
private: false
---

# bin/memory_doctor.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `def main()` (line 58)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None |  | str |  |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, resolve_db_path)`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 68)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
