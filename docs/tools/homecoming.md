---
tool: bin/homecoming.py
sha1: e1c161be01eb
mtime_utc: 2026-05-21T14:41:32.907417+00:00
generated_utc: 2026-05-24T12:09:07.906648+00:00
private: false
---

# bin/homecoming.py

## Purpose

bin/homecoming.py — "Homecoming" migration script for m3-memory.
Relocates repo-relative state to ~/.m3-memory/.

This tool is non-destructive: it COPIES databases using the SQLite Backup API
and MOVES configuration files. It does NOT modify system-wide tool settings
(Claude/Gemini) to ensure safety. Users should update their tool settings
manually to point to the new bridge paths.

---

## Entry points

- `def main()` (line 72)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (get_m3_root)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `dst`` (line 63)
- `sqlite3.connect()  → `src`` (line 62)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `.chatlog_config.json`
- `.chatlog_ingest_cursor.json`
- `.chatlog_state.json`
- `.db`
- `.migrate_config.json`
- `agent_chatlog.db`
- `agent_memory.db`
- `agent_test_bench.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
