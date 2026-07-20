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

import enum
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# The coordination-file schema is a shared contract in m3_core (so writers,
# readers, and a future dashboard lock panel can't drift). m3_halt owns the
# runtime ACTIONS (stamp current process, O_EXCL acquire, registry dir); the
# schema (ProcInfo, build/parse, reserved keys, PROTOCOL_VERSION) lives there.
# Re-exported here (ProcInfo, PROTOCOL_VERSION) for back-compat with existing
# `m3_halt.ProcInfo` / `m3_halt.PROTOCOL_VERSION` importers.
from m3_core.registry_payload import (  # noqa: F401
    PROTOCOL_VERSION,
    RESERVED_PAYLOAD_KEYS,
    ProcInfo,
    build_payload,
    parse_payload,
)
from m3_core.runtime import iso_local_timestamp

logger = logging.getLogger(__name__)

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
    """True if a process with ``pid`` currently exists. Cross-platform, no deps.

    NOTE on identity: liveness alone can't tell the ORIGINAL owner from a process
    that REUSED the pid (and on Windows GetExitCodeProcess==STILL_ACTIVE(259) is
    ambiguous). For a stale-vs-owner decision, pair this with a create_time match
    (see _proc_create_time / _read_lock_owner). This function answers only
    "does *a* process with this pid exist right now".
    """
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


# Max allowed difference (seconds) between a registry entry's recorded
# create_time and the live process's create_time for them to be the SAME process.
# Used by the PID REGISTRY (_pid_is_live_owner / list_live_processes) to detect a
# reused PID. (The single-instance LOCK no longer needs this — it uses an OS
# advisory lock the kernel releases on death, so there's no stale-lock reclaim to
# guard; this is purely for the registry's liveness reads.) Tight enough to catch
# a reused PID, loose enough to absorb create_time read-rounding (~10ms on Linux).
_CREATE_TIME_MATCH_TOLERANCE_S = 0.1


def _pid_is_live_owner(pid: int, recorded_create_time: "float | None") -> bool:
    """True iff ``pid`` is alive AND is the SAME process that recorded
    ``recorded_create_time`` — i.e. the pid was not reused.

    This is the reuse-safe identity check both the PID registry
    (``list_live_processes``) and the single-instance lock (``_read_lock_owner``)
    go through, so they can't disagree. Pairing liveness with create_time closes
    two gaps that bare ``_pid_is_alive`` has: OS PID reuse, and the Windows
    ``GetExitCodeProcess``/STILL_ACTIVE(259) ambiguity (a process that exited
    with code 259 reads as alive). If ``recorded_create_time`` is None (older
    entry that predates the create_time field) we fall back to liveness-only —
    no worse than before, and the entry gets rewritten with create_time on the
    owner's next registration.
    """
    if not _pid_is_alive(pid):
        return False
    if recorded_create_time is None:
        return True  # legacy entry without create_time → liveness-only fallback
    live_ct = _proc_create_time(pid)
    if live_ct is None:
        return True  # can't read create_time (perm) → don't wrongly reap a live pid
    return abs(live_ct - recorded_create_time) <= _CREATE_TIME_MATCH_TOLERANCE_S


# Public alias: cross-module callers (pg_sync, doctor, services) should use the
# unprefixed name rather than reaching a `_`-private symbol across the module
# boundary (§12c: formalize the seam, don't let callers depend on internals).
pid_is_alive = _pid_is_alive


# ──────────────────────────────────────────────────────────────────────────
# PID registry
# ──────────────────────────────────────────────────────────────────────────
# ProcInfo + the payload schema/parser are re-exported from m3_core.registry_payload
# at the top of this module. What lives HERE is the runtime action: read THIS
# process's pid + create_time and stamp a payload via the shared pure builder.


def _build_registry_payload(
    role: str,
    engine_root: Optional[str],
    extra: Optional[dict] = None,
    *,
    pid: Optional[int] = None,
) -> dict:
    """Stamp a coordination payload for the current (or given) process, using the
    shared schema builder. This is the runtime seam: it reads pid + create_time
    (side effects) and delegates the SHAPE to m3_core.registry_payload.build_payload
    so the on-disk schema has one owner. Used by both register_process and the
    single-instance lock."""
    _pid = os.getpid() if pid is None else pid
    return build_payload(
        role,
        str(_engine_root(engine_root)),
        pid=_pid,
        create_time=_proc_create_time(_pid),
        extra=extra,
    )


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
    dashboard on its original address. The on-disk shape is the canonical
    registry payload (see ``m3_core.registry_payload`` — ``build_payload`` /
    ``RESERVED_PAYLOAD_KEYS``); reserved keys can't be shadowed; ``extra`` is
    namespaced.
    """
    role = _safe_role(role)
    pid = os.getpid()
    pdir = _pid_dir(engine_root)
    try:
        pdir.mkdir(parents=True, exist_ok=True)
        entry = pdir / f"{role}.{pid}"
        payload = _build_registry_payload(role, engine_root, extra, pid=pid)
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
        except (OSError, json.JSONDecodeError):
            _reap(entry)  # unreadable → reap (fail safe)
            continue
        info = parse_payload(data, entry)
        if info is None:
            # Malformed / implausible entry: can't trust it, reap it (fail safe).
            # A bad pid must never reach os.kill via a stuck-holder kill.
            _reap(entry)
            continue
        # Reuse-safe: alive AND the same process that registered (create_time).
        # A dead pid OR a reused pid (create_time mismatch) is stale → reap, so a
        # writer that crashed and whose pid was recycled never counts as a live
        # holder (closes the STILL_ACTIVE(259) / PID-reuse gap for the registry +
        # the quiesce protocol that reads it).
        if _pid_is_live_owner(info.pid, info.create_time):
            live.append(info)
        else:
            _reap(entry)
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


def kill_stale_daemons(
    engine_root: Optional[str] = None,
    *,
    timeout: float = 8.0,
) -> "list[dict]":
    """Terminate every running m3 DB-writer for this engine root. Call this at the
    START of an install/upgrade so no OLD-version daemon survives across the swap:
    a leftover writer runs the pre-upgrade code against the just-upgraded DB/payload
    (schema drift, two generations racing the same tables, the "duplicate loop that
    won't die on reinstall" footgun). The single-instance lock stops a NEW dup from
    a fresh start; it does NOT reach back and kill a daemon that was already running
    — that is this function's job.

    Discovery is the full union (registry + cmdline scan via list_all_db_writers),
    so it catches BOTH current-protocol writers and pre-HALT writers from the
    version being replaced. Kill is by PRECISE PID only — never a name/substring
    sweep (the 2026-06-30 substring-kill incident) — the pid comes from a matched
    m3 writer signature, and we never target our own pid or our parent chain.

    Returns one result dict per targeted pid: {pid, role, killed: bool, error}.
    Empty list when nothing was running (§3 — a list, never None). Best-effort and
    idempotent: a pid that vanishes mid-kill counts as killed; an AccessDenied
    (elevated writer, unprivileged installer) is reported killed=False with the
    error so the caller can surface "re-run elevated", never a false success."""
    self_pid = os.getpid()
    # Never kill our own ancestry — an installer launched BY the loop (unusual, but
    # possible in a self-update) must not saw off the branch it is sitting on.
    protected = {self_pid}
    try:
        protected.add(os.getppid())
    except Exception:  # noqa: BLE001 — getppid missing/odd → just protect self
        pass

    results: list[dict] = []
    for w in list_all_db_writers(engine_root):
        if w.pid in protected:
            continue
        entry: dict = {"pid": w.pid, "role": w.role, "killed": False, "error": None}
        try:
            if not _pid_is_alive(w.pid):
                entry["killed"] = True  # already gone → the desired end state
                results.append(entry)
                continue
            if os.name == "nt":
                import subprocess
                cp = subprocess.run(["taskkill", "/F", "/T", "/PID", str(w.pid)],
                                    capture_output=True, text=True)
                # /T also kills the child tree — a pythonw launcher stub + its real
                # worker die together, so no orphaned half-generation is left.
                if cp.returncode != 0 and _pid_is_alive(w.pid):
                    entry["error"] = (cp.stderr or cp.stdout or "taskkill failed").strip()
            else:
                import signal as _signal
                os.kill(w.pid, _signal.SIGTERM)
                for _ in range(int(timeout * 10)):
                    if not _pid_is_alive(w.pid):
                        break
                    time.sleep(0.1)
                if _pid_is_alive(w.pid):
                    os.kill(w.pid, _signal.SIGKILL)  # type: ignore[attr-defined]  # POSIX-only; this branch is os.name != "nt"
            # Confirm death (bounded) and reap its registry entry so a later
            # list_live_processes doesn't resurrect a ghost.
            for _ in range(int(timeout * 10)):
                if not _pid_is_alive(w.pid):
                    break
                time.sleep(0.1)
            entry["killed"] = not _pid_is_alive(w.pid)
            if not entry["killed"] and not entry["error"]:
                entry["error"] = "still alive after kill+timeout"
        except PermissionError as e:
            entry["error"] = f"access denied (elevated writer?): {e}"
        except Exception as e:  # noqa: BLE001 — one bad pid must not abort the sweep
            entry["error"] = str(e)
        _log_lock_event("killed_stale" if entry["killed"] else "kill_failed",
                        w.role, None, victim_pid=w.pid, by_pid=self_pid)
        results.append(entry)
    return results


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
        "created_at": iso_local_timestamp(),
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


# ──────────────────────────────────────────────────────────────────────────
# Single-instance lock — race-free "only one of this role runs" primitive,
# built on an OS ADVISORY LOCK (fcntl.flock on POSIX, msvcrt.locking on Windows).
#
# WHY an OS advisory lock and not a PID file / port probe:
#   * A port/health probe is check-then-act (TOCTOU) — two near-simultaneous
#     launches both see "not serving" and both proceed (dashboard pile-up,
#     2026-07-20).
#   * A PID file (O_EXCL + write pid) is atomic to CREATE, but the file OUTLIVES
#     a crashed holder — so it needs stale-detection, PID-liveness, PID-reuse
#     guards (create_time), and reclaim, each with its own failure mode
#     (reclaim-failed-on-dead-owner, Windows STILL_ACTIVE(259) ambiguity, ...).
#   * An OS advisory lock is held by the KERNEL for the open fd. The OS releases
#     it automatically when the process dies — crash, SIGKILL, taskkill /F,
#     power loss. So there is NO stale lock, NO dead-PID ambiguity, NO reclaim.
#     "Held" means "a LIVE process holds it", by construction. This is what the
#     mature cross-platform libraries (fasteners, portalocker) converged on; we
#     use the stdlib primitives directly (NO new dependency — fcntl and msvcrt
#     are stdlib) to keep the local-first/offline posture (§1).
#
# The lock file ALSO carries a JSON sidecar (the shared registry-payload schema:
# pid/role/host/port/create_time) so doctor / the dashboard can SHOW who holds
# the lock — but the sidecar is INFORMATIONAL; the OS lock is the mutex. A stale
# sidecar (holder died) is harmless because the OS lock it describes is already
# released, so the next acquirer simply re-locks and rewrites it.
#
# CONTRACT (per DESIGN_PHILOSOPHIES §3 fail-loud/never-silent): acquire returns a
# structured LockResult with a CATEGORICAL status, so downstream can act per
# cause (not a boolean). See LockStatus + LockResult below.
# ──────────────────────────────────────────────────────────────────────────

# ── Exit codes: a HIGH-FIDELITY signal for a process that did not acquire ──────
# The exit code itself carries the CATEGORY so a supervisor / operator / script
# reading $? knows WHY the process didn't run (they need different actions), not
# just "it exited non-zero". Base 4 (1/2/3 are taken: argparse usage=2,
# embed_server GGUF dim-mismatch=3, generic=1), so 4/5/6 avoid all collisions.
#
#   4 ALREADY_RUNNING  — a LIVE peer holds the lock. BENIGN/expected. Supervisors
#                        treat 4 as a clean exit (systemd SuccessExitStatus=4,
#                        launchd KeepAlive:Crashed-only) → do NOT respawn.
#   5 LOCK_CONFIG_ERROR— the lock FILE couldn't be opened/created (unwritable
#                        .internal dir, bad engine root, perms). A SETUP problem —
#                        an operator should fix perms/root. NOT suppressed by the
#                        supervisor (a restart won't help, but it must be visible,
#                        not silently swallowed like "already running").
#   6 LOCK_ERROR       — the OS lock call failed UNEXPECTEDLY (not would-block).
#                        Rare; doctor-worthy. Also visible (not suppressed).
#
# NOTE: for CONFIG/LOCK errors the DEFAULT service behavior is to run DEGRADED
# (fail-safe, §3) rather than exit — these codes are for a caller that chooses to
# bail (or for acquire_or_exit(strict=True)). Only ALREADY_RUNNING is a routine
# "loser" exit.
EXIT_ALREADY_RUNNING = 4
EXIT_LOCK_CONFIG_ERROR = 5
EXIT_LOCK_ERROR = 6

_LOCK_SUFFIX = ".lock"

# Locks THIS process currently holds, keyed by (role, resolved_engine_root). Lets
# a re-entrant acquire by the same process return its EXISTING handle
# (idempotent) instead of failing on its own held fd. Cleared on release().
# Guarded by _HELD_LOCKS_MUTEX because acquire() and release() run on different
# threads (the loop dispatches passes via asyncio.to_thread), so the
# check-then-set of the re-entrant fast-path + the dict writes must be atomic.
_HELD_LOCKS: "dict[tuple[str, str], InstanceLock]" = {}
_HELD_LOCKS_MUTEX = threading.RLock()

# Append-only lock-event audit log. Records every ownership change (acquired /
# released / stolen) and contention (held_by_peer) / degraded outcome, so the
# whole ownership history per role is queryable — a dashboard lock panel, doctor,
# or churn debugging ("who held it at time T; how often does it flap"). §6 audit
# trail. Best-effort + bounded: a log failure NEVER affects the lock outcome (the
# OS lock is the mutex; this is observability), and the file is size-capped so it
# can't grow unbounded (§10 hygiene).
_LOCK_EVENTS_FILENAME = "lock_events.jsonl"
_LOCK_EVENTS_MAX_BYTES = 1_000_000  # ~1 MB; rotate (keep one .1 backup) past this


def _lock_events_path(engine_root: Optional[str] = None) -> Path:
    return _internal_dir(engine_root) / _LOCK_EVENTS_FILENAME


def _log_lock_event(event: str, role: str, engine_root: Optional[str],
                    **fields) -> None:
    """Append one JSONL lock event. Best-effort — swallow ALL errors so logging
    can never affect a lock acquire/release. Rotates when the file exceeds
    _LOCK_EVENTS_MAX_BYTES (keeps a single .1 backup) to stay bounded."""
    try:
        p = _lock_events_path(engine_root)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Rotate if too big (cheap stat; only acts at the threshold).
        try:
            if p.exists() and p.stat().st_size > _LOCK_EVENTS_MAX_BYTES:
                backup = p.with_suffix(p.suffix + ".1")
                os.replace(str(p), str(backup))  # atomic; drops the older .1
        except OSError:
            pass
        rec = {"ts": iso_local_timestamp(), "event": event, "role": role,
               "pid": os.getpid()}
        rec.update(fields)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec))
            fh.write("\n")
    except Exception:  # noqa: BLE001 — observability must never break the lock
        pass


def read_lock_events(engine_root: Optional[str] = None,
                     role: Optional[str] = None,
                     limit: int = 200) -> "list[dict]":
    """Return recent lock events (newest last), optionally filtered by ``role``.
    For doctor / the dashboard lock panel. Reads the current file only (rotated
    .1 backup is not merged — recent history is what matters). Empty list (never
    None, §3) on any error. ``limit`` caps the returned rows (most recent)."""
    p = _lock_events_path(engine_root)
    out: "list[dict]" = []
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if role is not None and rec.get("role") != role:
                    continue
                out.append(rec)
    except OSError:
        return []
    return out[-limit:] if limit and limit > 0 else out


class LockStatus(str, enum.Enum):
    """Categorical outcome of an acquire attempt. Different causes need different
    downstream handling — a caller must NOT collapse every non-ACQUIRED into
    "already running" (a wedged lock or a config error is not a live peer)."""

    ACQUIRED = "acquired"        # WON — we hold the OS lock; run.
    REENTRANT = "reentrant"      # this process already held this role; same handle.
    HELD_BY_PEER = "held_by_peer"  # a LIVE peer holds the OS lock → exit
    #                                EXIT_ALREADY_RUNNING (by construction the
    #                                holder is alive; the OS freed it if it died).
    CONFIG_ERROR = "config_error"  # the lock FILE could not be opened/created —
    #                                unwritable/missing .internal dir, bad engine
    #                                root, permissions. A SETUP problem, not a
    #                                lock contention. Fail safe: run degraded.
    LOCK_ERROR = "lock_error"    # the OS lock call itself failed for an
    #                                unexpected reason (not "already held"). Rare;
    #                                surfaced for doctor. Fail safe: run degraded.

    @property
    def exit_code(self) -> int:
        """The high-fidelity process exit code for a caller that CHOOSES to exit
        on this status. ACQUIRED/REENTRANT have no exit (0 = success). The
        loser/error codes are distinct so $? tells the operator WHICH problem."""
        return {
            LockStatus.ACQUIRED: 0,
            LockStatus.REENTRANT: 0,
            LockStatus.HELD_BY_PEER: EXIT_ALREADY_RUNNING,   # 4
            LockStatus.CONFIG_ERROR: EXIT_LOCK_CONFIG_ERROR,  # 5
            LockStatus.LOCK_ERROR: EXIT_LOCK_ERROR,           # 6
        }[self]


# Statuses under which the service SHOULD run (has, at least, a usable handle).
_RUNNABLE = frozenset({LockStatus.ACQUIRED, LockStatus.REENTRANT,
                       LockStatus.CONFIG_ERROR, LockStatus.LOCK_ERROR})


def _proc_create_time(pid: int) -> "float | None":
    """The process create time (epoch seconds) for pid, or None if unknown.
    Cross-platform via psutil (a hard dependency). Recorded in the sidecar so
    doctor/dashboard can show it; NOT load-bearing for the lock (the OS lock is)."""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return None
    try:
        return float(psutil.Process(pid).create_time())
    except Exception:  # noqa: BLE001
        return None


def _lock_path(role: str, engine_root: Optional[str] = None) -> Path:
    return _internal_dir(engine_root) / f"{_safe_role(role)}{_LOCK_SUFFIX}"


def _os_lock_exclusive_nb(fd: int) -> "tuple[bool, Optional[Exception]]":
    """Take a NON-BLOCKING exclusive OS advisory lock on ``fd``.

    Returns (got_lock, error):
      * (True, None)          — we hold the lock.
      * (False, None)         — a live peer holds it (would-block).
      * (False, exc)          — an unexpected lock error (surface it).

    Cross-platform, stdlib only: fcntl.flock (POSIX) / msvcrt.locking (Windows).
    The kernel releases the lock automatically when this fd is closed or the
    process dies — so there is nothing stale to clean up.
    """
    if os.name == "nt":
        # Windows only. msvcrt has no POSIX stub, so a type checker running on
        # Linux (CI is ubuntu) flags locking/LK_* as missing — but this branch
        # never runs there. Suppress the platform-stub false positive (§12c).
        import msvcrt  # type: ignore[import-not-found]
        try:
            # Lock 1 byte, non-blocking. Raises OSError if another handle holds it.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            return True, None
        except OSError as e:
            # EACCES/EDEADLOCK == already locked by another process (would-block).
            import errno
            if e.errno in (errno.EACCES, errno.EDEADLOCK, errno.EDEADLK):
                return False, None
            return False, e
    else:
        # POSIX only. fcntl has no Windows stub, so a type checker running on
        # Windows flags flock/LOCK_* as missing — but this branch never runs
        # there (guarded by os.name == "nt" above). Suppress the platform-stub
        # false positive rather than let it read as a real error (§12c).
        import fcntl  # type: ignore[import-not-found]
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            return True, None
        except (BlockingIOError, InterruptedError):
            return False, None  # held by a live peer
        except OSError as e:
            import errno
            if e.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return False, None
            return False, e


def _os_unlock(fd: int) -> None:
    """Release the OS advisory lock on ``fd`` (best-effort; the OS also releases
    on close/death, so a failure here is not fatal)."""
    try:
        if os.name == "nt":
            import msvcrt  # type: ignore[import-not-found]  # Windows only (see above)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            except OSError:
                pass
        else:
            import fcntl  # type: ignore[import-not-found]  # POSIX only (see above)
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
            except OSError:
                pass
    except Exception:  # noqa: BLE001 — never raise from a release path
        pass


@dataclass
class InstanceLock:
    """A held single-instance lock — the passable handle. The OS holds the lock
    for the open ``fd``'s lifetime; ``release()`` (idempotent) unlocks + closes +
    removes the sidecar. Also a context manager. Held for the process lifetime."""

    role: str
    pid: int
    path: Path
    fd: int
    status: "LockStatus"
    engine_root: Optional[str] = None
    _released: bool = field(default=False, repr=False)

    @property
    def acquired(self) -> bool:
        """True iff this is a REAL held OS lock (status ACQUIRED/REENTRANT). False
        for a DEGRADED handle (CONFIG_ERROR/LOCK_ERROR) where the service runs but
        single-instance is NOT enforced — callers can log/branch on this."""
        return self.status in (LockStatus.ACQUIRED, LockStatus.REENTRANT)

    def release(self) -> None:
        """Unlock, close the fd, and remove the sidecar file (best-effort,
        idempotent). The OS would release the lock on close/death anyway; this is
        the tidy path."""
        if self._released:
            return
        self._released = True
        with _HELD_LOCKS_MUTEX:
            _HELD_LOCKS.pop((self.role, str(_engine_root(self.engine_root))), None)
        if self.fd >= 0:
            _os_unlock(self.fd)
            try:
                os.close(self.fd)
            except OSError:
                pass
            # Remove the readable owner file only if it still describes US (never
            # delete one a live successor rewrote after re-locking). The lock file
            # itself is left in place — it's just the mutex target; the OS lock is
            # already released by the close above.
            try:
                op = _owner_sidecar_path(self.path)
                data = json.loads(op.read_text(encoding="utf-8"))
                if int(data.get("pid", -1)) == self.pid:
                    op.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pass
            _log_lock_event("released", self.role, self.engine_root)

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def __del__(self):  # last-resort net; explicit release preferred
        try:
            self.release()
        except Exception:  # noqa: BLE001
            pass


@dataclass
class LockResult:
    """Structured, categorical result of acquire_single_instance. Callers switch
    on ``status`` (LockStatus). ``lock`` is present for every RUNNABLE status
    (ACQUIRED/REENTRANT/CONFIG_ERROR/LOCK_ERROR); ``owner`` is set only for
    HELD_BY_PEER (the live holder's sidecar info, if readable)."""

    status: "LockStatus"
    lock: "Optional[InstanceLock]" = None
    owner: "Optional[ProcInfo]" = None

    @property
    def runnable(self) -> bool:
        """True iff the service should proceed to run (won, re-entrant, or a
        degraded-but-fail-safe error). False only for HELD_BY_PEER."""
        return self.status in _RUNNABLE

    @property
    def should_exit_already_running(self) -> bool:
        """True iff the caller should exit EXIT_ALREADY_RUNNING (a LIVE peer holds
        the lock)."""
        return self.status is LockStatus.HELD_BY_PEER


def _register_lock_cleanup(lock: "InstanceLock") -> None:
    """Register best-effort lock cleanup on process exit.

    The OS releases the advisory lock on process death regardless — this just
    removes the informational sidecar tidily and unlocks on a GRACEFUL stop.
    Three layers: atexit (clean shutdown), a SIGTERM handler (graceful supervisor
    stop — atexit does NOT run on a signal), and (uncovered by design) SIGKILL /
    taskkill /F, where the OS drops the lock and the sidecar is overwritten by the
    next acquirer. Fail-safe: never crash the service or clobber a pre-existing
    SIGTERM handler (we chain to it)."""
    import atexit
    atexit.register(lock.release)
    try:
        import signal
        import threading
        if threading.current_thread() is not threading.main_thread():
            return  # signal handlers install only from the main thread
        prev = signal.getsignal(signal.SIGTERM)

        def _on_sigterm(signum, frame):
            try:
                lock.release()
            except Exception:  # noqa: BLE001
                pass
            if callable(prev):
                prev(signum, frame)
            else:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                os.kill(os.getpid(), signum)

        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError, AttributeError, RuntimeError):
        pass  # best-effort; atexit + OS-release remain the nets


def _owner_sidecar_path(lock_path: Path) -> Path:
    """The readable owner-identity file that sits BESIDE the OS-locked lock file.

    Why a separate file: on Windows the process holding the lock file open blocks
    other processes from READING it (PermissionError), so a peer couldn't recover
    "who holds it" for the 'already running (pid N)' message. The mutex is the OS
    lock on ``<role>.lock``; the identity is this always-readable ``<role>.owner``
    (written by the winner, never locked). A stale owner file (holder died) is
    harmless — the OS lock it describes is already released."""
    return lock_path.with_suffix(lock_path.suffix + ".owner")


def _read_sidecar_owner(path: Path, engine_root: Optional[str]) -> "Optional[ProcInfo]":
    """Best-effort: identify who holds the lock, for a human-readable "already
    running (pid N)" message. INFORMATIONAL only — the OS lock is the authority on
    "is it held". Reads the always-readable sibling owner file (never the locked
    lock file itself). Returns None if absent/unreadable (the peer holds the OS
    lock regardless)."""
    owner_path = _owner_sidecar_path(path)
    try:
        data = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parse_payload(data, owner_path)


def acquire_single_instance(
    role: str,
    *,
    engine_root: Optional[str] = None,
    extra: Optional[dict] = None,
    steal: bool = False,
    timeout: float = 0.0,
) -> "LockResult":
    """Acquire the single-instance OS advisory lock for ``role``. Returns a
    structured LockResult with a CATEGORICAL status (see LockStatus):

      * ACQUIRED    — won the OS lock; result.lock is the held handle. Run.
      * REENTRANT   — this process already held it; result.lock is the SAME
                      handle (idempotent). Run. Never exit on this.
      * HELD_BY_PEER— a LIVE peer holds it (the OS freed it if the holder died,
                      so this is never a dead process). result.owner is the
                      holder's sidecar info if readable. Caller exits
                      EXIT_ALREADY_RUNNING.
      * CONFIG_ERROR— the lock FILE couldn't be opened/created (unwritable dir,
                      bad engine root, perms). Not contention — a setup problem.
                      Fail safe (§3): result.lock is a degraded handle (run
                      anyway, single-instance NOT enforced); surfaced for doctor.
      * LOCK_ERROR  — the OS lock call failed unexpectedly (not would-block).
                      Rare. Fail safe: degraded handle, surfaced for doctor.

    Use ``result.runnable`` (proceed?) and ``result.should_exit_already_running``
    for the common branch, or ``acquire_or_exit`` for the standard service main.

    ``timeout`` (default 0 = non-blocking one-shot): if > 0, WAIT up to that many
    seconds for a HELD_BY_PEER lock to free (polling), instead of returning
    immediately — for a caller that wants to QUEUE behind the current holder
    (e.g. a periodic task that should run after the prior run finishes) rather
    than skip. CONFIG/LOCK errors return immediately (waiting can't help them).

    THREAD-SAFETY: acquire and release may run on different threads (the loop
    dispatches passes via asyncio.to_thread). fcntl.flock is per-PROCESS on Linux
    (two threads could BOTH satisfy it), so the whole acquire is serialized per
    process under _HELD_LOCKS_MUTEX — one thread attempts a given role at a time,
    and the re-entrant check + dict write are atomic with the OS-lock attempt.

    steal=True (destructive, §6-gated): if a LIVE peer holds it, kill it (SIGTERM
    / taskkill) and retry. Explicit 'restart' flows only; every steal is logged.
    """
    role = _safe_role(role)
    my_pid = os.getpid()
    root = str(_engine_root(engine_root))
    path = _lock_path(role, engine_root)
    key = (role, root)

    def _try_once() -> "LockResult":
        """One acquire attempt. MUST be called holding _HELD_LOCKS_MUTEX so the
        re-entrant check, the per-process OS-lock attempt, and the _HELD_LOCKS
        write are atomic together."""
        # RE-ENTRANT: this process already holds a live handle for this role.
        existing = _HELD_LOCKS.get(key)
        if existing is not None and not existing._released and existing.acquired:
            return LockResult(status=LockStatus.REENTRANT, lock=existing)

        # 1. Open (create) the lock file. Failure = CONFIG/permissions, not
        #    contention → degraded (fail safe), distinct status.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            logger.warning("m3_halt: cannot open lock file %s: %s — running DEGRADED "
                           "(single-instance NOT enforced; check .internal perms/root)",
                           path, e)
            _log_lock_event("config_error", role, engine_root, error=str(e)[:200])
            return LockResult(
                status=LockStatus.CONFIG_ERROR,
                lock=InstanceLock(role=role, pid=my_pid, path=path, fd=-1,
                                  status=LockStatus.CONFIG_ERROR, engine_root=engine_root),
            )

        # 2. Try the OS advisory lock (non-blocking; one retry to cover a steal).
        for _attempt in range(2):
            got, err = _os_lock_exclusive_nb(fd)
            if got:
                # Won. Write owner identity to the SEPARATE readable owner file
                # (a peer can't read the locked lock file on Windows).
                try:
                    payload = _build_registry_payload(role, engine_root, extra, pid=my_pid)
                    op = _owner_sidecar_path(path)
                    tmp = op.with_suffix(op.suffix + f".{my_pid}.tmp")
                    tmp.write_text(json.dumps(payload), encoding="utf-8")
                    os.replace(str(tmp), str(op))
                except OSError as e:
                    logger.warning("m3_halt: owner-sidecar write failed for %s: %s "
                                   "(lock still held)", path, e)
                lock = InstanceLock(role=role, pid=my_pid, path=path, fd=fd,
                                    status=LockStatus.ACQUIRED, engine_root=engine_root)
                _HELD_LOCKS[key] = lock
                _register_lock_cleanup(lock)
                _log_lock_event("acquired", role, engine_root,
                                **{k: v for k, v in (extra or {}).items()
                                   if k in ("host", "port")})
                return LockResult(status=LockStatus.ACQUIRED, lock=lock)

            if err is not None:
                logger.warning("m3_halt: OS lock error on %s: %s — running DEGRADED",
                               path, err)
                try:
                    os.close(fd)
                except OSError:
                    pass
                _log_lock_event("lock_error", role, engine_root, error=str(err)[:200])
                return LockResult(
                    status=LockStatus.LOCK_ERROR,
                    lock=InstanceLock(role=role, pid=my_pid, path=path, fd=-1,
                                      status=LockStatus.LOCK_ERROR, engine_root=engine_root),
                )

            # would-block: a LIVE peer holds it.
            owner = _read_sidecar_owner(path, engine_root)
            if steal and _attempt == 0:
                _steal_lock(owner, path, fd)
                continue
            try:
                os.close(fd)
            except OSError:
                pass
            return LockResult(status=LockStatus.HELD_BY_PEER, owner=owner)

        # steal retry exhausted: still held.
        try:
            os.close(fd)
        except OSError:
            pass
        return LockResult(status=LockStatus.HELD_BY_PEER,
                          owner=_read_sidecar_owner(path, engine_root))

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        with _HELD_LOCKS_MUTEX:
            res = _try_once()
        # Only HELD_BY_PEER is worth waiting on (config/lock errors won't clear by
        # waiting; ACQUIRED/REENTRANT are done). Poll until the deadline.
        if res.status is not LockStatus.HELD_BY_PEER or time.monotonic() >= deadline:
            if res.status is LockStatus.HELD_BY_PEER:
                _log_lock_event("held_by_peer", role, engine_root,
                                owner_pid=(res.owner.pid if res.owner else None))
            return res
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def acquire_or_exit(
    role: str,
    *,
    engine_root: Optional[str] = None,
    extra: Optional[dict] = None,
    on_already_running=None,
    strict: bool = False,
) -> "InstanceLock":
    """Convenience for a service main: acquire the single-instance lock, or exit
    with a HIGH-FIDELITY code. Collapses the categorical result so EVERY caller
    handles outcomes identically (not hand-rolled per service):

      * ACQUIRED / REENTRANT      → return the held handle (exit 0 territory).
      * HELD_BY_PEER              → print "already running (pid N)" and
                                    sys.exit(EXIT_ALREADY_RUNNING=4). If
                                    ``on_already_running(owner)`` is given it is
                                    called first (e.g. to log the URL).
      * CONFIG_ERROR / LOCK_ERROR → DEFAULT: return the DEGRADED handle (run
                                    anyway, fail-safe §3; acquire already logged).
                                    With ``strict=True``: exit the status's
                                    distinct code (5 / 6) instead — for a caller
                                    that would rather bail loudly than run a
                                    process with single-instance unenforced.

    So the exit code a loser emits is category-specific: 4 (live peer), and with
    strict=True 5 (lock file/config) or 6 (OS lock error). Returns an InstanceLock
    in every non-exiting case.
    """
    res = acquire_single_instance(role, engine_root=engine_root, extra=extra)
    if res.status is LockStatus.HELD_BY_PEER:
        owner = res.owner
        pid = owner.pid if owner is not None else "?"
        if on_already_running is not None:
            try:
                on_already_running(owner)
            except Exception:  # noqa: BLE001 — never let a message hook block exit
                pass
        else:
            print(f"{role} already running (pid {pid}); exiting.", flush=True)
        sys.exit(res.status.exit_code)  # 4
    if strict and res.status in (LockStatus.CONFIG_ERROR, LockStatus.LOCK_ERROR):
        # Caller opted to bail on a broken lock rather than run degraded. The
        # distinct code (5/6) tells the operator whether it's a config/perms
        # problem or an unexpected OS lock failure.
        print(f"{role}: single-instance lock unavailable ({res.status.value}); "
              f"exiting.", flush=True)
        sys.exit(res.status.exit_code)  # 5 or 6
    # RUNNABLE — ACQUIRED/REENTRANT hold the OS lock; CONFIG/LOCK_ERROR degraded.
    return res.lock  # type: ignore[return-value]  # always set for these statuses


def _steal_lock(owner: "Optional[ProcInfo]", path: Path, our_fd: int) -> None:
    """Kill the LIVE lock holder so we can take over. Destructive — only reached
    via acquire_single_instance(steal=True). Precise: kills exactly the sidecar's
    recorded pid (never a name/cmdline match — the 2026-06-30 substring incident),
    and only if a sidecar pid is readable. Audited. The OS releases the victim's
    advisory lock when it dies, so our caller's retry can then take it."""
    if owner is None:
        logger.warning("m3_halt: STEAL requested for %s but no readable owner "
                       "sidecar — cannot identify a pid to kill; not stealing.",
                       path.name)
        return
    logger.warning("m3_halt: STEAL — killing live lock holder role=%s pid=%s to "
                   "take over %s", owner.role, owner.pid, path.name)
    _log_lock_event("stolen", owner.role, None,
                    victim_pid=owner.pid, by_pid=os.getpid())
    try:
        if os.name == "nt":
            import subprocess
            subprocess.run(["taskkill", "/F", "/PID", str(owner.pid)],
                           capture_output=True, check=False)
        else:
            import signal as _signal
            os.kill(owner.pid, _signal.SIGTERM)
    except Exception as e:  # noqa: BLE001
        logger.warning("m3_halt: steal kill of pid %s failed: %s", owner.pid, e)
    for _ in range(20):
        if not _pid_is_alive(owner.pid):
            break
        time.sleep(0.1)
