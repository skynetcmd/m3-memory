---
tool: bin/test_sqlite_pragmas.py
sha1: bd4a0292abf6
mtime_utc: 2026-05-07T03:32:14.565827+00:00
generated_utc: 2026-05-09T13:54:35.071345+00:00
private: false
---

# bin/test_sqlite_pragmas.py

## Purpose

test_sqlite_pragmas.py — regression tests for bin/sqlite_pragmas.py.

Tests:
- Each profile applies without error on a real on-disk DB.
- journal_mode=WAL after apply.
- wal_autocheckpoint and journal_size_limit match the profile spec.
- 1000-row write loop in WAL mode never grows the WAL beyond journal_size_limit.
- profile_for_db() returns the expected profile for known DB names and a
  generic path.

Run:
    python -m pytest bin/test_sqlite_pragmas.py -v
    python bin/test_sqlite_pragmas.py            # or run directly

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `sqlite_pragmas (PROFILES, apply_pragmas, checkpoint_passive, checkpoint_truncate, profile_for_db)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 43)


---

## Notable external imports

- `pytest`

---

## File dependencies (repo paths referenced)

- `.db`
- `/data/my_results_bench.db`
- `/some/path/test_chatlog.db`
- `/var/data/myapp.db`
- `cp.db`
- `cp_passive.db`
- `cp_truncate.db`
- `memory/agent_chatlog.db`
- `memory/agent_memory.db`
- `memory/agent_test_bench.db`
- `memory/lme_m.db`
- `test.db`
- `wal_test.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
