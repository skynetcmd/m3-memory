---
tool: benchmarks/locomo/analyze_handoff.py
sha1: 22796a5ef7ea
mtime_utc: 2026-04-21T20:02:02.907203+00:00
generated_utc: 2026-04-21T21:26:02.017889+00:00
private: false
---

# benchmarks/locomo/analyze_handoff.py

## Purpose

Phase 1 analysis: what does retrieval hand off to the answerer?

Reads retrieval_trace.jsonl and characterizes the context the answerer would
see. No LLM calls — pure structural analysis.

Questions answered:
  - Where does the FIRST gold hit land in the ranking? (distribution)
  - How much of top-K is noise vs gold? (precision@K)
  - Which categories fail where? (rank histogram per category)
  - Zero-hit questions: why — gold not ingested, or ingested but unretrieved?
  - For temporal Qs: is the session_date visible in the snippets?
  - Top-K content: how much is role=user vs assistant, within-session clustering

## Entry points

- `def main()` (line 231)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--trace` | Path to retrieval_trace.jsonl file. | — | Argument is required. | Path | Loads and analyzes the trace file at PATH. |
| `--out` | Optional path to write JSON analysis results. | None | Analysis printed to stdout only. | Path | Writes analysis JSON to PATH; also prints to stdout. |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `statistics`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
