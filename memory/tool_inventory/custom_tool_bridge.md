---
tool: bin/custom_tool_bridge.py
sha1: a94138529ffc
mtime_utc: 2026-04-18T22:28:14.296957+00:00
generated_utc: 2026-04-19T00:39:15.980813+00:00
private: false
---

# bin/custom_tool_bridge.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `llm_failover (get_best_llm)`
- `m3_sdk (M3Context, StructuredLogger)`
- `thermal_utils (get_thermal_status)`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `httpx`
- `mcp.server.fastmcp (FastMCP)`

## File dependencies (repo paths referenced)

- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
