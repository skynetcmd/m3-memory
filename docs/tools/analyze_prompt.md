---
tool: benchmarks/locomo/analyze_prompt.py
sha1: 8e143feb913a
mtime_utc: 2026-04-21T20:02:02.907203+00:00
generated_utc: 2026-05-01T13:05:27.145526+00:00
private: true
---

# benchmarks/locomo/analyze_prompt.py

## Purpose

Phase 1: answerer-prompt anatomy and waste analysis.

Replays format_retrieved on every Q in a Phase-1 audit trace (using the hit
IDs the trace already captured — no re-retrieval needed) and measures:
  - Total prompt size (system + timeline + anchors + history + footer)
  - Whether gold dia_ids survive into the rendered history
  - Where gold appears (by char offset and by session block index)
  - How much of the prompt is "waste" by various definitions

Outputs:
  - prompt_analysis.jsonl  — one record per question
  - prompt_summary.json    — aggregate per-category and overall

---

## Entry points

- `def main()` (line 368)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--audit-dir` | Path to audit run directory containing retrieval_trace.jsonl. | `benchmarks/locomo/runs/audit_20260417_141947` | Uses fixed hardcoded audit directory. | str | Uses audit run at PATH instead. |
| `--dataset` | Path to LOCOMO dataset JSON file. | `str(BASE_DIR / 'data' / 'locomo' / 'locomo10.json')` | Uses hardcoded dataset path. | str | Loads dataset from PATH. |
| `--limit` | Process only first N questions from trace (0 = all). | `0` | Processes all questions in trace. | int | Limits processing to first N questions. |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `bench_locomo`
- `memory_core`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `locomo10.json`
- `prompt_summary.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
