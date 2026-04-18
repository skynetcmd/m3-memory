---
tool: bin/bench_memory.py
sha1: 617c734e07f6
mtime_utc: 2026-04-18T03:34:10.128688+00:00
generated_utc: 2026-04-18T05:16:53.092429+00:00
private: false
---

# bin/bench_memory.py

## Purpose

Memory system benchmark script.
Seeds test data, measures latency/throughput, reports pass/fail against targets.

Usage: python bin/bench_memory.py

## Entry points

- `def main()` (line 198)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Implementation notes

**SQLite-only:** This benchmark tool operates directly on SQLite (agent_memory.db) and does not use bulk-insert or memory_write_bulk_impl. No embedding or ingest pipeline integration. Useful for isolated performance testing on local database operations.

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 50)


## Notable external imports

- `statistics`

## File dependencies (repo paths referenced)

- `agent_memory.db`
- `bench_report.json`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
