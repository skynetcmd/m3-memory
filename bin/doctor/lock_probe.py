"""Single-instance lock probe — is any service's lock wedged, degraded, or flapping?

The autonomous services (cognitive-loop, dashboard, embed-server, mcp-proxy) take
a shared OS-advisory single-instance lock (m3_halt.acquire_single_instance) so
only one of each runs. The lock records a categorical outcome and an append-only
event log (~/.m3/engine/.internal/lock_events.jsonl). This probe surfaces the
states an operator needs to know about (DESIGN §3 fail-loud), which are otherwise
invisible in a JSONL file:

  * DEGRADED runs   — a service started with a CONFIG_ERROR / LOCK_ERROR (the
                      lock file was unwritable, or the OS lock call failed), so it
                      is running WITHOUT single-instance enforcement. A setup/perms
                      problem to fix.
  * CHURN           — many held_by_peer events for one role in a short window = two
                      launchers fighting (a respawn loop / duplicate self-heal).
  * a live holder   — informational: who currently holds each role's lock.

Report-only: never fails the doctor run (returns 0). A wedged/degraded lock is a
warning, not a hard error — the services still run (fail-safe).
"""
from __future__ import annotations

import logging
import os
import sys
import time

logger = logging.getLogger("memory.doctor.lock_probe")

# Roles we expect to be single-instance services (for the "who holds it" view).
_SERVICE_ROLES = ("cognitive-loop", "dashboard", "embed-server", "mcp-proxy")

# Churn threshold: this many held_by_peer events for one role within the window
# below suggests two launchers fighting over the lock (a respawn loop).
_CHURN_COUNT = 5
_CHURN_WINDOW_S = 300.0  # 5 minutes


def _parse_ts(ts: str) -> "float | None":
    """Parse an iso_local_timestamp ('YYYY-MM-DDTHH:MM:SS') to epoch seconds, or
    None. Local-time strptime (matches how the events are stamped)."""
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return None


def run(brief: bool = False) -> int:
    """Report single-instance-lock health. Always returns 0 (report-only)."""
    bin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    try:
        import m3_halt
    except Exception as e:  # noqa: BLE001 — a probe must never crash the doctor
        if brief:
            print("locks: unknown (m3_halt not loadable)")
        else:
            print(f"  status   : could not load m3_halt: {type(e).__name__}: {e}")
        return 0

    events = m3_halt.read_lock_events(limit=1000)

    # Latest outcome per role (last acquired/degraded/held; released → free).
    latest: "dict[str, dict]" = {}
    churn: "dict[str, int]" = {}
    now = time.time()
    for ev in events:
        role = ev.get("role", "?")
        latest[role] = ev
        if ev.get("event") == "held_by_peer":
            ts = _parse_ts(ev.get("ts", ""))
            if ts is not None and (now - ts) <= _CHURN_WINDOW_S:
                churn[role] = churn.get(role, 0) + 1

    degraded = {r: e for r, e in latest.items()
                if e.get("event") in ("config_error", "lock_error")}
    flapping = {r: n for r, n in churn.items() if n >= _CHURN_COUNT}

    if brief:
        parts = []
        if degraded:
            parts.append(f"{len(degraded)} degraded")
        if flapping:
            parts.append(f"{len(flapping)} flapping")
        print("locks: " + ("; ".join(parts) if parts else "ok"))
        return 0

    print()
    print("=== single-instance locks ===")

    if not events:
        print("  no lock events recorded yet (no service has taken a lock, or the "
              "event log is empty).")
        return 0

    # Degraded (the important one — running without enforcement).
    if degraded:
        print("  DEGRADED (running WITHOUT single-instance enforcement):")
        for role, rec in sorted(degraded.items()):
            print(f"    - {role}: {rec.get('event')} — {rec.get('error', '')[:120]}")
        print("    Fix: check that the engine root's .internal/ dir is writable "
              "(M3_ENGINE_ROOT); re-run the service.")
    # Flapping (churn — two launchers fighting).
    if flapping:
        print("  FLAPPING (many contention events — possible duplicate launcher / "
              "respawn loop):")
        for role, n in sorted(flapping.items()):
            print(f"    - {role}: {n} 'already running' events in the last "
                  f"{int(_CHURN_WINDOW_S/60)} min")
        print("    Fix: check the scheduled task / launchd / systemd for duplicate "
              "triggers (MultipleInstances, Boot+Logon).")

    # Informational: current holder per known service role.
    print("  current holders:")
    for role in _SERVICE_ROLES:
        rec = latest.get(role)
        if rec is None:
            print(f"    - {role:14} : (no events)")
        elif rec.get("event") == "acquired":
            loc = ""
            if rec.get("host") and rec.get("port"):
                loc = f" → {rec['host']}:{rec['port']}"
            print(f"    - {role:14} : held by pid {rec.get('pid')}{loc}")
        elif rec.get("event") == "released":
            print(f"    - {role:14} : free (last released by pid {rec.get('pid')})")
        else:
            print(f"    - {role:14} : {rec.get('event')}")

    if not degraded and not flapping:
        print("  status   : OK — no degraded or flapping locks.")
    return 0
