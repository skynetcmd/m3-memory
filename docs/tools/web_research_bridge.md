---
tool: bin/web_research_bridge.py
sha1: 0ec1146b2a82
mtime_utc: 2026-05-23T12:31:13.390595+00:00
generated_utc: 2026-05-23T17:51:49.342520+00:00
private: false
---

# bin/web_research_bridge.py

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

- `m3_sdk (M3Context)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `httpx`
- `mcp.server.fastmcp (FastMCP)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
