---
tool: bin/bench_memory.py
sha1: 66ecf923d0ce
mtime_utc: 2026-04-18T22:29:31.706839+00:00
generated_utc: 2026-04-19T00:39:15.942053+00:00
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
