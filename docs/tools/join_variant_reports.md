---
tool: benchmarks/locomo/join_variant_reports.py
sha1: e91b5cfad9bc
mtime_utc: 2026-04-21T20:02:02.907203+00:00
generated_utc: 2026-04-29T13:47:47.202139+00:00
private: true
---

# benchmarks/locomo/join_variant_reports.py

## Purpose

Join multiple retrieval_audit summary.json files into one comparison report.

Finds the most recent audit run for each requested variant by walking
benchmarks/Phase1/runs/audit_*/summary.json and matching by the variant
recorded in metadata. If a variant has no run yet, it is omitted.

Writes a markdown report to stdout (or --out) summarizing:
- overall any-gold-hit rate, mean_first_gold_rank, zero-hit count
- recall@K columns side-by-side for K in (1, 3, 5, 10, 20, 40)
- per-category table for any-gold-hit and r@10

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--variants` | Variant ordering in report (default: baseline, heuristic_c1c4, llm_v1, llm_only) | `DEFAULT_VARIANTS` | Uses default column ordering. | str | Renders comparison columns in specified variant order. |
| `--baseline` | Variant to show deltas against | `baseline` | Shows deltas relative to baseline variant. | str | Computes delta columns relative to specified variant. |
| `--out` | Write report here instead of stdout | None | Writes markdown to stdout. | Path | Writes comparison report to PATH. |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `audit_*/summary.json`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
