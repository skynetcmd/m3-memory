---
tool: bin/weekly_auditor.py
sha1: 9a0ff8cd9916
mtime_utc: 2026-04-18T22:28:14.300396+00:00
generated_utc: 2026-04-19T00:39:16.155346+00:00
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

- `def main()` (line 374)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--no-memory` | Skip writing summary to memory system and ChromaDB | `False` | Generates PDF + writes summary to memory_items + chroma_sync | store_true | Generates PDF only; skips memory_write & chroma_sync (line 414) |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `memory_bridge (chroma_sync, memory_write)`

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.check_call()  → `[sys.executable, gen_script]`` (line 291)
- `subprocess.check_call()  → `[sys.executable, graph_script]`` (line 308)
- `subprocess.check_output()  → `cmd`` (line 243)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 60)


## Notable external imports

- `fpdf (FPDF)`
- `fpdf.enums (XPos, YPos)`

## File dependencies (repo paths referenced)

- `.md`
- `INDEX.md`
- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
