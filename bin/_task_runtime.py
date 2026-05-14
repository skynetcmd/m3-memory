#!/usr/bin/env python3
"""
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
"""

import atexit
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
_LOCK_PATH: pathlib.Path | None = None


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


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID is currently running. Cross-platform."""
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_INFORMATION = 0x0400
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            # PermissionError means the process exists but is owned by someone
            # else — still "alive" for our purposes.
            return isinstance(sys.exc_info()[1], PermissionError)
        except OSError:
            return False


def _acquire_lock(lock_name: str) -> bool:
    """Acquire a single-instance PID lock. Returns True on success.

    On a live duplicate, returns False (caller logs + exits quietly). A stale
    PID file (process no longer alive, or unparseable) is silently reclaimed.
    """
    global _LOCK_PATH
    lock_path = REPO_ROOT / "memory" / f"{lock_name}.pid"

    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text().strip())
            if old_pid != os.getpid() and _pid_alive(old_pid):
                return False
        except (ValueError, OSError):
            # Unparseable / unreadable PID file -> treat as stale, reclaim it.
            pass

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(str(os.getpid()))
    _LOCK_PATH = lock_path
    atexit.register(_release_lock)
    return True


def _release_lock() -> None:
    """Remove our PID file on exit, but only if it's still ours."""
    global _LOCK_PATH
    if _LOCK_PATH is None:
        return
    try:
        if _LOCK_PATH.exists():
            current = int(_LOCK_PATH.read_text().strip())
            if current == os.getpid():
                _LOCK_PATH.unlink()
    except (ValueError, OSError):
        pass
    finally:
        _LOCK_PATH = None


def setup_task_runtime(
    log_file=None,
    lock_name: str | None = None,
    logger_name: str | None = None,
) -> pathlib.Path:
    """Call once as the first statement in a scheduled-task __main__.

    1. Redirects stdout/stderr to `log_file` and configures logging onto it.
    2. If `lock_name` is given, acquires a single-instance PID lock; on a live
       duplicate, logs 'duplicate process (PID nnnn) already running' and
       exits 0 quietly.

    Returns the resolved log file path.

    Calling this before heavy imports do their work ensures their output is
    captured too. Idempotent: a second call is a no-op.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return _resolve_log_file(log_file)

    log_path = _resolve_log_file(log_file)
    _redirect_output(log_path, logger_name)
    _INITIALIZED = True

    if lock_name:
        if not _acquire_lock(lock_name):
            # The duplicate's PID, for the log line.
            other = "?"
            try:
                other = (REPO_ROOT / "memory" / f"{lock_name}.pid").read_text().strip()
            except OSError:
                pass
            logging.getLogger(logger_name or lock_name).info(
                "duplicate process (PID %s) already running; exiting", other
            )
            sys.exit(0)

    return log_path
