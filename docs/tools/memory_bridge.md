---
tool: bin/memory_bridge.py
sha1: d149a3c7a7dd
mtime_utc: 2026-07-03T02:03:59.690525+00:00
generated_utc: 2026-07-03T20:00:03.666752+00:00
private: false
---

# bin/memory_bridge.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `M3_BRIDGE_PATH`
- `M3_HTTP_HOST`
- `M3_HTTP_PATH`
- `M3_HTTP_PORT`
- `M3_TOOLS_LAZY`
- `M3_TRANSPORT`

---

## Calls INTO this repo (intra-repo imports)

- `m3_memory.installer (load_config)`
- `m3_sdk (active_database)`
- `mcp_tool_catalog`
- `memory_core`
- `tool_domains`
- `tool_loader`
- `version_drift (check_and_record)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `mcp.server.fastmcp (FastMCP)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
