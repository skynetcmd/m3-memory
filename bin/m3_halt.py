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
from dataclasses import dataclass, field
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
    # Optional per-role metadata (e.g. a server's listen host/port). Absent for
    # roles that don't record it; readers must tolerate None. Kept out of the
    # required fields so older registry files (no ``extra`` key) still parse.
    extra: dict = field(default_factory=dict)


def _now_iso() -> str:
    # Wall-clock is fine here (registry timestamps, not ordering-critical).
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def register_process(
    role: str,
    engine_root: Optional[str] = None,
    extra: Optional[dict] = None,
) -> Path:
    """Register the current process as a live writer of role ``role``.

    Writes ``PID/<role>.<pid>``. Call once on startup AFTER the process has
    opened its DB connections. Idempotent per (role, pid). Best-effort: a
    registry write failure is logged, never fatal (the process should run even
    if coordination is degraded — fail safe, not loud-fatal, per §3).

    ``extra`` is optional per-role metadata (e.g. a server's ``{"host": ...,
    "port": ...}``) stored under an ``extra`` key so readers can recover it —
    doctor uses the recorded host/port to probe liveness and to restart a dead
    dashboard on its original address. Reserved keys (pid/role/started_at/
    engine_root/protocol) can't be shadowed; ``extra`` is namespaced.
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
        if extra:
            payload["extra"] = dict(extra)
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
            raw_extra = data.get("extra")
            live.append(ProcInfo(
                pid=pid,
                role=str(data.get("role", entry.stem.rsplit(".", 1)[0])),
                started_at=str(data.get("started_at", "")),
                engine_root=str(data.get("engine_root", "")),
                path=entry,
                extra=dict(raw_extra) if isinstance(raw_extra, dict) else {},
            ))
        else:
            _reap(entry)  # dead PID → stale → reap; never counts as a holder
    return live


def _reap(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# Command-line signatures of the autonomous m3 DB-writers, matched substring-wise
# against a process's cmdline. This is the REGISTRY-INDEPENDENT discovery floor:
# a writer from an OLDER m3 version (before the PID registry / HALT protocol
# existed) never wrote a PID/ entry and never polls HALT_m3, so list_live_processes
# can't see it — yet on an UPGRADE it is exactly the process holding the DB open.
# Matching by what the process RUNS (not by a registry it didn't populate) closes
# that bootstrap gap. Keyed by role so the caller can report/-handle uniformly.
# cmdline substrings that identify each writer (the precise, preferred match).
_WRITER_CMDLINE_SIGNATURES = {
    "cognitive-loop": ("m3_cognitive_loop.py",),
    "embed-server": ("embed_server_inproc.py",),
    "mcp": ("mcp-memory", "mcp_proxy.py"),
}
# Fallback process-NAME substrings, used when a process's cmdline is unreadable —
# which happens for an ELEVATED process when the installer runs unprivileged
# (psutil returns an empty cmdline / raises AccessDenied). The name alone can't
# distinguish which python script is running, so a bare-interpreter match is
# reported as the ambiguous "m3?(elevated)" role: enough to STOP and prompt,
# never enough to silently proceed. mcp-memory(.exe) is unambiguous by name.
_WRITER_NAME_SIGNATURES = {
    "mcp": ("mcp-memory",),
}
_INTERPRETER_NAMES = ("python", "pythonw", "python3")


def scan_db_writer_processes(engine_root: Optional[str] = None) -> list[ProcInfo]:
    """Find running m3 DB-writers by COMMAND-LINE signature (with a process-NAME
    fallback for privilege-denied cmdlines), independent of the PID registry.
    Cross-platform via psutil (Windows / Linux / macOS).

    This complements ``list_live_processes`` (which only sees writers of THIS
    protocol version that registered themselves). Use both — union'd by pid — so
    an exclusive op also detects pre-HALT writers on an upgrade.

    Elevated processes: when the installer runs unprivileged, an elevated writer's
    cmdline is not readable (empty / AccessDenied). We do NOT silently skip it —
    a bare interpreter (python/pythonw) whose cmdline we can't read is reported as
    role ``m3?`` with ``elevated=True`` in engine_root marker, so the caller
    surfaces "a process may be a stale elevated m3 writer — can't confirm, can't
    kill without elevation" and prompts/aborts rather than migrating blind. An
    mcp-memory process is name-identifiable even elevated.

    Best-effort: no psutil, or a process vanishing mid-scan → skip that item
    rather than fail (registry + Windows file-lock probe remain as nets).
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001 — no psutil → rely on registry + file-lock probe
        return []
    root = str(_engine_root(engine_root))
    self_pid = os.getpid()
    found: list[ProcInfo] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = proc.info["pid"]
            if pid == self_pid:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            name = (proc.info.get("name") or "").lower()

            # 1. Precise cmdline match (the normal, unprivileged-readable case).
            matched = None
            if cmdline:
                for role, sigs in _WRITER_CMDLINE_SIGNATURES.items():
                    if any(sig in cmdline for sig in sigs):
                        matched = role
                        break
            if matched:
                found.append(ProcInfo(pid=pid, role=matched, started_at="",
                                      engine_root=root, path=Path()))
                continue

            # 2. Name-only fallback for unreadable (typically ELEVATED) cmdlines.
            # Only for NAME-UNAMBIGUOUS writers (mcp-memory): a bare python whose
            # cmdline is denied is NOT reported — most such processes aren't m3,
            # and flooding the prompt with false positives would make it useless.
            # An unprivileged installer fundamentally cannot inspect an elevated
            # process's cmdline OR open files (both hit AccessDenied), so a stale
            # elevated LOOP/EMBED (script name unknowable) may be undetectable
            # here. That residual case is handled at the KILL boundary: attempting
            # to stop any writer that turns out to be elevated fails with a
            # permission error, which the installer surfaces as "re-run elevated"
            # rather than a false success (see _kill_process_* callers).
            if not cmdline:
                for role, sigs in _WRITER_NAME_SIGNATURES.items():
                    if any(sig in name for sig in sigs):
                        found.append(ProcInfo(pid=pid, role=f"{role}(elevated?)",
                                              started_at="", engine_root=root,
                                              path=Path()))
                        break
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:  # noqa: BLE001 — one odd row must not abort the whole scan
            continue
    return found


def elevated_kill_commands(pids: "list[int]") -> "list[str]":
    """The exact command(s) to stop the given PIDs from an ELEVATED shell, for the
    CURRENT OS. Surfaced to the user when the unprivileged installer can't stop an
    elevated/stale m3 writer, so they can clear it and retry — Windows / Linux /
    macOS. Empty list for an empty pid list.

    Windows: an elevated PowerShell/cmd. Linux & macOS: sudo kill (SIGTERM), with
    a SIGKILL escalation line as a fallback."""
    pids = [int(p) for p in pids if int(p) > 0]
    if not pids:
        return []
    joined = " ".join(str(p) for p in pids)
    if os.name == "nt":
        # /T also stops child processes; run from an elevated (Run as administrator) shell.
        return [f"taskkill /F /T {' '.join(f'/PID {p}' for p in pids)}"]
    # POSIX (Linux + macOS): polite TERM first, then KILL if still alive.
    return [
        f"sudo kill {joined}",
        f"sudo kill -9 {joined}   # only if the above didn't stop them",
    ]


def list_all_db_writers(engine_root: Optional[str] = None) -> list[ProcInfo]:
    """Union of registered writers (list_live_processes) and cmdline-discovered
    writers (scan_db_writer_processes), deduplicated by pid. This is the complete
    set an exclusive op must quiesce — covering both current-protocol writers and
    pre-HALT writers from an older version being upgraded over."""
    by_pid: dict[int, ProcInfo] = {}
    for p in list_live_processes(engine_root):
        by_pid[p.pid] = p
    for p in scan_db_writer_processes(engine_root):
        by_pid.setdefault(p.pid, p)  # registry entry wins (richer metadata)
    return list(by_pid.values())


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
    means every registered writer has released its DB handle → quiesced.

    CRUCIALLY, this waits on ``list_all_db_writers`` — the UNION of the registry
    and a cmdline scan — NOT the registry alone. A writer from an older m3 version
    (pre-HALT) never registers and never polls HALT_m3, so it would be invisible
    to a registry-only wait, which would then falsely report ok=True while that
    process keeps writing straight through the migration. The cmdline scan makes
    such a writer visible: it will never "deregister" (it can't), so it stays in
    ``stuck`` past the timeout and the caller's kill/abort path handles it — the
    only safe outcome for a process that doesn't speak the protocol.

    NOTE: the caller must have already called ``set_halt``. This only waits and
    reports; it never kills. The kill decision (prompt a human / --force-quiesce)
    stays with the caller so policy lives at the installer boundary.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        live = list_all_db_writers(engine_root)
        if not live:
            return QuiesceResult(ok=True, stuck=[])
        if time.monotonic() >= deadline:
            return QuiesceResult(ok=False, stuck=live)
        time.sleep(poll)
