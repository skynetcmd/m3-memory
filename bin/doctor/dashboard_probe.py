"""Doctor probe: is the local web dashboard healthy — and self-heal it.

The dashboard (bin/dashboard_server.py) registers itself in the engine root's
``.internal/PID/dashboard.<pid>`` JSON registry (via m3_halt) with the host/port
it bound. This probe reads that registry and does a TWO-STAGE liveness check per
entry:

  1. PROCESS alive?  — m3_halt.pid_is_alive(pid)  (cross-platform, no deps)
  2. PORT serving?   — a TCP connect to the recorded host:port

and classifies each dashboard entry:

  * healthy  (proc alive + port serving)  → report the URL, exit 0.
  * dead     (proc not alive)             → stale registry file. Report; with
                                            --fix, reap it and RESTART on the
                                            recorded host/port.
  * wedged   (proc alive, port not serving)→ half-dead. Report; with --fix, kill
                                            it, reap the file, and RESTART on the
                                            recorded host/port.

Plain `run(brief)` is REPORT-ONLY: it never kills or restarts — it prints the
detected problem and tells the operator to run `m3 doctor --fix`. It also never
bumps the exit code: a stopped dashboard is a supported state (the user simply
hasn't started it), not a degraded fleet, so it nags rather than failing doctor.

`run(brief, fix=True)` performs the kill/reap/restart. A restart re-invokes
`m3 dashboard --host H --port P`, which re-enters the in-process run_dashboard()
and re-registers itself — so there is ONE launch model and the restarted server
is itself supervisable next time.

Report-and-fix, never crashes the doctor.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8088


def _payload_bin() -> str:
    """bin/ dir of this payload (this file is bin/doctor/dashboard_probe.py)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_m3_halt():
    """Import m3_halt from the payload bin/ (dependency-free, like sibling probes)."""
    bin_dir = _payload_bin()
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    import m3_halt  # noqa: E402
    return m3_halt


def _port_serving(host: str, port: int, timeout: float = 2.0) -> bool:
    """True if something accepts a TCP connection at host:port.

    A plain connect is enough to tell "the socket is bound and listening" from
    "the process is alive but the server loop is wedged / not bound" — we don't
    need an HTTP round-trip, and a connect avoids depending on any dashboard
    route staying stable. 0.0.0.0 is probed via loopback (you can't connect TO
    the wildcard address).
    """
    # B104 false positive: "0.0.0.0" here is a string comparison that remaps the
    # wildcard to loopback for an outbound connect probe; this is not a bind-to-all.
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host  # nosec B104
    try:
        with socket.create_connection((probe_host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _kill(pid: int) -> bool:
    """Terminate a wedged dashboard process. Best-effort, cross-platform."""
    try:
        if sys.platform == "win32":
            subprocess.run(  # noqa: S603 — fixed argv
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [fix] could not kill wedged dashboard (pid {pid}): {e}")
        return False


def _restart(host: str, port: int) -> bool:
    """Relaunch the dashboard on host:port, detached and WINDOWLESS.

    Re-invokes `m3 dashboard --host H --port P`, which ITSELF launches the server
    windowless + detached (pythonw / new session) and returns — so this restart
    goes through the ONE supported launch path, re-registering the new PID in the
    .internal/PID registry and leaving no stray window. We just run the CLI
    (blocking briefly while it spawns the detached child) and confirm the port.
    """
    m3_cmd = os.path.join(os.path.dirname(sys.executable), "m3")
    argv = [
        m3_cmd if os.path.exists(m3_cmd) else "m3",
        "dashboard", "--host", host, "--port", str(port),
    ]
    try:
        # `m3 dashboard` detaches the server itself and returns promptly; run it
        # blocking (no window inherited into doctor — it spawns its own detached
        # windowless child). Bounded so a hung CLI can't wedge doctor.
        subprocess.run(  # noqa: S603 — fixed argv, our own CLI
            argv,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=30, check=False,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  [fix] could not relaunch dashboard: {e}")
        return False

    # Give the detached child a moment to bind, then confirm the port serves.
    for _ in range(10):
        if _port_serving(host, port, timeout=1.0):
            print(f"  [fix] dashboard restarted → http://{host}:{port}")
            return True
        time.sleep(0.5)
    print(f"  [fix] relaunched dashboard on {host}:{port} but it is not yet "
          "answering — check the dashboard log.")
    return False


def run(brief: bool = False, fix: bool = False) -> int:
    """Report (and with fix, heal) the dashboard's registry/liveness state.

    Report-only and exit-code-neutral by design: a stopped dashboard is a
    supported state, not a doctor failure. Returns 0 always (the fix path
    reports its own success/failure inline).
    """
    try:
        m3_halt = _load_m3_halt()
    except Exception as e:  # noqa: BLE001 — probe must never crash doctor
        if not brief:
            print(f"[dashboard] skipped (could not load registry: {e})")
        return 0

    # list_live_processes reaps dead-PID and malformed files as a side effect, so
    # a crashed dashboard's stale entry is cleaned up just by reading here.
    try:
        live = m3_halt.list_live_processes()
    except Exception as e:  # noqa: BLE001
        if not brief:
            print(f"[dashboard] skipped (registry read failed: {e})")
        return 0

    entries = [p for p in live if p.role == "dashboard"]

    if not entries:
        # Nothing registered/alive. A dead entry was already reaped above. Stay
        # quiet in brief mode (a stopped dashboard is normal); in verbose mode
        # note how to start it.
        if not brief:
            print("[dashboard] not running — start with:  m3 dashboard")
        return 0

    for p in entries:
        host = str(p.extra.get("host") or _DEFAULT_HOST)
        try:
            port = int(p.extra.get("port") or _DEFAULT_PORT)
        except (TypeError, ValueError):
            port = _DEFAULT_PORT
        url = f"http://{host}:{port}"

        if _port_serving(host, port):
            # Healthy: alive AND serving.
            print(f"[OK] Web Dashboard available at: {url}  (pid {p.pid})")
            continue

        # Alive per the registry but NOT serving the port → wedged/half-dead.
        if not fix:
            print(f"[!] dashboard (pid {p.pid}) registered on {host}:{port} "
                  "but not responding.")
            print("      → run `m3 doctor --fix` to restart it.")
            continue

        # --fix: kill the wedged process, reap its registry file, restart.
        print(f"[fix] dashboard (pid {p.pid}) wedged on {host}:{port} — "
              "killing and restarting...")
        _kill(p.pid)
        try:
            p.path.unlink(missing_ok=True)  # reap the stale registry entry
        except OSError:
            pass
        _restart(host, port)

    return 0
