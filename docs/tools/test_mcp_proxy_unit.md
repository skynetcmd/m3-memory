---
tool: bin/test_mcp_proxy_unit.py
sha1: 994a5ba6d42c
mtime_utc: 2026-04-22T01:03:02.054983+00:00
generated_utc: 2026-04-22T01:32:11.703491+00:00
private: false
---

# bin/test_mcp_proxy_unit.py

## Purpose

test_mcp_proxy_unit.py - In-process unit tests for mcp_proxy.

Unlike test_mcp_proxy.py (which exercises the running HTTP proxy with real
provider keys), this suite imports mcp_proxy as a module and verifies:
  - It imports without ImportError (regression for the m3_sdk break)
  - Tool merging from PROTOCOL + DEBUG + catalog produces the expected count
  - Default-allow filtering hides destructive catalog tools
  - MCP_PROXY_ALLOW_DESTRUCTIVE=1 exposes them
  - _execute_tool dispatches catalog tools through mcp_tool_catalog.execute_tool
  - _execute_tool refuses unknown tools and gives a helpful error for disabled destructive tools
  - inject_agent_id is honored on memory_write

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `mcp_proxy`
- `mcp_tool_catalog`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `unittest`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
