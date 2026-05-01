---
tool: bin/debug_agent_bridge.py
sha1: 3bcb9dc58afe
mtime_utc: 2026-05-01T09:15:53.148020+00:00
generated_utc: 2026-05-01T13:05:26.782292+00:00
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

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `AI_WORKSPACE_DIR`
- `ORIGIN_DEVICE`

---

## Calls INTO this repo (intra-repo imports)

- `agent_protocol (_THINK_TAG_RE)`
- `embedding_utils (parse_model_size)`
- `m3_sdk (LM_STUDIO_BASE, M3Context)`
- `thermal_utils (get_thermal_status)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `mcp.server.fastmcp (FastMCP)`
- `platform`

---

## File dependencies (repo paths referenced)

- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
