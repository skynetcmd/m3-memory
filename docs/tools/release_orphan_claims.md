---
tool: bin/release_orphan_claims.py
sha1: c740f32763df
mtime_utc: 2026-07-19T03:04:59.630082+00:00
generated_utc: 2026-07-19T19:29:22.824704+00:00
private: false
---

# bin/release_orphan_claims.py

## Purpose

release_orphan_claims — safely release stuck in_progress enrichment_groups rows.

Use after a worker process crashes mid-batch and leaves rows claimed but
unfinalized. Provides three filtering modes to avoid sniping live workers'
claims:

  --run-id <id>       Release all in_progress rows belonging to this run.
                      Safe ONLY when you've confirmed the run's process is
                      dead (tasklist | grep python).

  --older-than <min>  Release rows whose claimed_at is older than N minutes.
                      Heuristic for "definitely abandoned." Default cutoff
                      should exceed the longest legitimate in_progress
                      window — typically batch-poll cadence × max-poll-count.
                      For our Anthropic batches: ~60-120 min is the sweet spot.

  --dry-run           Show what would be released without committing.

  --skip-qps-done     Defensive: do NOT release a row if its
                      question_pipeline_state.result is already in done_text /
                      done_empty / failed. This prevents the "release-back-to-
                      pending" reverse-drift bug where a previously-terminal
                      qps row gets re-flagged because of a worker crash.

Default mode requires explicit user confirmation.

Usage:
    python bin/release_orphan_claims.py --db memory/your-corpus.db \
        --older-than 120 --skip-qps-done

    python bin/release_orphan_claims.py --db memory/your-corpus.db \
        --run-id <enrichment_runs.id>

---

## Entry points

- `def main()` (line 47)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--db` | Path to the SQLite DB. | — |  | str |  |
| `--run-id` | Release rows where enrich_run_id = this value. | None |  | str |  |
| `--older-than` | Release rows where claimed_at older than N minutes. | None |  | int |  |
| `--all` | Release ALL in_progress rows (DANGEROUS — only use when no live workers exist). | `False` |  | store_true |  |
| `--skip-qps-done` | Skip rows whose question_pipeline_state already says done_text/done_empty/failed (prevents reverse-drift). | `False` |  | store_true |  |
| `--dry-run` | Preview without committing. | `False` |  | store_true |  |
| `-y`, `--yes` | Skip the interactive confirm prompt. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (active_database)`
- `memory_core`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `memory.backends (dialect)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
