---
tool: bin/benchmark_memory.py
sha1: 87c55b0846a7
mtime_utc: 2026-04-22T01:03:02.023007+00:00
generated_utc: 2026-05-01T13:05:26.714575+00:00
private: false
---

# bin/benchmark_memory.py

## Purpose

Retrieval Quality Benchmark for M3 Memory System.
Measures Hit@1, Hit@5, MRR, and Latency across diverse test cases.

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

- `auth_utils (get_api_key)`
- `memory_core (memory_delete_impl, memory_search_impl, memory_write_impl)`

---

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 28)


---

## Notable external imports

- `httpx`
- `statistics`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
