---
tool: bin/bench_memory.py
sha1: 488bf9c0dfa6
mtime_utc: 2026-05-06T05:09:14.767568+00:00
generated_utc: 2026-05-06T23:11:44.803541+00:00
private: false
---

# bin/bench_memory.py

## Purpose

Memory system benchmark script.
Seeds test data, measures latency/throughput, reports pass/fail against targets.

Usage: python bin/bench_memory.py [--database PATH]

Point --database at a scratch DB (e.g. memory/bench.db) to keep benchmark
data out of your live memory store. Default honors M3_DATABASE then falls
back to memory/agent_memory.db.

---

## Entry points

- `def main()` (line 206)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes this run against PATH for all DB reads/writes. |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, resolve_db_path)`
- `sqlite_pragmas (apply_pragmas, profile_for_db)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 58)


---

## Notable external imports

- `statistics`

---

## File dependencies (repo paths referenced)

- `bench_report.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
