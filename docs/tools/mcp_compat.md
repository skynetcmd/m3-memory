---
tool: bin/mcp_compat.py
sha1: 1c5284e896d5
mtime_utc: 2026-09-17T16:50:29.984857+00:00
generated_utc: 2026-09-17T18:03:36.503934+00:00
private: false
---

# bin/mcp_compat.py

## Purpose

Single owner of the MCP server-class import, across mcp 1.x and 2.x.

── WHY THIS MODULE EXISTS ────────────────────────────────────────────────────

mcp 2.0 renamed the server class: `mcp.server.fastmcp.FastMCP` became
`mcp.server.mcpserver.MCPServer`. Eight modules in this repo construct one, and
before this module each carried its own import -- in three different spellings
(a bare `from mcp.server.fastmcp import`, a try/except falling back to
`from mcp import FastMCP`, and one that fell back to a hand-rolled no-op stub).

Copying a try/except into each of those eight sites would be the §10a
duplication defect: eight copies of one policy, free to drift. The resolution
lives here once, and call sites import the NAME.

── WHAT ACTUALLY CHANGED IN 2.x (measured, not assumed) ──────────────────────

Verified against mcp 2.2.0 in a throwaway venv on 2026-09-17. Far less moved
than the rename suggests -- every API this repo uses survived:

    tool()  run()  settings  add_tool()  remove_tool()  list_tools()
    streamable_http_app()  sse_app()  _token_verifier

`run()` and `tool()` keep signatures compatible with our call sites, and
construction still accepts `instructions=`.

ONE real break, and it is not the rename. `Settings` dropped three fields:

    settings.host                  -> ValueError: no field "host"
    settings.port                  -> ValueError: no field "port"
    settings.streamable_http_path  -> ValueError: no field "..."

They moved into the transport call:

    run_streamable_http_async(*, host='127.0.0.1', port=8000,
                              streamable_http_path='/mcp', ...)

`Settings` now carries only auth, debug, dependencies, lifespan, log_level and
the three warn_on_duplicate_* flags. `run_http()` below owns that difference so
no bridge has to branch on it.

── WHAT THIS MODULE DELIBERATELY DOES NOT DO ─────────────────────────────────

It does NOT re-export a stub when mcp is absent. news_fetcher.py previously
fell back to a dummy class whose `tool()` decorator silently did nothing, so a
missing dependency produced a server that started and registered ZERO tools
rather than failing. That is the §3 violation -- a fault that presents as a
working system. A missing mcp now raises at import, where it is legible.

---

## Entry points

_(no conventional entry point detected)_

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

- `mcp.server.fastmcp (FastMCP)`
- `mcp.server.mcpserver (MCPServer)`
- `mcp.server.transport_security (TransportSecuritySettings)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
