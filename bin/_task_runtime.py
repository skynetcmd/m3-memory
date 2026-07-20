#!/usr/bin/env python3
"""
_task_runtime — shared runtime setup for m3-memory scheduled-task entrypoints.

Two jobs, one call (`setup_task_runtime`):

  1. Log redirect. Opens the task's logfile, points sys.stdout/sys.stderr at it
     (captures bare `print()` and uncaught tracebacks) and configures the root
     logger onto the same stream (captures `logging` users). One mechanism
     covers both styles, so callers don't need a shell `>> logfile 2>&1`.

  2. Single-instance lock. Delegates to the shared system-wide OS-advisory lock
     (m3_halt.acquire_single_instance) keyed by the per-task name. If a live
     duplicate holds it, logs one quiet line and exits EXIT_ALREADY_RUNNING (the
     fleet-wide "another instance already running" code). Task Scheduler / cron
     ignore the exit code for scheduling, so a re-fire stays a clean no-op.

Why this exists: Windows scheduled tasks used to run `cmd.exe /c "python ...
>> log 2>&1"`. The cmd.exe wrapper drew a focus-stealing console window every
fire. Registering python directly removes the window but also removes the
shell that evaluated the `>>` redirect — so logging moves in-process, here.

The lock now uses the shared m3_halt primitive (an atomic OS advisory lock at
the ENGINE root) instead of a hand-rolled PID file under the CODE dir. That
fixes two bugs the old version had: (1) the PID file lived under the pipx
PAYLOAD dir (REPO_ROOT/memory), which a `pipx upgrade` wipes out from under a
running task; (2) the old check-then-write was NOT atomic, so two simultaneous
fires (Boot+Logon) could both pass — a double secret_rotator / sync_all, etc.
"""

import logging
import os
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def no_window_kwargs() -> dict:
    """Return subprocess kwargs that suppress a console window on Windows.

    Scheduled tasks run via pythonw.exe (no console), but any child process
    they spawn with subprocess.* gets its OWN console window unless told
    otherwise — that is the focus-stealing flash. Spread this into every
    subprocess call a scheduled-task entrypoint makes:

        subprocess.run([...], **no_window_kwargs())

    On POSIX this is an empty dict (CREATE_NO_WINDOW is a Windows-only flag),
    so the call site stays cross-platform.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}

# Module-level guard so a double call (e.g. re-import) is a no-op rather than
# stacking redirects / handlers.
_INITIALIZED = False
# Holds the shared single-instance lock for the task's lifetime (released by the
# lock's own atexit + SIGTERM cleanup). Module-level so it isn't GC'd.
_INSTANCE_LOCK = None


def add_log_file_arg(parser) -> None:
    """Register a `--log-file PATH` option on an argparse parser.

    For entrypoints that already build an ArgumentParser. Scripts without one
    can just pass the path straight to setup_task_runtime().
    """
    parser.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="Write stdout/stderr/logging to this file (scheduled-task mode). "
        "Defaults to <repo>/logs/<script>.log.",
    )


def _resolve_log_file(log_file) -> pathlib.Path:
    """Resolution order: explicit arg -> $M3_TASK_LOG_FILE -> repo/logs/<stem>.log."""
    if log_file:
        resolved = pathlib.Path(log_file)
    elif os.environ.get("M3_TASK_LOG_FILE"):
        resolved = pathlib.Path(os.environ["M3_TASK_LOG_FILE"])
    else:
        stem = pathlib.Path(sys.argv[0]).stem or "task"
        resolved = REPO_ROOT / "logs" / f"{stem}.log"
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _redirect_output(log_path: pathlib.Path, logger_name: str | None) -> None:
    """Point stdout/stderr at the logfile and configure logging onto it."""
    # Line-buffered, utf-8 so non-ASCII log output doesn't crash on cp1252.
    fh = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = fh
    sys.stderr = fh
    # force=True overrides any module-level basicConfig() that ran at import
    # time in the entrypoint or its dependencies.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    if logger_name:
        logging.getLogger(logger_name).setLevel(logging.INFO)


def setup_task_runtime(
    log_file=None,
    lock_name: str | None = None,
    logger_name: str | None = None,
) -> pathlib.Path:
    """Call once as the first statement in a scheduled-task __main__.

    1. Redirects stdout/stderr to `log_file` and configures logging onto it.
    2. If `lock_name` is given, takes the shared single-instance lock; on a live
       duplicate, logs 'duplicate process (PID nnnn) already running' and exits
       EXIT_ALREADY_RUNNING (Task Scheduler / cron ignore the code, so a re-fire
       is a clean no-op). A degraded lock (config/OS error) still lets the task
       run — fail-safe.

    Returns the resolved log file path.

    Calling this before heavy imports do their work ensures their output is
    captured too. Idempotent: a second call is a no-op.
    """
    global _INITIALIZED, _INSTANCE_LOCK
    if _INITIALIZED:
        return _resolve_log_file(log_file)

    log_path = _resolve_log_file(log_file)
    _redirect_output(log_path, logger_name)
    _INITIALIZED = True

    if lock_name:
        from m3_sdk import acquire_or_exit
        _log = logging.getLogger(logger_name or lock_name)
        _INSTANCE_LOCK = acquire_or_exit(
            lock_name,
            on_already_running=lambda o: _log.info(
                "duplicate process (PID %s) already running; exiting",
                o.pid if o else "?"),
        )
        if not _INSTANCE_LOCK.acquired:
            _log.warning("%s: single-instance lock DEGRADED (%s) — running "
                         "without enforcement", lock_name, _INSTANCE_LOCK.status.value)

    return log_path
