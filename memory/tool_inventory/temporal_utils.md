---
tool: bin/temporal_utils.py
sha1: be5809e14f74
mtime_utc: 2026-04-19T02:44:47.990823+00:00
generated_utc: 2026-04-19T02:53:55.523919+00:00
private: false
---

# bin/temporal_utils.py

## Purpose

Enhanced temporal resolution utility for m3-memory.
Resolves relative date expressions (yesterday, last Friday, the Sunday before June 1st)
into absolute ISO-8601 dates based on an anchor timestamp.

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
