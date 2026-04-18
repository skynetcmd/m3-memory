---
tool: bin/mcp_tool_catalog.py
sha1: 357c04d9682a
mtime_utc: 2026-04-18T15:53:48.184714+00:00
generated_utc: 2026-04-18T16:33:21.663705+00:00
private: false
---

# bin/mcp_tool_catalog.py

## Purpose

mcp_tool_catalog.py — single source of truth for the m3-memory MCP tool catalog.

Imported by:
  - bin/memory_bridge.py (FastMCP stdio server — registers each spec via @mcp.tool())
  - examples/multi-agent-team/dispatch.py (orchestrator-side dispatch loop)

Zero FastMCP dependency. Pure Python + memory_core + memory_sync + memory_maintenance.
Never import this module from those modules — that would create a cycle.

## Entry points

_(no conventional entry point detected)_

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `chatlog_core`
- `chatlog_status`
- `memory_core`
- `memory_maintenance`
- `memory_sync`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
