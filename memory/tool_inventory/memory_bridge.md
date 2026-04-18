---
tool: bin/memory_bridge.py
sha1: f9587a13dd1f
mtime_utc: 2026-04-18T03:34:10.130691+00:00
generated_utc: 2026-04-18T16:33:21.666099+00:00
private: false
---

# bin/memory_bridge.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `mcp_tool_catalog`
- `memory_core`
- `memory_maintenance`
- `memory_sync`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `mcp.server.fastmcp (FastMCP)`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
