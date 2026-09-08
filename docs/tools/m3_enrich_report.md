---
tool: bin/m3_enrich_report.py
sha1: e5fea80d2e10
mtime_utc: 2026-09-08T10:33:19.172210+00:00
generated_utc: 2026-09-08T10:33:52.480952+00:00
private: false
---

# bin/m3_enrich_report.py

## Purpose

Summarize an m3_enrich run from enrichment_groups + enrichment_runs.

Produces a human-readable report with:
  - status breakdown (pending / success / empty / failed / dead_letter)
  - error_class distribution + sample messages
  - per-size-band success rate (if content_size_k is populated)
  - elapsed wallclock + throughput
  - a clear note when 429 / quota patterns dominate, so the operator
    knows to wait + resume rather than re-run from scratch

Modes:
  --run-id UUID          summarize a specific enrich_run_id
  --variant VARIANT      summarize all rows for a source_variant (any run)
  --target FILE          write markdown to FILE (default: stdout)
  --db PATH              SQLite DB path (default: memory/agent_memory.db
                         or M3_DATABASE env)

Designed to be called automatically at the end of m3_enrich runs so that
every run leaves an artifact behind, mirroring the docs/audits/ pattern
for security scans.

---

## Entry points

- `def main()` (line 297)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--db` | SQLite DB path. Default: memory/agent_memory.db or $M3_DATABASE. | `os.environ.get('M3_DATABASE', 'memory/agent_memory.db')` |  | str |  |
| `--run-id` | enrich_run_id to summarize (UUID). | — |  | str |  |
| `--variant` | source_variant to summarize across all runs. | — |  | str |  |
| `--target` | Output file (default: stdout). | — |  | str |  |

---

## Environment variables read

- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `m3_core.paths (seam_backend)`

---

## File dependencies (repo paths referenced)

- `memory/agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
