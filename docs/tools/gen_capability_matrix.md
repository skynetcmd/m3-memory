---
tool: bin/gen_capability_matrix.py
sha1: f7ddbe61912c
mtime_utc: 2026-07-02T21:51:11.641458+00:00
generated_utc: 2026-07-03T20:00:03.359771+00:00
private: false
---

# bin/gen_capability_matrix.py

## Purpose

gen_capability_matrix.py — generate docs/CAPABILITY_MATRIX.md from the MCP catalog.

A single scannable capability index grouped by domain, serving three audiences at
once (humans scanning for a feature, search engines indexing capabilities, and AI
agents mapping a natural-language request to the right tool). Generated from
docs/tools/MCP_CATALOG.json so it never drifts from the actual tool surface — run
after any catalog change, same as gen_mcp_inventory.py.

    python bin/gen_capability_matrix.py

---

## Entry points

- `def main()` (line 43)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

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

- `CAPABILITY_MATRIX.md`
- `MCP_CATALOG.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
