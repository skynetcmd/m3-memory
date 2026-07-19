---
tool: bin/distill_procedures.py
sha1: bc880ad92c41
mtime_utc: 2026-07-19T03:04:59.553134+00:00
generated_utc: 2026-07-19T19:29:22.176606+00:00
private: false
---

# bin/distill_procedures.py

## Purpose

Autonomous procedural distillation (tasks → reusable `procedure` memories).

Rolls up successful (completed) task runs — a task plus its step/result
memories — into reusable `procedure` memories using a pluggable, local-first
(cloud-capable) model. The engine is memory_distill_procedures_impl; this script
is the *trigger and policy* around it. Procedures link back to their source
memories via `distills_from` edges, and — unlike belief consolidation — the
sources are PRESERVED (never soft-deleted): a procedure augments history, it
doesn't replace it.

Model selection (M3_DISTILL_MODEL): unset/"slm" → the local `procedure_local`
SLM profile (sovereign default); "llm" → largest local model; any other value →
a profile name (another local model, or a cloud endpoint via a
`backend: anthropic|openai` profile). Local-first by default, cloud by config.

Gated by M3_DISTILL_AUTO (default off): when the flag is unset, this runs in
DRY-RUN regardless of --apply, so a scheduled/loop invocation is a safe no-op
until the operator opts in. Pass --apply AND set M3_DISTILL_AUTO=1 to write.

Usage:
    python bin/distill_procedures.py [--apply] [--threshold N] [--stale-days N]
                                     [--max-procedures N] [--log-file PATH]

---

## Entry points

- `def main()` (line 93)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--apply` | Write procedures (requires M3_DISTILL_AUTO=1); else dry-run. | `False` |  | store_true |  |
| `--threshold` | f'Min completed tasks before distilling (default {DEFAULT_THRESHOLD}).' | `DEFAULT_THRESHOLD` |  | int |  |
| `--stale-days` | f'Only distill tasks completed > N days ago (default {DEFAULT_STALE_DAYS}).' | `DEFAULT_STALE_DAYS` |  | int |  |
| `--max-procedures` | f'Max procedures written per run (default {DEFAULT_MAX_PROCEDURES}).' | `DEFAULT_MAX_PROCEDURES` |  | int |  |

---

## Environment variables read

- `M3_DATABASE`
- `M3_DISTILL_AUTO`

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
