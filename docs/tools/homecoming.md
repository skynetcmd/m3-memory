---
tool: bin/homecoming.py
sha1: 284f628ef211
mtime_utc: 2026-05-31T16:08:17.247433+00:00
generated_utc: 2026-05-31T18:42:52.733344+00:00
private: false
---

# bin/homecoming.py

## Purpose

bin/homecoming.py — "Homecoming" migration script for m3-memory.
Relocates repo-relative and old ~/.m3-memory/ state to new decoupled standard roots
(~/.m3/config and ~/.m3/engine).

This tool is non-destructive: it COPIES databases using the SQLite Backup API
and MOVES configuration files. It does NOT modify system-wide tool settings
(Claude/Gemini) to ensure safety. manually update tool settings if needed.

---

## Entry points

- `def main()` (line 91)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (get_m3_config_root, get_m3_engine_root)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `dst`` (line 82)
- `sqlite3.connect()  → `src`` (line 81)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `.db`
- `memory/.chatlog_config.json`
- `memory/.chatlog_ingest_cursor.json`
- `memory/.chatlog_state.json`
- `memory/.migrate_config.json`
- `memory/agent_chatlog.db`
- `memory/agent_memory.db`
- `memory/agent_test_bench.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
