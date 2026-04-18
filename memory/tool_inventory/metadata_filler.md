---
tool: scripts/metadata_filler.py
sha1: 6a562a75d6ca
mtime_utc: 2026-04-06T00:25:01.015417+00:00
generated_utc: 2026-04-18T16:33:21.793771+00:00
private: false
---

# scripts/metadata_filler.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `def main()` (line 78)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 79)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `memory/agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
