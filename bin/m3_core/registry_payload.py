"""registry_payload — the canonical schema for m3's process-coordination files.

Every on-disk coordination file under the engine root's ``.internal/`` directory
(the PID registry entries written by ``m3_halt.register_process`` and the
single-instance ``*.lock`` files written by ``m3_halt.acquire_single_instance``)
shares ONE JSON shape, defined here so writers and readers — including a future
dashboard lock-management / review panel — can never drift:

    {
      "pid":         int,          # owner process id
      "role":        str,          # e.g. "dashboard", "cognitive-loop"
      "started_at":  str,          # ISO-8601 local wall-clock, human-readable
      "create_time": float | None, # process create time (epoch s) — identity
      "engine_root": str,          # which engine root this file belongs to
      "protocol":    int,          # PROTOCOL_VERSION at write time
      "extra":       dict          # OPTIONAL namespaced per-role metadata
    }

``create_time`` is the second half of process identity: a bare PID is unsafe
because the OS reuses PIDs (and on Windows ``GetExitCodeProcess`` returns
STILL_ACTIVE(259), which a process that legitimately exited with 259 also
reports). Pairing the PID with its create time lets a reader tell the ORIGINAL
owner from a process that merely reused the PID.

The reserved keys cannot be shadowed by ``extra`` (which is a namespaced
sub-dict for per-role metadata such as ``{"host": ..., "port": ...}``).

This module is pure — ``build_payload`` takes the pid/create_time as arguments
rather than reading the current process — so it is trivially testable and has no
side effects. The runtime "stamp the current process" wrappers live in
``m3_halt`` (which calls ``os.getpid()`` / ``psutil`` and passes the values in).

Re-exported through ``m3_sdk`` so any consumer reaches the schema as
``from m3_sdk import parse_payload, ProcInfo, build_payload``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from m3_core.runtime import iso_local_timestamp

# Bump when the on-disk payload shape changes incompatibly. Owned here (the
# schema module), re-exported by m3_halt for back-compat with existing importers.
PROTOCOL_VERSION = 1

# Keys the schema owns; ``extra`` metadata can never shadow one of these.
RESERVED_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"pid", "role", "started_at", "create_time", "engine_root", "protocol", "extra"}
)


@dataclass(frozen=True)
class ProcInfo:
    """The parsed, structured view of a coordination file. Consumers (doctor,
    dashboard, the services) receive this — never a raw dict — so the schema is
    enforced at one boundary (§3: structured returns, not free-form dicts)."""

    pid: int
    role: str
    started_at: str
    engine_root: str
    path: Path
    # Optional per-role metadata (e.g. a server's listen host/port). Absent for
    # roles that don't record it; readers must tolerate an empty dict. Kept out
    # of the required fields so older files (no ``extra`` key) still parse.
    extra: dict = field(default_factory=dict)
    # Process create time (epoch s) at write, or None for older/opaque entries.
    # Used with the pid for reuse-safe identity; a reader compares it to the
    # live process's create time.
    create_time: Optional[float] = None


def build_payload(
    role: str,
    engine_root: str,
    *,
    pid: int,
    create_time: Optional[float],
    extra: Optional[dict] = None,
) -> dict:
    """Build the canonical payload dict for a process. PURE — the caller supplies
    ``pid`` and ``create_time`` (the runtime reads them via os.getpid / psutil),
    so this has no side effects and is directly testable. ``extra`` is namespaced
    under the ``extra`` key so it can never shadow a reserved field.
    """
    payload: dict[str, Any] = {
        "pid": int(pid),
        "role": str(role),
        "started_at": iso_local_timestamp(),
        "create_time": create_time,
        "engine_root": str(engine_root),
        "protocol": PROTOCOL_VERSION,
    }
    if extra:
        # Drop any key that would collide with a reserved field (defense in depth).
        payload["extra"] = {k: v for k, v in dict(extra).items()
                            if k not in RESERVED_PAYLOAD_KEYS}
    return payload


def parse_payload(data: Any, path: Path) -> Optional[ProcInfo]:
    """Turn a decoded coordination-file dict into a ``ProcInfo``, or ``None`` if
    it is malformed / implausible. The ONE parser both the registry and the lock
    reader go through (§3: single validation boundary, empty→None never a raise).

    ``path`` is the on-disk file the dict came from (carried into ProcInfo so
    callers can reap/inspect it). Returns None on: not-a-dict, missing/invalid
    pid. A missing ``role`` falls back to the file stem so a hand-forged legacy
    entry still identifies.
    """
    if not isinstance(data, dict):
        return None
    try:
        pid = int(data["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    raw_extra = data.get("extra")
    raw_ct = data.get("create_time")
    try:
        create_time = float(raw_ct) if raw_ct is not None else None
    except (TypeError, ValueError):
        create_time = None
    return ProcInfo(
        pid=pid,
        role=str(data.get("role", path.stem)),
        started_at=str(data.get("started_at", "")),
        engine_root=str(data.get("engine_root", "")),
        path=path,
        extra=dict(raw_extra) if isinstance(raw_extra, dict) else {},
        create_time=create_time,
    )
