---
tool: bin/weekly_auditor.py
sha1: a8f0f8c37a5e
mtime_utc: 2026-04-17T04:13:06.909217+00:00
generated_utc: 2026-04-17T04:17:01.801310+00:00
private: false
---

# bin/weekly_auditor.py

## Purpose

Weekly Audit Report -- M3 Memory

Generates a PDF covering:
  1. Memory System Health (memory_items + embeddings)
  2. Project Decisions (last 7 days)
  3. Activity Timeline (legacy activity_logs)
  4. ChromaDB Sync Status
  5. Git Activity (~/m3-memory)

Optionally writes a consolidated summary into memory_items + ChromaDB.
Use --no-memory to skip the memory write step.

## Entry points

- `def main()` (line 354)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--no-memory` | Skip memory/ChromaDB write | — |  | store_true |  |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `memory_bridge (memory_write, chroma_sync)` — write weekly summary to memory; sync ChromaDB

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.check_call()  → `gen_tool_inventory.py`` (line 291)
- `subprocess.check_output()  → git log`` (line 243)

**sqlite**

- `sqlite3.connect()  → agent_memory.db`` (line 60)


## Notable external imports

- `fpdf (FPDF)`
- `fpdf.enums (XPos, YPos)`

## File dependencies (repo paths referenced)

- `memory/tool_inventory/*.md` (reads/updates inventory)
- `reports/Audit_*.pdf` (PDF output)
- `agent_memory.db` (query sections 1–4)

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
