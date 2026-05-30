---
name: m3-export
description: GDPR Article 20 — export all memories you own as portable JSON.
---
# M3 Export

## When to Use
Use this skill when the user requests to export all their memories (GDPR Article 20 data export) or wants to back up their local memory store to a portable JSON file.

## Instructions
Call the `m3:gdpr_export` MCP tool. The tool returns the full memory set as JSON.

Save the JSON to `~/.m3-memory/export-$(date +%Y%m%d-%H%M%S).json` and tell the user:
- where it landed
- how many memories it contains
- one-sentence note that the file is portable: another m3-memory instance can `gdpr_import` it
