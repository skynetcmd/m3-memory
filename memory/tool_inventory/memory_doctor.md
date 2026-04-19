---
tool: bin/memory_doctor.py
sha1: 62db595f8159
mtime_utc: 2026-04-18T22:28:14.310828+00:00
generated_utc: 2026-04-19T00:39:16.073086+00:00
private: false
---

# bin/memory_doctor.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `def main()` (line 56)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 61)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
