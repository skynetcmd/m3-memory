---
tool: bin/chatlog_status.py
sha1: 90720e796394
mtime_utc: 2026-04-18T22:28:14.289182+00:00
generated_utc: 2026-04-19T00:39:15.965734+00:00
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

## Entry points

- `def main()` (line 194)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `chatlog_config`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `chatlog_db`` (line 69)
- `sqlite3.connect()  → `main_db`` (line 53)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
