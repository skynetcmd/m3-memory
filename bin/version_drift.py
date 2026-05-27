"""B17: detect version drift between installed package and running MCP server.

When a user runs `pip install -U m3-memory` while the MCP server is
running, the on-disk package version changes but the long-lived
mcp-memory.exe process keeps serving the OLD code (cached in its
imported modules) until restart. The next time mcp-memory boots, this
module records the version that's now running; subsequent boots compare
and warn if the user's installed wheel is newer than what was last
recorded.

The recorded state lives in ``~/.m3-memory/version_state.json``:

    {
      "last_boot_version": "2026.5.4.7",
      "last_boot_at":      "2026-05-27T15:42:11Z",
      "last_boot_pid":     12345
    }

The warning fires on EITHER of:

1. ``installed_version != last_boot_version`` AND installed version is
   newer (sorted lexically — package uses YYYY.M.D.N so this is correct
   ordering).
2. The recorded PID is still alive AND it's running a different
   version than the import does.

Surface: a single ``check_and_record()`` call from memory_bridge.py at
startup. Prints a single-line WARNING to stderr; never aborts.

No new dependencies. Uses only stdlib (pathlib, json, datetime, os).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("memory.version_drift")

STATE_FILENAME = "version_state.json"


def _state_dir() -> Path:
    """Per-user state dir at ``~/.m3-memory/``.

    Deliberately does NOT honor ``M3_MEMORY_ROOT`` — that env var
    redirects the installer's payload-clone root (a developer convenience
    for testing against alternate repo locations). Honoring it for
    version-drift state would put the file in the same repo whose
    upgrade we're trying to detect, which defeats the purpose.
    """
    return Path.home() / ".m3-memory"


def _state_path() -> Path:
    return _state_dir() / STATE_FILENAME


def _current_version() -> str:
    """Resolve m3-memory package version. Returns '0.0.0+local' on failure."""
    try:
        from m3_memory import __version__  # type: ignore[attr-defined]
        return str(__version__) or "0.0.0+local"
    except Exception:
        return "0.0.0+local"


def _load_state() -> dict:
    p = _state_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        _state_dir().mkdir(parents=True, exist_ok=True)
        _state_path().write_text(
            json.dumps(state, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as e:
        logger.debug(f"version-drift state write failed: {e}")


def check_and_record() -> dict:
    """Call at MCP server startup.

    Compares the just-imported package version against the previous boot
    record. Returns a dict of the comparison findings (also recorded to
    disk).

    Emits a WARNING log line if the version drifted (caller already has
    a logger pointed wherever it wants).
    """
    current = _current_version()
    prior = _load_state()
    prior_version = prior.get("last_boot_version")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pid = os.getpid()

    findings = {
        "current_version": current,
        "prior_version": prior_version,
        "drifted": False,
        "prior_pid": prior.get("last_boot_pid"),
        "prior_pid_alive": False,
    }

    if prior_version and prior_version != current:
        # Lexical compare works because m3-memory uses YYYY.M.D.N.
        if current > prior_version:
            findings["drifted"] = True
            logger.warning(
                "version drift detected: this server is %s but last boot was "
                "%s. If the older server is still running (PID %s), restart "
                "it to pick up the changes (kill it OR /mcp restart in your "
                "agent).",
                current, prior_version, prior.get("last_boot_pid", "?"),
            )

    # Detect any other live mcp-memory processes from previous boots
    # (best-effort, Windows only — Unix rename-in-place doesn't have the
    # same problem).
    if prior.get("last_boot_pid") and prior.get("last_boot_pid") != pid:
        findings["prior_pid_alive"] = _pid_is_alive(prior["last_boot_pid"])
        if findings["prior_pid_alive"] and findings["drifted"]:
            logger.warning(
                "PID %s from the previous boot is STILL RUNNING. Two MCP "
                "servers serving the same agent will produce inconsistent "
                "results. Kill PID %s.",
                prior["last_boot_pid"], prior["last_boot_pid"],
            )

    _save_state({
        "last_boot_version": current,
        "last_boot_at": now,
        "last_boot_pid": pid,
    })

    return findings


def _pid_is_alive(pid: int) -> bool:
    """Cross-platform liveness probe. Best-effort; returns False on any error."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            # Windows: tasklist filter by PID
            import subprocess
            out = subprocess.run(
                ["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return f'"{pid}"' in out.stdout or f",{pid}," in out.stdout
        else:
            # Unix: kill(pid, 0) signals nothing but raises on no-such-process
            os.kill(pid, 0)
            return True
    except (OSError, subprocess.TimeoutExpired, Exception):
        return False
