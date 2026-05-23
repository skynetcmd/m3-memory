---
tool: bin/start_mcp_proxy.sh
sha1: 6f0cf6bf3552
mtime_utc: 2026-05-23T14:11:45.522317+00:00
generated_utc: 2026-05-23T17:51:49.296023+00:00
private: false
---

# bin/start_mcp_proxy.sh

## Purpose

start_mcp_proxy.sh — Launch the MCP Tool Execution Proxy on localhost:9000
Usage: bash ~/m3-memory/repo/bin/start_mcp_proxy.sh [--background]

---

## Entry points

- Bash execution

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `BIN_DIR`
- `EXISTING_PID`
- `HOME`
- `LOG_FILE`
- `PID_FILE`
- `PORT`
- `PROXY_SCRIPT`
- `PYTHON`
- `WORKSPACE`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
