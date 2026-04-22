---
tool: bin/weekly_auditor.py
sha1: 7edccdc5ec0e
mtime_utc: 2026-04-22T01:22:50.173063+00:00
generated_utc: 2026-04-22T01:22:54.717772+00:00
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

- `def main()` (line 382)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--no-memory` | Skip writing summary to memory system and ChromaDB | `False` | Generates PDF + writes summary to memory_items + chroma_sync | store_true | Generates PDF only; skips memory_write & chroma_sync (line 414) |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None |  | str |  |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, resolve_db_path)`
- `memory_bridge (chroma_sync, memory_write)`

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.check_call()  → `[sys.executable, gen_script]`` (line 299)
- `subprocess.check_call()  → `[sys.executable, graph_script]`` (line 316)
- `subprocess.check_output()  → `cmd`` (line 251)

**sqlite**

- `sqlite3.connect()  → `resolve_db_path(None)`` (line 68)


## Notable external imports

- `fpdf (FPDF)`
- `fpdf.enums (XPos, YPos)`

## File dependencies (repo paths referenced)

- `.md`
- `INDEX.md`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
