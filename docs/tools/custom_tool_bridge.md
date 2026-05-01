---
tool: bin/custom_tool_bridge.py
sha1: 7c5b0246391c
mtime_utc: 2026-05-01T09:13:26.346877+00:00
generated_utc: 2026-05-01T13:05:26.778005+00:00
private: false
---

# bin/custom_tool_bridge.py

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

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `agent_protocol (_THINK_TAG_RE)`
- `llm_failover (clear_failover_caches)`
- `llm_failover (get_best_llm)`
- `m3_sdk (M3Context, StructuredLogger)`
- `thermal_utils (get_thermal_status)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `httpx`
- `mcp.server.fastmcp (FastMCP)`

---

## File dependencies (repo paths referenced)

- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
