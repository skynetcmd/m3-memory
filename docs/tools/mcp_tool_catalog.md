---
tool: bin/mcp_tool_catalog.py
sha1: 3b7bd6204bef
mtime_utc: 2026-06-30T11:36:02.422636+00:00
generated_utc: 2026-06-30T22:19:18.421152+00:00
private: false
---

# bin/mcp_tool_catalog.py

## Purpose

mcp_tool_catalog.py — single source of truth for the m3-memory MCP tool catalog.

Imported by:
  - bin/memory_bridge.py (FastMCP stdio server — registers each spec via @mcp.tool())
  - examples/multi-agent-team/dispatch.py (orchestrator-side dispatch loop)

Zero FastMCP dependency. Pure Python + memory_core + memory_sync + memory_maintenance.
Never import this module from those modules — that would create a cycle.

Mutation-safety invariant (do not regress): mutating memory tools
(memory_delete, memory_supersede) require the FULL UUID for their target id —
a prefix is rejected via _is_full_uuid in their validators. Read tools
(memory_get) accept an 8-char prefix for convenience, but an ambiguous prefix
on a mutation could close/delete the wrong memory irreversibly. This asymmetry
is intentional; keep the validators and the "full UUID required" wording in the
tool descriptions so it survives doc-inventory regeneration. Also note:
memory_supersede is non-destructive and creates a NEW successor each call — it
is an update primitive, not a delete; do not chain it to "clean up" clutter.

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `MCP_PROXY_ALLOW_DESTRUCTIVE`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (active_database)`
- `tool_domains`
- `tool_loader`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `importlib`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
