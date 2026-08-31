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


_DEFAULT_LOG_MAX_BYTES = 64 * 1024 * 1024
_LOG_CFG_TTL = 5.0
_log_cfg_cache: dict = {"ts": 0.0, "mtime": None, "max_bytes": None}


def _log_config_path() -> str:
    """`.log_config.json` at the config root (same root as .governor_config.json)."""
    try:
        from m3_sdk import get_m3_config_root

        return os.path.join(get_m3_config_root(), ".log_config.json")
    except Exception:  # noqa: BLE001 — pre-config bootstrap must still rotate
        return os.path.join(
            os.path.expanduser("~"), ".m3", "config", ".log_config.json"
        )


def _log_max_bytes(now: float | None = None) -> int:
    """Rotation cap in bytes. Precedence: config file > env var > default.

    A config FILE, not an env var alone (§3): none of the headless launchers
    inherit a shell environment — the Windows scheduled task that runs the
    cognitive loop, the launchd agent, the `systemd --user` unit each get a bare
    one — so `M3_LOG_MAX_BYTES` set in a terminal is simply absent in the exact
    process whose log grows. Mirrors m3_core.governor._governor_thresholds.

    Re-read only when the file's mtime changes, stat throttled to _LOG_CFG_TTL
    (§4). 0 disables rotation. Never raises.
    """
    import time

    now = now if now is not None else time.time()
    if now - _log_cfg_cache["ts"] >= _LOG_CFG_TTL:
        _log_cfg_cache["ts"] = now
        try:
            mtime = os.stat(_log_config_path()).st_mtime
        except OSError:
            mtime = None  # absent/unreadable -> env + default
        if mtime != _log_cfg_cache["mtime"]:
            _log_cfg_cache["mtime"] = mtime
            cfg: dict = {}
            if mtime is not None:
                try:
                    import json as _json

                    with open(_log_config_path(), encoding="utf-8") as f:
                        cfg = _json.load(f) or {}
                except Exception as e:  # noqa: BLE001 — any parse failure
                    # §3 never silent: a malformed file would otherwise revert
                    # to defaults invisibly, so the operator's tuning is dead
                    # without them knowing.
                    print(
                        f"[m3] WARNING: log config {_log_config_path()} is "
                        f"unreadable/malformed ({e}) — falling back to "
                        f"M3_LOG_MAX_BYTES + default until fixed.",
                        file=sys.__stderr__ or sys.stderr,
                    )
                    cfg = {}
            _log_cfg_cache["max_bytes"] = cfg.get("max_bytes")

    val = _log_cfg_cache["max_bytes"]
    if val is not None:
        try:
            return int(val)
        except (TypeError, ValueError):
            pass  # malformed value -> fall through to env/default
    try:
        return int(os.environ.get("M3_LOG_MAX_BYTES", str(_DEFAULT_LOG_MAX_BYTES)))
    except (TypeError, ValueError):
        return _DEFAULT_LOG_MAX_BYTES


def ensure_log_config() -> str:
    """Create `<config_root>/.log_config.json` with the current effective cap.

    §3 says seed the file idempotently so the knob is DISCOVERABLE: an operator
    who never sees the file cannot know the cap is tunable without reading the
    source. Atomic create, never overwrites an existing file, never raises — a
    write failure just leaves the system on env+defaults. Mirrors
    m3_core.governor.ensure_governor_config.
    """
    path = _log_config_path()
    if os.path.exists(path):
        return path
    try:
        import json as _json

        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "_comment": (
                "Log rotation for m3 daemons. max_bytes: roll <name>.log aside "
                "to <name>.log.1 once it exceeds this size, checked at startup. "
                "0 disables rotation. Env var M3_LOG_MAX_BYTES is a fallback "
                "only — headless launchers (Windows Task Scheduler, launchd, "
                "systemd --user) do not inherit your shell env, so this FILE is "
                "the knob that actually reaches them."
            ),
            "max_bytes": _log_max_bytes(),
        }
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass  # env+defaults still apply; seeding is a convenience, not a bound
    return path


def _rotate_if_oversized(log_path: pathlib.Path) -> None:
    """Roll the log aside at startup if it has grown past the cap.

    Rotation happens HERE, at startup, and not via RotatingFileHandler: the
    daemon logs by pointing sys.stdout/sys.stderr straight at this file
    (_redirect_output), and a live OS handle cannot be rotated out from under
    itself on Windows — the rename fails with EACCES and the handler silently
    keeps appending to the old inode. A startup check is the honest seam: it
    always runs before the handle is opened.

    Sized-based, single generation: keep <name>.1 and drop anything older, so a
    runaway cannot fill the disk. A cognitive-loop bug once produced a 720 MB
    log this way (2026-08-29) with nothing to bound it.

    Never raises — failing to rotate must not stop the daemon from starting.
    """
    cap = _log_max_bytes()
    if cap <= 0:
        return  # explicitly disabled
    try:
        if not log_path.exists() or log_path.stat().st_size < cap:
            return
        prev = log_path.with_suffix(log_path.suffix + ".1")
        if prev.exists():
            prev.unlink()
        log_path.replace(prev)
    except OSError as e:
        # Fail SAFE (a large log beats a daemon that will not start) but never
        # SILENT (§3): this is the moment the only disk-space bound stopped
        # working, and an operator who is not told will find out via a full
        # disk. Logging is not configured yet at this point, so write to the
        # real stderr — which the launcher captures.
        print(
            f"[m3] WARNING: could not rotate oversized log {log_path} "
            f"({type(e).__name__}: {e}). It will keep growing past {cap} bytes "
            f"until this is resolved.",
            file=sys.__stderr__ or sys.stderr,
        )


def _redirect_output(log_path: pathlib.Path, logger_name: str | None) -> None:
    """Point stdout/stderr at the logfile and configure logging onto it."""
    # Bound the log BEFORE opening the handle (see _rotate_if_oversized).
    _rotate_if_oversized(log_path)
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
