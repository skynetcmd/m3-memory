---
tool: benchmarks/locomo/retrieval_audit.py
sha1: 1bec395aa1eb
mtime_utc: 2026-04-21T20:02:02.909000+00:00
generated_utc: 2026-04-21T21:26:02.050805+00:00
private: false
---

# benchmarks/locomo/retrieval_audit.py

## Purpose

Phase 1: LOCOMO retrieval audit.

Runs the production retrieve_for_question on the first N questions of the LOCOMO
dataset and compares the retrieved hits against the per-question gold dia_id
evidence. No answerer, no judge. Output is a JSONL trace that Phase 2 consumes.

This script imports from bin/ read-only — it does not modify any main-branch
retrieval, ingest, or generation logic.

## Entry points

- `async def run()` (line 141)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dataset` |  | `DEFAULT_DATASET` |  | Path |  |
| `--limit` | Process first N questions across samples (default 200). | `200` |  | int |  |
| `--k` | Top-K retrieval baseline — matches bench_locomo default. | `40` |  | int |  |
| `--cluster-size` |  | `5` |  | int |  |
| `--graph-depth` |  | `1` |  | int |  |
| `--force-ingest` | Re-ingest touched samples even if already present. | `False` |  | store_true |  |
| `--enable-smart-retrieval` | Opt into smart_time_boost + neighbor-session expansion. Off by default on LOCOMO (relative-date dialog). Env var: M3_ENABLE_SMART_RETRIEVAL=1. | `os.environ.get('M3_ENABLE_SMART_RETRIEVAL', '').lower() in ('1', 'true', 'yes')` |  | store_true |  |
| `--variant` | Filter retrieval to rows with this variant tag. Use '__none__' for untagged rows. Empty (default) returns all rows regardless of variant. | `` |  | str |  |

## Environment variables read

- `M3_ENABLE_SMART_RETRIEVAL`

## Calls INTO this repo (intra-repo imports)

- `bench_locomo`
- `bench_locomo (CATEGORIES, classify_question, ingest_sample_with_graph, retrieve_for_question)`
- `memory_core`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `locomo10.json`
- `summary.json`
- `zero_hit_questions.json`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
