---
tool: benchmarks/locomo/compare_runs.py
sha1: 6ecab171c430
mtime_utc: 2026-04-21T20:02:02.907203+00:00
generated_utc: 2026-05-01T13:05:27.158028+00:00
private: true
---

# benchmarks/locomo/compare_runs.py

## Purpose

Compare two Phase 1 runs side-by-side.

---

## Entry points

- `def main()` (line 15)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--a` | Baseline run dir name under benchmarks/locomo/runs/ | — | Argument is required. | str | Loads baseline summary and handoff analysis from runs/A/. |
| `--b` | Candidate run dir name under benchmarks/locomo/runs/ | — | Argument is required. | str | Loads candidate summary and handoff analysis from runs/B/; renders diff vs A. |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `handoff_analysis.json`
- `summary.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
