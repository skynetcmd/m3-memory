---
tool: scripts/inventory_graph.py
sha1: 9e89d836cfa4
mtime_utc: 2026-04-18T05:06:31.404255+00:00
generated_utc: 2026-04-18T05:16:53.263041+00:00
private: false
---

# scripts/inventory_graph.py

## Purpose

Build a mermaid call-graph from tool-inventory markdown files.

Edges come from each tool's "Calls INTO this repo" section (intra-repo imports)
and "Calls OUT" subprocess invocations of sibling `bin/*.py` scripts.

## Entry points

- `def main()` (line 39)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `CALL_GRAPH.md`
- `INDEX.md`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
