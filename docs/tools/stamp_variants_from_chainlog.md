---
tool: benchmarks/locomo/stamp_variants_from_chainlog.py
sha1: 7d44ee6527f9
mtime_utc: 2026-04-21T20:02:02.919062+00:00
generated_utc: 2026-04-29T13:47:47.262632+00:00
private: true
---

# benchmarks/locomo/stamp_variants_from_chainlog.py

## Purpose

Retrofit `variant` field into summary.json files based on a chain.log.

The chain runner emits:
    === START <variant> HH:MM:SS ===
    (audit output, including summary_path at the end)
    === DONE  <variant> HH:MM:SS ===

For each bracket, find the audit run_dir whose run.log starts at a
timestamp between START and DONE, and stamp the variant into
summary.json (under key "variant") unless already present.

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--chain-log` | Path to chain.log with START/DONE brackets. | — | Argument is required. | Path | Parses chain log and stamps variants into matching audit run summaries. |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `summary.json`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
