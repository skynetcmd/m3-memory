#!/usr/bin/env python3
"""m3 cognitive-loop watchdog — progress-based self-heal for all three OSes.

WHY THIS EXISTS (2026-08-09)
The loop was down ~4h after an upgrade and nothing noticed. Three failure modes;
the platform supervisors cover only the first two, and only on some platforms:

  1. CLEAN EXIT. An upgrade raises HALT_m3; the loop checkpoints and exits 0.
     macOS shipped KeepAlive={Crashed:true}, which by design ignores a clean
     exit, so the loop stayed dead. Linux (Restart=always) and Windows (the
     ONSTART task's self-heal Repetition) were never exposed to this; the plist
     is now KeepAlive=true, bringing macOS to parity.
  2. CRASH. Covered everywhere: KeepAlive / Restart=always / task repetition.
  3. WEDGED BUT ALIVE. Livelocked work-gate, stuck pass, blocked on a DB lock.
     The process exists, so every supervisor on every platform is satisfied and
     the loop does nothing forever. NOTHING covers this — it is why this file
     exists, and it is why the check is PROGRESS-based (heartbeat), not
     liveness-based (pid exists).

DESIGN (§2 modularity, §3 fail-safe, §5 effectiveness)
  * Detection is OS- and backend-agnostic: a heartbeat file the loop stamps at
    each cycle boundary. Only the RESTART ACTION is per-OS, isolated in
    _restart_backend(). Nothing here touches a database, so SQLite vs
    PostgreSQL is irrelevant to it by construction.
  * Restart goes through the platform supervisor (launchctl kickstart /
    systemctl --user restart / schtasks /End+/Run), never by spawning the loop
    directly — two writers on one WAL DB is precisely what the single-instance
    lock and the halt protocol exist to prevent.
  * HALT_m3-aware via m3_halt.halt_is_active(), the shared protocol reader (it
    already voids a halt whose owner died). Re-parsing HALT_m3 here would let
    this drift from the protocol it must honor. Restarting into an exclusive op
    is the torn-WAL case the protocol exists to prevent.
  * Degrade, never guess: a MISSING heartbeat is "unknown", not "dead" — an
    upgrade predating the heartbeat would otherwise cause an endless restart
    loop. Falls back to the m3_halt PID registry for liveness.
  * Import-fragile by necessity, so imports are guarded: if the venv is
    half-upgraded and m3_sdk/m3_halt won't import, the watchdog still resolves
    roots from the documented env vars and keeps working. It is least useful
    exactly when it is most needed otherwise.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_BIN = Path(__file__).resolve().parent
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

# This runs unattended every 5 min under a scheduled task; on Windows each
# subprocess spawn would get its OWN console window (a focus-stealing flash)
# unless CREATE_NO_WINDOW is set. Guarded import so a half-upgraded payload
# degrades to the local equivalent rather than taking the watchdog down.
try:
    from _task_runtime import no_window_kwargs
except Exception:  # pragma: no cover - fallback if _task_runtime unavailable
    def no_window_kwargs() -> dict:
        # getattr, not subprocess.CREATE_NO_WINDOW: the attribute is Windows-only
        # and a direct reference fails mypy on the Linux CI runner.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        return {"creationflags": flags} if flags else {}

# Same role string the loop registers under (m3_cognitive_loop._HALT_ROLE).
_HALT_ROLE = "cognitive-loop"

_LAUNCHD_LABEL = "com.m3memory.cognitiveloop"
_SYSTEMD_UNIT = "m3-cognitive-loop.service"
_WINDOWS_TASK = "AgentOS_CognitiveLoop"

# Miss this many cycles before declaring the loop wedged. 3 leaves room for a
# slow pass (entity extraction on a large backlog) to finish without a false
# restart. Mirrors the tolerance the Windows task repetition already assumes
# ("a stalled heartbeat is tolerable for ~30 min", install_schedules).
_MISSED_CYCLES = 3
_MIN_STALE_S = 900          # floor, for very short --interval values
_MIN_RESTART_GAP_S = 600    # backoff, so a crash-on-startup is not a fork bomb


def _os_name() -> str:
    """WMI-safe OS name. platform.system() hangs on a WMI query on
    Py3.14/Windows; os.name/sys.platform are constants. Same idiom as
    install_schedules._os_name()."""
    if os.name == "nt":
        return "Windows"
    if sys.platform == "darwin":
        return "Darwin"
    return "Linux"


def _roots() -> tuple[Path, Path]:
    """(config_root, engine_root), honoring the Homecoming env overrides.

    Hardcoding ~/.m3 would silently break every deployment that sets
    M3_CONFIG_ROOT / M3_ENGINE_ROOT / M3_MEMORY_ROOT. Prefer the SDK resolvers;
    fall back to the documented precedence when the venv is unimportable."""
    try:
        from m3_sdk import get_m3_config_root, get_m3_engine_root
        return Path(get_m3_config_root()), Path(get_m3_engine_root())
    except Exception:
        master = os.environ.get("M3_MEMORY_ROOT")
        cfg = (os.environ.get("M3_CONFIG_ROOT")
               or (os.path.join(master, "config") if master else None)
               or os.path.expanduser("~/.m3/config"))
        eng = (os.environ.get("M3_ENGINE_ROOT")
               or (os.path.join(master, "engine") if master else None)
               or os.path.expanduser("~/.m3/engine"))
        return Path(cfg), Path(eng)


CONFIG_ROOT, ENGINE_ROOT = _roots()
HEARTBEAT = CONFIG_ROOT / ".loop_heartbeat.json"
STATE = CONFIG_ROOT / ".loop_watchdog_state.json"


def _log_path() -> Path:
    if _os_name() == "Darwin":
        return Path(os.path.expanduser("~/Library/Logs/m3_loop_watchdog.log"))
    return CONFIG_ROOT.parent / "logs" / "m3_loop_watchdog.log"


def log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    # Match the codebase's cp1252-safe print (ASCII only here, but the console
    # encoding can still reject the stream's default).
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    try:
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def halt_active() -> bool:
    """True if a LIVE exclusive op holds HALT_m3.

    Delegates to the shared protocol reader, which already treats a halt whose
    owner died as void. Fail SAFE on an unimportable m3_halt: if we cannot tell,
    assume a halt may be held and do nothing (a missed restart is recoverable at
    the next tick; a restart into a migration may not be)."""
    try:
        import m3_halt
        return bool(m3_halt.halt_is_active(engine_root=str(ENGINE_ROOT)))
    except Exception as e:
        log(f"cannot read halt state ({e}) — standing down this tick")
        return True


def loop_process_is_registered() -> bool | None:
    """True/False from the m3_halt PID registry, or None if it cannot be read.

    Cross-platform liveness without shelling out: the loop registers on startup
    and deregisters on clean exit, and list_live_processes() reaps entries whose
    pid is dead or recycled."""
    try:
        import m3_halt
        procs = m3_halt.list_live_processes(engine_root=str(ENGINE_ROOT))
        return any(getattr(p, "role", None) == _HALT_ROLE for p in procs)
    except Exception:
        return None


def heartbeat_age() -> tuple[float | None, int]:
    """(age_seconds, interval_s). age is None when the file is absent or
    unreadable — explicitly 'unknown', never 'stale'."""
    try:
        hb = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(hb["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        interval = int(hb.get("interval_s") or 300)
        return (datetime.now(timezone.utc) - ts).total_seconds(), interval
    except (OSError, ValueError, KeyError, TypeError):
        return None, 300


def recently_restarted() -> bool:
    try:
        last = json.loads(STATE.read_text(encoding="utf-8")).get("last_restart", 0)
        return (time.time() - float(last)) < _MIN_RESTART_GAP_S
    except (OSError, ValueError, TypeError):
        return False


def _restart_backend() -> tuple[list[list[str]], str]:
    """(commands, description) to bounce the loop via its platform supervisor.

    Each OS gets the idiom that preserves single-supervisor ownership:
      Darwin  — kickstart -k restarts the launchd-managed job in place.
      Linux   — systemctl --user restart; the unit is Restart=always.
      Windows — /End then /Run; MultipleInstances=IgnoreNew makes a redundant
                /Run harmless, and /End releases the single-instance lock first.
    """
    osn = _os_name()
    if osn == "Darwin":
        uid = os.getuid() if hasattr(os, "getuid") else 0
        return ([["launchctl", "kickstart", "-k",
                  f"gui/{uid}/{_LAUNCHD_LABEL}"]], f"launchd {_LAUNCHD_LABEL}")
    if osn == "Linux":
        return ([["systemctl", "--user", "restart", _SYSTEMD_UNIT]],
                f"systemd {_SYSTEMD_UNIT}")
    return ([["schtasks", "/End", "/TN", _WINDOWS_TASK],
             ["schtasks", "/Run", "/TN", _WINDOWS_TASK]],
            f"schtasks {_WINDOWS_TASK}")


def restart(reason: str) -> int:
    if recently_restarted():
        log(f"WOULD restart ({reason}) but backoff window is open — standing down")
        return 0
    cmds, what = _restart_backend()
    log(f"RESTARTING via {what}: {reason}")
    ok = True
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                               **no_window_kwargs())
        except (OSError, subprocess.SubprocessError) as e:
            log(f"  {cmd[0]} failed: {e}")
            ok = False
            break
        # Windows /End returns non-zero when the task is not running; that is
        # the expected no-op on the dead-loop path, not a failure.
        if r.returncode != 0 and cmd[:2] != ["schtasks", "/End"]:
            log(f"  {' '.join(cmd)} exit={r.returncode} "
                f"stderr={(r.stderr or '').strip()}")
            ok = False
    if not ok:
        return 1
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"last_restart": time.time(),
                                     "reason": reason}), encoding="utf-8")
    except OSError:
        pass
    log("restart issued OK")
    return 0


def main() -> int:
    if halt_active():
        log("HALT_m3 is live — an exclusive op owns the DBs; standing down")
        return 0

    age, interval = heartbeat_age()
    stale_after = max(_MIN_STALE_S, interval * _MISSED_CYCLES)

    if age is None:
        # No progress signal. Fall back to liveness so a loop that died BEFORE
        # ever writing a heartbeat is still recovered.
        registered = loop_process_is_registered()
        if registered is False:
            return restart("no heartbeat and no live cognitive-loop in the "
                           "m3_halt PID registry")
        log(f"OK (degraded): no heartbeat at {HEARTBEAT} "
            f"(registry says {'alive' if registered else 'unknown'}) — "
            f"liveness only. Heartbeat patch missing or loop not yet cycled?")
        return 0

    if age > stale_after:
        return restart(f"heartbeat is {age:.0f}s old (> {stale_after:.0f}s = "
                       f"{_MISSED_CYCLES} x {interval}s interval) — process may "
                       f"be alive but is not completing cycles")

    log(f"OK: heartbeat {age:.0f}s old (threshold {stale_after:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
