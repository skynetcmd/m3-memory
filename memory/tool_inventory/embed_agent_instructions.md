---
tool: bin/embed_agent_instructions.py
sha1: fbfd0024bb9f
mtime_utc: 2026-04-11T18:26:43.113999+00:00
generated_utc: 2026-04-18T05:16:53.112240+00:00
private: false
---

# bin/embed_agent_instructions.py

## Purpose

One-shot script: embed AGENT_INSTRUCTIONS.md sections as searchable memory items.

Splits the file into 9 semantic sections, writes each as type=document
with embed=True. Idempotent: soft-deletes any prior architecture items
(agent_id="system", source="architecture") before writing fresh ones.

## Entry points

- `async def main()` (line 217)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `memory_bridge (memory_delete, memory_write)`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 224)
- `sqlite3.connect()  → `DB_PATH`` (line 260)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `AGENT_INSTRUCTIONS.md`
- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
