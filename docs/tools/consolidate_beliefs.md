---
tool: bin/consolidate_beliefs.py
sha1: f87eee2210e9
mtime_utc: 2026-07-19T03:04:59.548388+00:00
generated_utc: 2026-07-19T19:29:22.102932+00:00
private: false
---

# bin/consolidate_beliefs.py

## Purpose

Autonomous episodic->semantic belief consolidation (knowledge-maintenance P4).

Rolls up large groups of episodic `observation` memories into stable, high-order
`belief` memories using the local LLM — the engine is memory_consolidate_impl;
this script is the *trigger and policy* around it. Beliefs link back to their
sources via `consolidates` edges and the sources are soft-deleted (never purged),
so a belief is always reversible and its provenance reconstructable.

Gated by M3_CONSOLIDATION_AUTO (default off): when the flag is unset, this runs in
DRY-RUN regardless of --apply, so a scheduled invocation is a safe no-op until the
operator opts in. Pass --apply AND set M3_CONSOLIDATION_AUTO=1 to actually write.

Scheduled weekly (see crontab.template / install_schedules.py). Protected types
(preference/user_fact/task/plan) are never consolidated — inherited from
memory_consolidate_impl's defaults.

Usage:
    python bin/consolidate_beliefs.py [--apply] [--threshold N] [--stale-days N]
                                      [--source-type observation] [--log-file PATH]

---

## Entry points

- `def main()` (line 100)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--apply` | Write beliefs (requires M3_CONSOLIDATION_AUTO=1); else dry-run. | `False` |  | store_true |  |
| `--threshold` | f'Min group size before consolidating (default {DEFAULT_THRESHOLD}).' | `DEFAULT_THRESHOLD` |  | int |  |
| `--stale-days` | f'Only consolidate items older than N days (default {DEFAULT_STALE_DAYS}).' | `DEFAULT_STALE_DAYS` |  | int |  |
| `--source-type` | f"Episodic source memory type (default '{DEFAULT_SOURCE_TYPE}')." | `DEFAULT_SOURCE_TYPE` |  | str |  |

---

## Environment variables read

- `M3_CONSOLIDATION_AUTO`
- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (add_log_file_arg, setup_task_runtime)`
- `m3_sdk (M3Context, get_governor_pacing)`
- `m3_sdk (_LAST_USER_INTERACTION)`
- `memory_maintenance`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
