---
tool: bin/memory_bridge.py
sha1: d79984f6d7e0
mtime_utc: 2026-09-19T00:56:35.476397+00:00
generated_utc: 2026-09-19T00:56:45.248595+00:00
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

- `M3_HTTP_HOST`
- `M3_HTTP_PATH`
- `M3_HTTP_PORT`
- `M3_HTTP_PUBLIC_HOST`
- `M3_PATH_BIN`
- `M3_TOOLS_LAZY`
- `M3_TRANSPORT`

---

## Calls INTO this repo (intra-repo imports)

- `m3_halt`
- `m3_http_auth`
- `m3_memory.installer (load_config)`
- `m3_sdk (active_database)`
- `mcp_compat`
- `mcp_compat (FastMCP)`
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

- `atexit`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
