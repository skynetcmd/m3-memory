---
tool: bin/_task_runtime.py
sha1: e0b59238cd68
mtime_utc: 2026-05-14T05:44:27.026165+00:00
generated_utc: 2026-05-23T17:55:29.668863+00:00
private: true
---

# bin/_task_runtime.py

## Purpose

_task_runtime — shared runtime setup for m3-memory scheduled-task entrypoints.

Two jobs, one call (`setup_task_runtime`):

  1. Log redirect. Opens the task's logfile, points sys.stdout/sys.stderr at it
     (captures bare `print()` and uncaught tracebacks) and configures the root
     logger onto the same stream (captures `logging` users). One mechanism
     covers both styles, so callers don't need a shell `>> logfile 2>&1`.

  2. Single-instance lock. A cross-platform PID-file lock keyed by a per-task
     name. If a live duplicate is already running, logs one quiet line
     ("duplicate process (PID nnnn) already running") and exits 0 — no
     traceback, no non-zero code, so Task Scheduler / cron see a clean run.

Why this exists: Windows scheduled tasks used to run `cmd.exe /c "python ...
>> log 2>&1"`. The cmd.exe wrapper drew a focus-stealing console window every
fire. Registering python directly removes the window but also removes the
shell that evaluated the `>>` redirect — so logging moves in-process, here.

The PID-lock logic is lifted from m3_cognitive_loop.py's acquire_lock/
release_lock so all scheduled tasks get the same single-instance guarantee
the cognitive loop already had.

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--log-file` | Write stdout/stderr/logging to this file (scheduled-task mode). Defaults to <repo>/logs/<script>.log. | None |  | str |  |

---

## Environment variables read

- `M3_TASK_LOG_FILE`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `atexit`
- `ctypes`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
