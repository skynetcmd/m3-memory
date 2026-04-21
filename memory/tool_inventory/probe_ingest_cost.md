---
tool: benchmarks/locomo/probe_ingest_cost.py
sha1: 8487324a6b94
mtime_utc: 2026-04-21T20:02:02.907203+00:00
generated_utc: 2026-04-21T21:26:02.044028+00:00
private: false
---

# benchmarks/locomo/probe_ingest_cost.py

## Purpose

Measure ingestion cost per variant for 1 and 10 LOCOMO turns.

Measures:
  - wall-clock seconds
  - Python-side CPU (user+sys), resource.getrusage
  - LM Studio-side CPU (Δuser+Δsys), psutil against LM Studio PID(s)
  - #LLM calls, prompt+completion tokens (from response.usage)
  - #embed calls, total chars embedded
  - rows written

Four variants:
  baseline         — no heuristic, no LLM enrichment
  heuristic_c1c4   — heuristic title/entities, no LLM
  llm_v1           — heuristic + force LLM title+entities
  llm_only         — LLM title+entities, no heuristic

Scratch DB via M3_DB_PATH; throwaway variant tags probe_<v>_n{N}.

## Entry points

- `async def main()` (line 295)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--sample` |  | `conv-26` |  | str |  |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `bench_locomo`
- `memory_core`
- `memory_core (memory_write_impl, _db, _content_hash)`
- `temporal_utils`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `httpx`
- `psutil`

## File dependencies (repo paths referenced)

- `INGEST_COST_PROBE.md`
- `locomo10.json`
- `probe_ingest_cost_results.json`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
