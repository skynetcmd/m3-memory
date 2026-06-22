---
tool: bin/gen_mcp_inventory.py
sha1: 06fc82b3496f
mtime_utc: 2026-05-31T20:45:02.905255+00:00
generated_utc: 2026-06-12T20:00:05.017253+00:00
private: false
---

# bin/gen_mcp_inventory.py

## Purpose

gen_mcp_inventory.py — Generates docs/MCP_TOOLS.md from mcp_tool_catalog and mcp_proxy.

---

## Entry points

- `def main()` (line 216)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `mcp_tool_catalog`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `MCP_TOOLS.md`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
