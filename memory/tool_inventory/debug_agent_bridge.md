---
tool: bin/debug_agent_bridge.py
sha1: d56f4de3dbb8
mtime_utc: 2026-04-18T03:45:31.260359+00:00
generated_utc: 2026-04-18T16:33:21.625896+00:00
private: false
---

# bin/debug_agent_bridge.py

## Purpose

Debug Agent MCP Bridge — Autonomous debugging tools.

Tools: debug_analyze, debug_bisect, debug_trace, debug_correlate, debug_history, debug_report

Registration (settings.json):
  "debug_agent": {
      "command": "python3",
      "args": ["[M3_MEMORY_ROOT]/bin/debug_agent_bridge.py"]
  }

All internal paths are relative to BASE_DIR (auto-detected or AI_WORKSPACE_DIR env var).

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `AI_WORKSPACE_DIR`
- `ORIGIN_DEVICE`

## Calls INTO this repo (intra-repo imports)

- `embedding_utils (parse_model_size)`
- `m3_sdk (M3Context, LM_STUDIO_BASE)`
- `thermal_utils (get_thermal_status)`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `mcp.server.fastmcp (FastMCP)`
- `platform`

## File dependencies (repo paths referenced)

- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
