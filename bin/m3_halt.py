"""
m3_halt.py — cooperative quiesce protocol for DB-exclusive operations.

An exclusive operation (schema migration, backup, gdpr_forget, doctor --repair)
must not run while an autonomous m3 writer — the cognitive loop, embed server,
or MCP server — holds a WAL-mode DB open, or it risks a torn WAL / silently
wrong state. Killing writers mid-write is itself what tears the WAL, and
elevated/scheduled writers can't always be stopped without admin.

Instead we coordinate through two files under the engine root's ``.internal/``
directory (design: docs/design/HALT_PROTOCOL.md):

  * ``PID/<role>.<pid>`` — a directory with one file per live writer. Each writer
    registers itself on startup and deregisters on clean exit. A directory of
    per-process files (not one shared registry file) means no shared-write races
    and one crash can't corrupt another writer's entry. A file whose PID is dead
    (or reused by a newer process) is stale and reaped on read.

  * ``HALT_m3`` — the quiesce semaphore. An exclusive-op author writes it before
    touching the DBs; writers poll it, checkpoint+close their DB connections, and
    spin-wait until it clears. It is stamped with the owner's PID and self-voids
    if that owner dies, so a crashed installer can never freeze writers forever.

This module is pure-stdlib and cross-platform (Windows / macOS / Linux). It is
imported from ``bin/`` the same way m3_sdk / chatlog_config are; it reuses
``m3_sdk.get_m3_engine_root()`` for path resolution.

Granularity note: there is ONE master switch (HALT_m3). ``halt_is_active`` takes
a ``role`` argument so per-role switches (HALT_<role>) are an addable extension,
but none are shipped — see the "Granularity" section of the design doc for why.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
_HALT_FILENAME = "HALT_m3"
_PID_DIRNAME = "PID"
# Master target token. Per-role targets (HALT_<role>) are the documented but
# unshipped extension point — set_halt only accepts "*" today.
_TARGET_ALL = "*"


def _safe_role(role: str) -> str:
    """Reject a role that isn't a plain identifier.

    ``role`` becomes part of a filename (``PID/<role>.<pid>``); a value with a
    path separator or ``..`` could escape the registry dir. Roles are internal
    constants today, but validating here keeps the seam safe for future callers
    (§12c: guard the footgun, don't shrug at it). Allow letters/digits/-/_ only.
    """
    if not role or not all(c.isalnum() or c in "-_" for c in role):
        raise ValueError(f"m3_halt: invalid role {role!r} "
                         "(letters, digits, '-' and '_' only)")
    return role


# ──────────────────────────────────────────────────────────────────────────
# Path resolution — everything hangs off the engine root's .internal/ dir so
# the protocol is scoped to the same DBs the writers actually open.
# ──────────────────────────────────────────────────────────────────────────
def _engine_root(engine_root: Optional[str] = None) -> Path:
    if engine_root:
        return Path(engine_root)
    # Resolve via the canonical m3_sdk helper (M3_ENGINE_ROOT > M3_MEMORY_ROOT/
    # engine > ~/.m3/engine). Imported lazily so this module has no import-time
    # dependency on bin/ being on sys.path yet.
    from m3_sdk import get_m3_engine_root
    return Path(get_m3_engine_root())


def _internal_dir(engine_root: Optional[str] = None) -> Path:
    return _engine_root(engine_root) / ".internal"


def _pid_dir(engine_root: Optional[str] = None) -> Path:
    return _internal_dir(engine_root) / _PID_DIRNAME


def _halt_path(engine_root: Optional[str] = None) -> Path:
    return _internal_dir(engine_root) / _HALT_FILENAME


# ──────────────────────────────────────────────────────────────────────────
# Liveness — cross-platform PID probe with a start-time reuse guard.
# ──────────────────────────────────────────────────────────────────────────
def _pid_is_alive(pid: int) -> bool:
    """True if a process with ``pid`` currently exists. Cross-platform, no deps."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows: OpenProcess via ctypes; STILL_ACTIVE means running.
        import ctypes  # local import keeps non-Windows clean

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                STILL_ACTIVE = 259
                return exit_code.value == STILL_ACTIVE
            return True  # couldn't read exit code but the handle opened → alive
        finally:
            kernel32.CloseHandle(handle)
    # POSIX: signal 0 probes existence without delivering a signal.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


# ──────────────────────────────────────────────────────────────────────────
# PID registry
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ProcInfo:
    pid: int
    role: str
    started_at: str
    engine_root: str
    path: Path


def _now_iso() -> str:
    # Wall-clock is fine here (registry timestamps, not ordering-critical).
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def register_process(role: str, engine_root: Optional[str] = None) -> Path:
    """Register the current process as a live writer of role ``role``.

    Writes ``PID/<role>.<pid>``. Call once on startup AFTER the process has
    opened its DB connections. Idempotent per (role, pid). Best-effort: a
    registry write failure is logged, never fatal (the process should run even
    if coordination is degraded — fail safe, not loud-fatal, per §3).
    """
    role = _safe_role(role)
    pid = os.getpid()
    pdir = _pid_dir(engine_root)
    try:
        pdir.mkdir(parents=True, exist_ok=True)
        entry = pdir / f"{role}.{pid}"
        payload = {
            "pid": pid,
            "role": role,
            "started_at": _now_iso(),
            "engine_root": str(_engine_root(engine_root)),
            "protocol": PROTOCOL_VERSION,
        }
        entry.write_text(json.dumps(payload), encoding="utf-8")
        return entry
    except OSError as e:
        logger.warning("m3_halt: could not register process %s.%s: %s", role, pid, e)
        return pdir / f"{role}.{pid}"


def deregister(role: str, engine_root: Optional[str] = None) -> None:
    """Remove this process's registry entry. Best-effort (atexit/signal safe)."""
    role = _safe_role(role)
    entry = _pid_dir(engine_root) / f"{role}.{os.getpid()}"
    try:
        entry.unlink(missing_ok=True)
    except OSError as e:
        logger.debug("m3_halt: deregister %s failed (non-fatal): %s", entry.name, e)


def list_live_processes(engine_root: Optional[str] = None) -> list[ProcInfo]:
    """Return live registered writers for this engine root, reaping stale files.

    A registry file is stale (and unlinked) if its PID is not alive. Returns an
    empty list when nothing is registered — never None (§3).
    """
    pdir = _pid_dir(engine_root)
    if not pdir.is_dir():
        return []
    live: list[ProcInfo] = []
    for entry in pdir.iterdir():
        if not entry.is_file():
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
            pid = int(data["pid"])
            if pid <= 0:
                raise ValueError(f"non-positive pid {pid}")
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            # Malformed / implausible entry: can't trust it, reap it (fail safe).
            # A bad pid must never reach os.kill via a stuck-holder kill.
            _reap(entry)
            continue
        if _pid_is_alive(pid):
            live.append(ProcInfo(
                pid=pid,
                role=str(data.get("role", entry.stem.rsplit(".", 1)[0])),
                started_at=str(data.get("started_at", "")),
                engine_root=str(data.get("engine_root", "")),
                path=entry,
            ))
        else:
            _reap(entry)  # dead PID → stale → reap; never counts as a holder
    return live


def _reap(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────────────
# HALT semaphore
# ──────────────────────────────────────────────────────────────────────────
def set_halt(owner: str, reason: str, engine_root: Optional[str] = None,
             targets: str = _TARGET_ALL) -> Path:
    """Raise the quiesce semaphore. Only ``targets="*"`` (all writers) is
    implemented — the per-role extension point is documented but unshipped.

    Stamps the file with this process's PID so a writer can void it if the owner
    dies (self-clearing on crash). Returns the semaphore path.
    """
    if targets != _TARGET_ALL:
        raise ValueError(
            f"m3_halt.set_halt: only targets={_TARGET_ALL!r} is supported "
            f"(per-role HALT is an unshipped extension point); got {targets!r}"
        )
    path = _halt_path(engine_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "owner_pid": os.getpid(),
        "owner": owner,
        "reason": reason,
        "created_at": _now_iso(),
        "protocol": PROTOCOL_VERSION,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def clear_halt(engine_root: Optional[str] = None) -> None:
    """Lower the semaphore so paused writers resume. Best-effort/idempotent."""
    try:
        _halt_path(engine_root).unlink(missing_ok=True)
    except OSError as e:
        logger.warning("m3_halt: could not clear HALT_m3: %s", e)


def halt_is_active(engine_root: Optional[str] = None, role: Optional[str] = None) -> bool:
    """True if writers of ``role`` should pause right now.

    Honors ONLY a semaphore whose owner is still alive: a HALT_m3 left by a
    crashed exclusive-op author self-voids (and is reaped) so it can never freeze
    writers forever. A malformed file is warned about and treated as ABSENT —
    a corrupt semaphore must neither silently pause nor silently un-pause (§3).

    ``role`` is accepted for the per-role extension point; today every role gates
    only on the master switch.
    """
    path = _halt_path(engine_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning("m3_halt: could not read HALT_m3 (%s) — treating as inactive", e)
        return False
    try:
        data = json.loads(raw)
        owner_pid = int(data["owner_pid"])
    except (ValueError, KeyError, json.JSONDecodeError):
        logger.warning("m3_halt: malformed HALT_m3 — treating as inactive")
        return False
    if not _pid_is_alive(owner_pid):
        # Owner is gone → self-void → reap so the next reader is clean.
        logger.info("m3_halt: HALT_m3 owner pid=%s is dead — voiding stale halt", owner_pid)
        _reap(path)
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────
# Quiesce orchestration (exclusive-op side)
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class QuiesceResult:
    ok: bool
    stuck: list[ProcInfo]


def wait_for_quiesce(engine_root: Optional[str] = None, timeout: float = 30.0,
                     poll: float = 0.5) -> QuiesceResult:
    """Wait up to ``timeout`` s for the PID registry to empty.

    Invariant (see the writer contract in HALT_PROTOCOL.md): a writer that honors
    HALT ``deregister``s while paused and re-``register``s on resume, so the
    registry means "who is holding the DB right now". An empty registry therefore
    means every writer has released its DB handle → quiesced. A writer still
    listed after the timeout is either not honoring HALT (crashed/wedged/an old
    version predating the protocol) or genuinely mid-task.

    NOTE: the caller must have already called ``set_halt``. This only waits and
    reports; it never kills. The kill decision (prompt a human / --force-quiesce)
    stays with the caller so policy lives at the installer boundary.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        live = list_live_processes(engine_root)
        if not live:
            return QuiesceResult(ok=True, stuck=[])
        if time.monotonic() >= deadline:
            return QuiesceResult(ok=False, stuck=live)
        time.sleep(poll)
