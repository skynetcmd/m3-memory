---
tool: bin/memory_bridge.py
sha1: f9587a13dd1f
mtime_utc: 2026-04-18T03:34:10.130691+00:00
generated_utc: 2026-04-18T05:16:53.145892+00:00
private: false
---

# bin/memory_bridge.py

## Purpose
Catalog-driven FastMCP bridge that registers 44 memory tools from `mcp_tool_catalog.TOOLS`. Exposes memory_write, memory_search, memory_update, task_create, conversation_start, and similar operations as MCP tools. Also provides module-level callables for direct test/import usage.

## Entry points / MCP tools exposed
- **MCP tool registration** (line 166–183) — `_register_catalog_tools()` registers all ToolSpec entries
- **Module-level callables** (line 188–189) — all catalog tools exposed as `globals()[_spec.name]`
- **Helper exports** (line 40–67) — `conversation_messages()`, `sync_status()`
- **FastMCP startup** (line 191–194) — `__main__` block runs `mcp.run()`

## CLI flags / arguments
_(no CLI surface — invoked as library/MCP server module only)_

## Environment variables read
_(none detected)_

## Calls INTO this repo (intra-repo imports)
- `memory_core` — database conn, embedding, sync tables, hash, pack helpers
- `memory_sync` — sync operations (imported but not called in this file)
- `memory_maintenance` — maintenance functions (imported but not called)
- `mcp_tool_catalog` — TOOLS list, validation, VALID_MEMORY_TYPES, MAX_* constants

## Calls OUT (external side-channels)
**sqlite** (via memory_core._db):
- Line 43–50: conversation_messages() queries memory_relationships + memory_items
- Line 59–65: sync_status() queries chroma_sync_queue, chroma_mirror, sync_conflicts

**FastMCP/MCP**:
- Line 1: mcp.server.fastmcp.FastMCP instance created
- Line 181: mcp.tool() decorator registers tools
- Line 194: mcp.run() starts server

## File dependencies (repo paths referenced)
- Dynamic imports from `os.path.dirname(__file__)` (line 18) to load memory_core, memory_sync, memory_maintenance, mcp_tool_catalog

## Re-validation
If `sha1` differs, run `python bin/gen_tool_inventory.py` then re-audit catalog membership and tool spec changes.
