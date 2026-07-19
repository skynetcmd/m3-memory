---
tool: bin/weekly_auditor.py
sha1: f0a3730740da
mtime_utc: 2026-07-19T17:58:00.537587+00:00
generated_utc: 2026-07-19T19:29:23.041651+00:00
private: false
---

# bin/weekly_auditor.py

## Purpose

Weekly Audit Report -- M3 Memory

Generates a PDF covering:
  1. Memory System Health (memory_items + embeddings)
  2. Project Decisions (last 7 days)
  3. Activity Timeline (legacy activity_logs)
  4. Git Activity (~/m3-memory)

Optionally writes a consolidated summary into memory_items.
Use --no-memory to skip the memory write step.

---

## Entry points

- `def main()` (line 425)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--no-memory` | Skip writing summary to memory system | `False` | Generates PDF + writes summary to memory_items | store_true | Generates PDF only; skips memory_write |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes all DB reads/writes against PATH for this run. |

---

## Environment variables read

- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (add_log_file_arg, setup_task_runtime)`
- `_task_runtime (no_window_kwargs)`
- `m3_sdk (add_database_arg, resolve_db_path)`
- `memory_bridge (memory_write)`
- `memory_core`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.check_call()  → `[sys.executable, gen_script]`` (line 300)
- `subprocess.check_call()  → `[sys.executable, graph_script]`` (line 317)
- `subprocess.check_output()  → `cmd`` (line 250)


---

## Notable external imports

- `fpdf (FPDF)`
- `fpdf.enums (XPos, YPos)`
- `memory.backends (dialect)`

---

## File dependencies (repo paths referenced)

- `.md`
- `INDEX.md`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
