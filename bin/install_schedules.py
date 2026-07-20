#!/usr/bin/env python3
"""
M3 Memory: Cross-Platform Schedule Installer.
Automatically configures crontab (macOS/Linux) or schtasks (Windows).
Uses project virtual environment paths and ensures log directories exist.
"""

import argparse
import os
import pathlib
import subprocess
import sys
import tempfile


def _os_name() -> str:
    """WMI-safe OS name. Replaces platform.system(), which hangs on a WMI query
    on Py3.14/Windows. os.name/sys.platform are constants — no WMI, same OS
    branching ('Windows'/'Darwin'/'Linux')."""
    if os.name == "nt":
        return "Windows"
    if sys.platform == "darwin":
        return "Darwin"
    return "Linux"


def _safe_print(msg: str) -> None:
    """Print that survives cp1252 consoles by stripping un-encodable chars."""
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(msg.encode(enc, errors="replace").decode(enc, errors="replace"))


OK = "[OK]"
FAIL = "[FAIL]"
WARN = "[WARN]"

def install_unix_crontab(m3_memory_root):
    template_path = os.path.join(m3_memory_root, "bin", "crontab.template")
    if not os.path.exists(template_path):
        print(f"Error: Could not find {template_path}")
        sys.exit(1)

    with open(template_path, "r") as f:
        template_content = f.read()

    # Create logs directory if it doesn't exist
    log_dir = os.path.join(m3_memory_root, "logs")
    os.makedirs(log_dir, exist_ok=True)

    # Replace placeholder with absolute path
    cron_content = template_content.replace("[M3_MEMORY_ROOT]", m3_memory_root)

    # Get current crontab
    current_cron = ""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if result.returncode == 0:
            current_cron = result.stdout
    except FileNotFoundError:
        print("Error: 'crontab' command not found. Ensure cron is installed.")
        sys.exit(1)

    # Filter out old agent_os entries to prevent duplicates
    # This filters any line containing the current m3_memory_root
    filtered_cron = "\n".join([line for line in current_cron.splitlines() if m3_memory_root not in line])

    # Append the new content
    new_cron = filtered_cron.strip() + "\n\n" + cron_content.strip() + "\n"

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
        tmp.write(new_cron)
        tmp_path = tmp.name

    try:
        subprocess.run(["crontab", tmp_path], check=True)
        _safe_print(f"{OK} Successfully installed crontab schedules for macOS/Linux.")
        _safe_print(f"   Logs available in: {log_dir}")
    except subprocess.CalledProcessError as e:
        _safe_print(f"{FAIL} Failed to install crontab: {e}")
    finally:
        os.unlink(tmp_path)


def _render_template(template_path: str, m3_memory_root: str, python_exe: str,
                     port: "int | None" = None) -> str:
    """Read a template file and substitute the [M3_MEMORY_ROOT] / [M3_PYTHON]
    (and optional [M3_DASHBOARD_PORT]) placeholders. Used for the launchd plist
    and systemd unit templates."""
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()
    content = (content
               .replace("[M3_MEMORY_ROOT]", m3_memory_root)
               .replace("[M3_PYTHON]", python_exe))
    if port is not None:
        content = content.replace("[M3_DASHBOARD_PORT]", str(port))
    return content


def install_unix_dashboard(m3_memory_root, port: int = 8088):
    """Install the web dashboard as a native user service (launchd on macOS,
    systemd --user on Linux) so it auto-starts at login on the given ``port``.

    Mirrors install_unix_cognitive_loop: the server runs in the FOREGROUND
    (``--foreground``) under the service manager's supervision (its own
    _already_serving pre-flight makes a redundant launch a quiet no-op, so this
    is safe to re-run). Cross-platform (Windows uses the ONSTART schtasks path
    instead — see install_windows_tasks)."""
    os_name = _os_name()
    python_exe = _venv_python(m3_memory_root)
    bin_dir = os.path.join(m3_memory_root, "bin")
    os.makedirs(os.path.join(m3_memory_root, "logs"), exist_ok=True)

    if os_name == "Darwin":
        template = os.path.join(bin_dir, "com.m3memory.dashboard.plist")
        if not os.path.exists(template):
            _safe_print(f"{FAIL} Missing template: {template}")
            return
        dest_dir = os.path.expanduser("~/Library/LaunchAgents")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "com.m3memory.dashboard.plist")
        with open(dest, "w", encoding="utf-8") as f:
            f.write(_render_template(template, m3_memory_root, python_exe, port))
        subprocess.run(["launchctl", "unload", dest], capture_output=True)
        r = subprocess.run(["launchctl", "load", dest], capture_output=True, text=True)
        if r.returncode == 0:
            _safe_print(f"{OK} Installed + loaded launchd agent (dashboard :{port}): {dest}")
        else:
            _safe_print(f"{FAIL} launchctl load failed: {r.stderr.strip()}")

    elif os_name == "Linux":
        template = os.path.join(bin_dir, "m3-dashboard.service")
        if not os.path.exists(template):
            _safe_print(f"{FAIL} Missing template: {template}")
            return
        dest_dir = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "m3-dashboard.service")
        with open(dest, "w", encoding="utf-8") as f:
            f.write(_render_template(template, m3_memory_root, python_exe, port))
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        r = subprocess.run(
            ["systemctl", "--user", "enable", "--now", "m3-dashboard.service"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            _safe_print(f"{OK} Installed + started systemd --user unit (dashboard :{port}): {dest}")
        else:
            _safe_print(f"{FAIL} systemctl enable --now failed: {r.stderr.strip()}")
    else:
        _safe_print(f"{WARN} install_unix_dashboard: unsupported OS {os_name}")


def install_unix_cognitive_loop(m3_memory_root):
    """Install the cognitive loop as a native service (launchd on macOS,
    systemd --user on Linux) so it auto-starts at login and auto-restarts on
    crash. The loop's own acquire_lock() makes a redundant launch a quiet
    no-op, so this is safe to re-run. Cron is deliberately NOT used — it is the
    wrong tool for a keepalive daemon."""
    os_name = _os_name()
    python_exe = _venv_python(m3_memory_root)
    bin_dir = os.path.join(m3_memory_root, "bin")
    os.makedirs(os.path.join(m3_memory_root, "logs"), exist_ok=True)

    if os_name == "Darwin":
        template = os.path.join(bin_dir, "com.m3memory.cognitiveloop.plist")
        if not os.path.exists(template):
            _safe_print(f"{FAIL} Missing template: {template}")
            return
        dest_dir = os.path.expanduser("~/Library/LaunchAgents")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "com.m3memory.cognitiveloop.plist")
        with open(dest, "w", encoding="utf-8") as f:
            f.write(_render_template(template, m3_memory_root, python_exe))
        # unload first (ignore failure if not loaded), then load.
        subprocess.run(["launchctl", "unload", dest], capture_output=True)
        r = subprocess.run(["launchctl", "load", dest], capture_output=True, text=True)
        if r.returncode == 0:
            _safe_print(f"{OK} Installed + loaded launchd agent: {dest}")
        else:
            _safe_print(f"{FAIL} launchctl load failed: {r.stderr.strip()}")

    elif os_name == "Linux":
        template = os.path.join(bin_dir, "m3-cognitive-loop.service")
        if not os.path.exists(template):
            _safe_print(f"{FAIL} Missing template: {template}")
            return
        dest_dir = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "m3-cognitive-loop.service")
        with open(dest, "w", encoding="utf-8") as f:
            f.write(_render_template(template, m3_memory_root, python_exe))
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        r = subprocess.run(
            ["systemctl", "--user", "enable", "--now", "m3-cognitive-loop.service"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            _safe_print(f"{OK} Installed + started systemd --user unit: {dest}")
        else:
            _safe_print(f"{FAIL} systemctl enable --now failed: {r.stderr.strip()}")
    else:
        _safe_print(f"{WARN} install_unix_cognitive_loop: unsupported OS {os_name}")


def remove_unix_cognitive_loop():
    """Uninstall the launchd agent / systemd unit for the cognitive loop."""
    os_name = _os_name()
    if os_name == "Darwin":
        dest = os.path.expanduser(
            "~/Library/LaunchAgents/com.m3memory.cognitiveloop.plist")
        if os.path.exists(dest):
            subprocess.run(["launchctl", "unload", dest], capture_output=True)
            os.unlink(dest)
            _safe_print(f"{OK} Removed launchd agent: {dest}")
        else:
            _safe_print(f"{WARN} launchd agent not installed (nothing to remove).")
    elif os_name == "Linux":
        dest = os.path.expanduser(
            "~/.config/systemd/user/m3-cognitive-loop.service")
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "m3-cognitive-loop.service"],
            capture_output=True,
        )
        if os.path.exists(dest):
            os.unlink(dest)
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            _safe_print(f"{OK} Removed systemd --user unit: {dest}")
        else:
            _safe_print(f"{WARN} systemd unit not installed (nothing to remove).")
    else:
        _safe_print(f"{WARN} remove_unix_cognitive_loop: unsupported OS {os_name}")


def _venv_python(m3_memory_root: str, windowless: bool = False) -> str:
    """Resolve the project venv's python interpreter, cross-platform.
    Falls back to sys.executable if no venv is present.

    windowless=True (Windows only) returns pythonw.exe instead of python.exe.
    pythonw.exe is a GUI-subsystem binary — the OS never allocates a console
    for it, so a scheduled task running it draws NO window. python.exe is a
    console-subsystem binary and DOES flash a console window when launched by
    Task Scheduler, even without a cmd.exe wrapper. Because pythonw.exe has no
    stdout/stderr, scheduled-task entrypoints MUST self-log via _task_runtime
    (they do — see get_schedule_specs / the --log-file args).
    """
    if _os_name() == "Windows":
        exe = "pythonw.exe" if windowless else "python.exe"
        candidate = os.path.join(m3_memory_root, ".venv", "Scripts", exe)
        if os.path.exists(candidate):
            return candidate
        # Fall back to a sibling of sys.executable (e.g. pythonw.exe next to
        # python.exe) before giving up on the windowless request entirely.
        if windowless:
            sibling = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if os.path.exists(sibling):
                return sibling
        return sys.executable
    else:
        candidate = os.path.join(m3_memory_root, ".venv", "bin", "python")
        return candidate if os.path.exists(candidate) else sys.executable


def get_schedule_specs(m3_memory_root, dashboard_port: int = 8088):
    """Return list of schedule specifications (normalized for Windows & Unix).

    ``dashboard_port`` sets the port in the AgentOS_Dashboard task's args
    (default 8088). Only the dashboard spec uses it.

    Each spec carries an ``args`` list — the python script path plus its CLI
    flags, including ``--log-file``. The Windows task action is ``python.exe``
    invoked directly with these args: NO ``cmd.exe`` wrapper and NO shell
    ``>>`` redirect (those drew a focus-stealing console window every fire).
    The entrypoints self-log via bin/_task_runtime.py instead.

    Note: ``AgentOS_CognitiveLoop`` is the Windows ONSTART spec. On macOS/Linux
    the cognitive loop is installed as a launchd/systemd service instead — see
    install_unix_cognitive_loop().
    """
    log_dir = os.path.join(m3_memory_root, "logs")
    bin_dir = os.path.join(m3_memory_root, "bin")

    def _log(name):
        return os.path.join(log_dir, name)

    def _script(name):
        return os.path.join(bin_dir, name)

    return [
        {
            "name": "AgentOS_WeeklyAuditor",
            "args": [_script("weekly_auditor.py"),
                     "--log-file", _log("auditor.log")],
            "schedule": "WEEKLY",
            "modifier": "FRI",
            "time": "16:00",
            "description": "Run weekly auditor on Fridays at 4pm"
        },
        {
            "name": "AgentOS_HourlySync",
            "args": [_script("sync_all.py"),
                     "--log-file", _log("sync_all.log")],
            "schedule": "HOURLY",
            "modifier": "1",
            "time": "00:00",
            "description": "Hourly sync: SQLite <-> PostgreSQL"
        },
        {
            "name": "AgentOS_Maintenance",
            # Previously invoked via `python -c "...memory_maintenance_impl()"`;
            # memory_maintenance.py now has a real __main__ block so it runs as
            # a script (enables --log-file + single-instance locking).
            "args": [_script("memory_maintenance.py"),
                     "--log-file", _log("maintenance.log")],
            "schedule": "DAILY",
            "modifier": "",
            "time": "03:00",
            "description": "Daily memory maintenance (decay, prune orphans)"
        },
        {
            "name": "AgentOS_SecretRotator",
            "args": [_script("secret_rotator.py"),
                     "--log-file", _log("secret_rotator.log")],
            "schedule": "MONTHLY",
            "modifier": "1",
            "time": "02:00",
            "description": "Monthly automated secret rotation"
        },
        {
            "name": "AgentOS_ChatlogEmbedSweep",
            # --batch 256 is efficient per-batch (embedding is cheap); --deadline
            # 60 bounds one run's wall-clock so a large backlog drains across
            # several scheduled runs instead of pinning the GPU in one long run
            # on an interactive machine. --max-per-run still caps total rows.
            "args": [_script("chatlog_embed_sweeper.py"),
                     "--batch", "256", "--max-per-run", "10000",
                     "--deadline", "60",
                     "--log-file", _log("chatlog_embed_sweep.log")],
            "schedule": "MINUTE",
            "modifier": "30",
            "time": "00:00",
            "description": "Embed un-embedded chat_log rows using the local embedding server"
        },
        {
            "name": "AgentOS_ObservationDrain",
            # --drain-batch 200 was a heavy local-LLM burst (enrich_local_qwen):
            # draining 200 queued observations in one run pins the GPU for a long
            # stretch. Lowered to 8 so each 15-min run is a short burst; the queue
            # still drains steadily across runs and interactive use isn't starved.
            "args": [_script("m3_enrich.py"),
                     "--drain-queue", "--drain-batch", "8",
                     "--profile", "enrich_local_qwen",
                     "--log-file", _log("observation_drain.log")],
            "schedule": "MINUTE",
            "modifier": "15",
            "time": "00:00",
            "description": "Drain observation_queue: extract user-facts from chatlog conversations"
        },
        {
            "name": "AgentOS_CognitiveLoop",
            # --background re-execs under pythonw.exe (no console at all) on
            # Windows. macOS/Linux use a launchd/systemd service instead.
            # --interval 60: on an idle host the loop re-ticks fast to drain a
            # backlog (backlog-aware wait in the loop), so 60s is the ceiling
            # between passes, not a fixed trickle. --limit-per-pass inherits the
            # loop's default (2): a short GPU burst per pass, shrunk to 1 under
            # THROTTLED load. (Previously --interval 300 with the old default-1
            # ceiling drained ~1 item / 5 min even while idle.)
            "args": [_script("m3_cognitive_loop.py"),
                     "--interval", "60", "--background",
                     "--log-file", _log("cognitive_loop.log")],
            "schedule": "ONSTART",
            "modifier": "",
            "time": "00:00",
            "description": "Autonomous heartbeat: entity extraction, observations, and reflection (continuous)"
        },
        {
            "name": "AgentOS_EmbedServer",
            # Shared in-process GPU embedder server (bin/embed_server_inproc.py):
            # loads the GGUF embedder ONCE and serves it over localhost HTTP so
            # every m3 process (MCP server, cognitive loop) uses ONE CUDA context
            # instead of one-per-process (~9-10 GB reclaimed). Clients defer via
            # .embed_config.json {"disable_inproc_embedder":true,"fallback_url":...}.
            # The server auto-detects the bge-m3 GGUF (discover_bge_m3_gguf) so it
            # needs no env; binds 127.0.0.1 only. ONSTART so it's the sole embedder
            # from boot, before any client tries to embed.
            "args": [_script("embed_server_inproc.py"),
                     "--port", "8082",
                     "--log-file", _log("embed_server_inproc.log")],
            "schedule": "ONSTART",
            "modifier": "",
            "time": "00:00",
            "description": "Shared in-process GPU embedder server (one CUDA context, localhost HTTP)"
        },
        {
            "name": "AgentOS_Dashboard",
            # Local web dashboard (bin/dashboard_server.py). --foreground runs the
            # uvicorn server IN the task process (the task IS the long-lived
            # server); the Windows action binds pythonw.exe (windowless — no
            # console, so no startup flash and no periodic flashes) with NO
            # cmd.exe wrapper. ONSTART so the dashboard is up from boot; it
            # self-registers in the PID registry and binds 127.0.0.1 only.
            # Optional feature — this task is only registered when the user opts
            # into the dashboard (setup wizard / `install_schedules --add dashboard`).
            "args": [_script("dashboard_server.py"),
                     "--foreground", "--port", str(dashboard_port),
                     "--log-file", _log("dashboard.log")],
            "schedule": "ONSTART",
            "modifier": "",
            "time": "00:00",
            "description": "Local web dashboard (windowless, localhost HTTP :8088)"
        }
    ]

def _filter_tasks(tasks: list, selector: str | None) -> list:
    """Return tasks matching the selector. selector may be a full name or short alias
    (e.g. 'chatlog-embed-sweep' matches 'AgentOS_ChatlogEmbedSweep')."""
    if not selector:
        return tasks
    sel_norm = selector.lower().replace("-", "").replace("_", "")
    matched = [t for t in tasks if t["name"].lower().replace("_", "") == sel_norm
               or sel_norm in t["name"].lower().replace("_", "")]
    return matched


# ── Windows Task Scheduler XML rendering ──────────────────────────────────────
# schtasks.exe cannot express MultipleInstances, ExecutionTimeLimit, or trigger
# Repetition from its CLI flags — the old code shelled out to PowerShell after
# /Create to patch those in. Task Scheduler's native format IS XML, and
# `schtasks /Create /XML` accepts a full definition in one call, so we render the
# spec to XML and drop the PowerShell dependency entirely. (macOS/Linux are
# unaffected — they use crontab / launchd / systemd.)

# Task Scheduler XML schema namespace (constant for all supported OS versions).
_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _xml_escape(text: str) -> str:
    """Escape the five XML metacharacters for both element text and attribute
    values. Deliberately not xml.sax.saxutils (Bandit B406 flags that module for
    untrusted-XML *parsing*; we only *emit* XML) and not a heavy DOM builder —
    this is the minimal correct escape for the trusted spec values we render."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )

# The long-lived ONSTART processes need a self-heal repetition: each is a single
# continuous process, so if it dies it never restarts until the next boot. The
# MINUTE/HOURLY/DAILY/WEEKLY/MONTHLY tasks already re-fire on their own cadence.
# The repetition re-fires the trigger indefinitely (no duration); MultipleInstances
# =IgnoreNew makes a re-fire while the process is still alive a harmless no-op, so
# the net effect is "restart it if and only if it has died."
#   - AgentOS_CognitiveLoop: PT30M — a stalled heartbeat is tolerable for ~30 min.
#   - AgentOS_EmbedServer:    PT5M  — this is the SOLE embedder for the whole fleet
#     (clients disable their own tier-1 via .embed_config.json), so its death is a
#     fleet-wide embedding outage. A 5-min heal bounds that outage to ~5 min.
#     (Was PT1M; widened 2026-07-19 — a 1-min re-fire is needless process churn now
#     that the re-fire is an invisible no-op. The re-fire is safe because (a)
#     MultipleInstances=IgnoreNew makes a re-fire of the task a no-op while the
#     server is alive, and (b) the server acquires the shared ATOMIC
#     single-instance lock (m3_halt.acquire_single_instance) on startup and, if a
#     peer already holds it, exits EXIT_ALREADY_RUNNING WITHOUT loading a second
#     GPU embedder — so a re-fire can never stack a second CUDA context even if
#     launched out-of-band (the atomic lock closed a TOCTOU race the old /health
#     probe had). Without the heal, one crash silently kills write-embedding AND
#     semantic/vector retrieval across every m3 process until reboot (2026-07-03).
_SELF_HEAL_TASKS = {
    "AgentOS_CognitiveLoop": "PT30M",
    "AgentOS_EmbedServer": "PT5M",
    # Re-fire every 5 min: if the dashboard dies it comes back within ~5 min.
    # Safe to re-fire — MultipleInstances=IgnoreNew makes a re-fire a no-op while
    # it's alive, and the dashboard's atomic single-instance lock makes a re-fire
    # that DOES launch exit EXIT_ALREADY_RUNNING without binding, so it never
    # double-binds (supervisors treat that exit as clean, not a crash).
    "AgentOS_Dashboard": "PT5M",
}


def _xml_repetition(interval_iso: str) -> str:
    """A <Repetition> that repeats every interval_iso forever (no Duration)."""
    return (
        "<Repetition>"
        f"<Interval>{interval_iso}</Interval>"
        "<StopAtDurationEnd>false</StopAtDurationEnd>"
        "</Repetition>"
    )


def _render_trigger_xml(task: dict) -> str:
    """Map a spec's schedule/modifier/time to a Task Scheduler <Triggers> body.

    Supported schedules mirror the schtasks /SC values used elsewhere:
      MINUTE / HOURLY  -> TimeTrigger with a Repetition interval
      DAILY            -> CalendarTrigger / ScheduleByDay
      WEEKLY           -> CalendarTrigger / ScheduleByWeek (modifier = day-of-week)
      MONTHLY          -> CalendarTrigger / ScheduleByMonth (modifier = day-of-month)
      ONSTART          -> BootTrigger (+ self-heal Repetition for the loop)
    """
    sched = task["schedule"]
    mod = task.get("modifier") or ""
    hh, mm = (task.get("time") or "00:00").split(":")[:2]
    # A fixed, past StartBoundary date keeps the XML deterministic (no clock read
    # at install time); Task Scheduler computes the next fire from it. Time-of-day
    # is what actually matters for the calendar/time triggers.
    start = f"2020-01-01T{int(hh):02d}:{int(mm):02d}:00"

    if sched == "MINUTE":
        n = int(mod or "1")
        return (
            "<TimeTrigger>"
            f"<StartBoundary>{start}</StartBoundary>"
            "<Enabled>true</Enabled>"
            f"{_xml_repetition(f'PT{n}M')}"
            "</TimeTrigger>"
        )
    if sched == "HOURLY":
        n = int(mod or "1")
        return (
            "<TimeTrigger>"
            f"<StartBoundary>{start}</StartBoundary>"
            "<Enabled>true</Enabled>"
            f"{_xml_repetition(f'PT{n}H')}"
            "</TimeTrigger>"
        )
    if sched == "DAILY":
        return (
            "<CalendarTrigger>"
            f"<StartBoundary>{start}</StartBoundary>"
            "<Enabled>true</Enabled>"
            "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
            "</CalendarTrigger>"
        )
    if sched == "WEEKLY":
        # schtasks uses 3-letter day tokens (MON/TUE/.../FRI); XML wants element
        # names (<Monday/> ...). Map, defaulting to Sunday if unrecognised.
        days = {
            "MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday",
            "THU": "Thursday", "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday",
        }
        day_el = days.get(mod.upper()[:3], "Sunday")
        return (
            "<CalendarTrigger>"
            f"<StartBoundary>{start}</StartBoundary>"
            "<Enabled>true</Enabled>"
            "<ScheduleByWeek>"
            f"<DaysOfWeek><{day_el}/></DaysOfWeek>"
            "<WeeksInterval>1</WeeksInterval>"
            "</ScheduleByWeek>"
            "</CalendarTrigger>"
        )
    if sched == "MONTHLY":
        dom = int(mod or "1")
        return (
            "<CalendarTrigger>"
            f"<StartBoundary>{start}</StartBoundary>"
            "<Enabled>true</Enabled>"
            "<ScheduleByMonth>"
            f"<DaysOfMonth><Day>{dom}</Day></DaysOfMonth>"
            "<Months>"
            "<January/><February/><March/><April/><May/><June/><July/>"
            "<August/><September/><October/><November/><December/>"
            "</Months>"
            "</ScheduleByMonth>"
            "</CalendarTrigger>"
        )
    if sched == "ONSTART":
        # Original schtasks-era task registered BOTH a boot trigger and a logon
        # trigger. A BootTrigger fires before interactive logon, but the task
        # runs as InteractiveToken (see _render_task_xml), so on a boot-before-
        # logon machine the boot start can be deferred until the user logs in.
        # Emitting ONLY a BootTrigger therefore risks the loop not starting until
        # a real logon/reboot — a regression from the original. Keep both so the
        # loop comes up at whichever event happens first. Both carry the same
        # self-heal repetition (IgnoreNew makes the duplicate a harmless no-op).
        rep = _SELF_HEAL_TASKS.get(task["name"])
        rep_xml = _xml_repetition(rep) if rep else ""
        return (
            "<BootTrigger>"
            "<Enabled>true</Enabled>"
            f"{rep_xml}"
            "</BootTrigger>"
            "<LogonTrigger>"
            "<Enabled>true</Enabled>"
            f"{rep_xml}"
            "</LogonTrigger>"
        )
    raise ValueError(f"Unsupported schedule for XML rendering: {sched!r}")


def _render_task_xml(task: dict, python_exe: str, user_id: str) -> str:
    """Render one spec to a complete Task Scheduler XML document string."""
    # <Arguments> is one space-joined string; each path is quoted so paths with
    # spaces survive, mirroring the previous /TR construction.
    arguments = " ".join(f'"{part}"' for part in task["args"])
    triggers = _render_trigger_xml(task)
    esc = _xml_escape
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.2" xmlns="{_TASK_NS}">'
        "<RegistrationInfo>"
        f"<Description>{esc(task['description'])}</Description>"
        "</RegistrationInfo>"
        f"<Triggers>{triggers}</Triggers>"
        "<Principals>"
        '<Principal id="Author">'
        f"<UserId>{esc(user_id)}</UserId>"
        # LeastPrivilege == the task RUNS as the logged-in user, non-elevated
        # (matches the prior schtasks default; no pass needs elevation to run).
        # NOTE: this is about EXECUTION, not REGISTRATION — registering an ONSTART
        # (boot-trigger) task still requires an elevated shell; the caller handles
        # the "Access is denied" case with a re-run-elevated hint.
        "<RunLevel>LeastPrivilege</RunLevel>"
        "<LogonType>InteractiveToken</LogonType>"
        "</Principal>"
        "</Principals>"
        "<Settings>"
        # Hidden: keep the task off the interactive desktop. pythonw.exe (GUI
        # subsystem) alone does NOT suppress the window — a non-Hidden task run
        # under InteractiveToken still FLASHES a console every fire (observed
        # 2026-07-19: the PT1M/PT5M/PT30M self-heal re-fires each flashed). Hidden
        # makes the (usually no-op) self-heal re-fires invisible. This is the
        # anti-flash guarantee the pythonw comment in install_windows_tasks() only
        # half-delivered.
        "<Hidden>true</Hidden>"
        # IgnoreNew: never stack a second copy while one runs — a slow/stuck run
        # must not over-dispatch the local LLM, and it makes the self-heal
        # repetition a no-op when the task is already alive.
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"
        "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>"
        "<AllowStartOnDemand>true</AllowStartOnDemand>"
        "<Enabled>true</Enabled>"
        "<StartWhenAvailable>true</StartWhenAvailable>"
        # ExecutionTimeLimit caps a hung run. The continuous loop must never be
        # killed mid-flight, so it gets PT0S (no limit); others get PT1H.
        f"<ExecutionTimeLimit>{'PT0S' if task['schedule'] == 'ONSTART' else 'PT1H'}</ExecutionTimeLimit>"
        "</Settings>"
        '<Actions Context="Author">'
        "<Exec>"
        f"<Command>{esc(python_exe)}</Command>"
        f"<Arguments>{esc(arguments)}</Arguments>"
        "</Exec>"
        "</Actions>"
        "</Task>"
    )


def install_windows_tasks(m3_memory_root, selector: str | None = None, dashboard_port: int = 8088):
    # pythonw.exe (GUI subsystem) — avoids the console window python.exe
    # (console subsystem) flashes every fire, even without a cmd.exe wrapper.
    # Entrypoints self-log via _task_runtime, so pythonw.exe having no stdout is
    # fine. NOTE: pythonw alone is NOT sufficient to guarantee no flash — a task
    # run under InteractiveToken still flashes unless <Hidden>true</Hidden> is set
    # in <Settings> (see _build_task_xml). Both are required.
    python_exe = _venv_python(m3_memory_root, windowless=True)
    if python_exe == sys.executable and not os.path.exists(os.path.join(m3_memory_root, ".venv")):
        _safe_print(f"{WARN} Using system Python {python_exe} because .venv was not found.")

    log_dir = os.path.join(m3_memory_root, "logs")
    os.makedirs(log_dir, exist_ok=True)

    tasks = _filter_tasks(get_schedule_specs(m3_memory_root, dashboard_port), selector)
    if not tasks:
        _safe_print(f"{FAIL} No schedule matches selector={selector!r}. Try --list to see all.")
        return

    # The task owner (Principal/UserId). Prefer the domain\user form Task
    # Scheduler records; fall back to the bare username. Never empty — an empty
    # UserId makes schtasks reject the XML.
    user_id = (
        f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
        if os.environ.get("USERDOMAIN") and os.environ.get("USERNAME")
        else os.environ.get("USERNAME", "")
    )

    success = True
    denied_any = False
    denied_tasks: list[str] = []
    for task in tasks:
        subprocess.run(["schtasks", "/Delete", "/TN", task["name"], "/F"], capture_output=True)
        # Register from a full Task Scheduler XML definition. Unlike the CLI
        # flags, XML can express MultipleInstances=IgnoreNew, ExecutionTimeLimit,
        # and trigger Repetition in ONE call — so the PowerShell post-hardening
        # step (and its dependency) is gone. schtasks /Create /XML requires the
        # file to be UTF-16; Python's "utf-16" codec writes the BOM it needs.
        try:
            xml_doc = _render_task_xml(task, python_exe, user_id)
        except ValueError as e:
            # An unsupported schedule is a contract violation, not an edge case
            # (§3 fail-loud): surface it, skip the task, keep going for the rest.
            _safe_print(f"{FAIL} Cannot render task {task['name']}: {e}")
            success = False
            continue

        xml_path = None
        try:
            fd, xml_path = tempfile.mkstemp(suffix=".xml", prefix=f"m3_{task['name']}_")
            with os.fdopen(fd, "w", encoding="utf-16") as fh:
                fh.write(xml_doc)
            result = subprocess.run(
                ["schtasks", "/Create", "/TN", task["name"], "/XML", xml_path, "/F"],
                capture_output=True, text=True,
            )
        finally:
            # Always clean up the temp file — no orphaned artifacts (§12c).
            if xml_path and os.path.exists(xml_path):
                try:
                    os.unlink(xml_path)
                except OSError:
                    pass

        if result.returncode == 0:
            note = " (+30-min self-heal repetition)" if task["name"] in _SELF_HEAL_TASKS else ""
            _safe_print(f"{OK} Created Windows Task: {task['name']}{note}")
        else:
            # Fail loud (§3): print the real schtasks error, don't swallow it.
            err = (result.stderr or result.stdout).strip()
            _safe_print(f"{FAIL} Failed to create task {task['name']}: {err}")
            if "access is denied" in err.lower():
                denied_any = True
                denied_tasks.append(task["name"])
            success = False

    if success:
        _safe_print(f"{OK} Finished installing {len(tasks)} Windows scheduled task(s).")
        _safe_print(f"   Logs available in: {log_dir}")
    else:
        _safe_print(f"{WARN} One or more Windows tasks failed to install (see above).")
        if denied_any:
            # "Access is denied" on /Create means the shell isn't elevated. The
            # ONSTART tasks (CognitiveLoop, EmbedServer, SecretRotator) register a
            # boot trigger, which Task Scheduler gates behind admin — the
            # MINUTE/DAILY tasks register fine unelevated, so a partial failure
            # here is almost always this. This is easy to miss in a wall of [OK]s,
            # and it means the user's BACKGROUND SERVICES WON'T START AT BOOT — so
            # surface it as a loud, unmissable banner with the EXACT command to run
            # (resolved from sys.executable + this script's real path, so it is
            # copy-pasteable for a pipx/site-packages install, not a fictional
            # `bin/` on cwd).
            names = ", ".join(denied_tasks) if denied_tasks else \
                "CognitiveLoop, EmbedServer, SecretRotator"
            script = os.path.abspath(__file__)
            py = sys.executable
            bar = "=" * 74
            _safe_print("")
            _safe_print(bar)
            _safe_print("  ACTION REQUIRED — boot-start services NOT installed (needs admin)")
            _safe_print(bar)
            _safe_print(f"  These background tasks could not be registered: {names}")
            _safe_print("  They start m3 at boot; until registered, they will NOT run after a")
            _safe_print("  restart. Registering a boot (ONSTART) task requires an elevated shell.")
            _safe_print("")
            _safe_print("  FIX — open an ADMIN terminal (Windows: right-click > 'Run as")
            _safe_print("  administrator'), then run this one command:")
            _safe_print("")
            _safe_print(f'      "{py}" "{script}" --repair')
            _safe_print("")
            _safe_print("  (--repair is idempotent — it only adds the missing tasks; the ones")
            _safe_print("  already created above are untouched.)")
            _safe_print(bar)
            _safe_print("")


def remove_windows_tasks(selector: str | None, m3_memory_root: str):
    tasks = _filter_tasks(get_schedule_specs(m3_memory_root), selector)
    if not tasks:
        _safe_print(f"{FAIL} No schedule matches selector={selector!r}.")
        return
    for task in tasks:
        r = subprocess.run(
            ["schtasks", "/Delete", "/TN", task["name"], "/F"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            _safe_print(f"{OK} Removed: {task['name']}")
        else:
            _safe_print(f"{WARN} Could not remove {task['name']} (may not exist): {r.stderr.strip()}")

def _verify_windows_task(name: str) -> bool:
    """Read the registered Windows task's XML and confirm the properties the
    installer set actually took (self-heal Repetition where expected,
    MultipleInstances=IgnoreNew). Cross-checks the LIVE task, not the spec, so it
    catches a task that was created but silently lost a setting. Returns True on
    match. Never raises — a missing task is a clean False."""
    r = subprocess.run(
        ["schtasks", "/Query", "/TN", name, "/XML", "ONE"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        _safe_print(f"{FAIL} {name}: not registered ({(r.stderr or '').strip() or 'no such task'})")
        return False

    xml = r.stdout
    ok = True
    # MultipleInstances=IgnoreNew — guards against the loop stacking copies.
    if "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" not in xml:
        _safe_print(f"{WARN} {name}: MultipleInstancesPolicy is not IgnoreNew")
        ok = False
    # Hidden=true — without it a self-heal re-fire flashes a console window
    # (observed 2026-07-19). Catch a task that was created before this fix or
    # that lost the bit to an out-of-band Set-ScheduledTask.
    if "<Hidden>true</Hidden>" not in xml:
        _safe_print(f"{WARN} {name}: Hidden is not true (task will flash a window on each fire)")
        ok = False
    # Self-heal Repetition — only the tasks in _SELF_HEAL_TASKS must have it.
    expected_rep = _SELF_HEAL_TASKS.get(name)
    if expected_rep:
        if f"<Interval>{expected_rep}</Interval>" in xml:
            _safe_print(f"{OK} {name}: self-heal Repetition {expected_rep} present")
        else:
            _safe_print(f"{FAIL} {name}: expected self-heal Repetition {expected_rep} is MISSING")
            ok = False
    if ok:
        _safe_print(f"{OK} {name}: registered and matches spec")
    return ok


def _verify_unix_cognitive_loop() -> bool:
    """Confirm the launchd agent (macOS) / systemd --user unit (Linux) for the
    cognitive loop is installed AND loaded. KeepAlive/Restart is the Unix
    self-heal analogue of the Windows repetition, so being *loaded* is the
    property that matters. Never raises."""
    osn = _os_name()
    if osn == "Darwin":
        dest = os.path.expanduser("~/Library/LaunchAgents/com.m3memory.cognitiveloop.plist")
        if not os.path.exists(dest):
            _safe_print(f"{FAIL} launchd agent not installed: {dest}")
            return False
        loaded = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        if "com.m3memory.cognitiveloop" in (loaded.stdout or ""):
            _safe_print(f"{OK} launchd agent installed and loaded: {dest}")
            # KeepAlive is the self-heal knob — warn loudly if the plist lacks it.
            try:
                with open(dest, encoding="utf-8") as f:
                    plist = f.read()
                if "KeepAlive" not in plist:
                    _safe_print(f"{WARN} plist has no <key>KeepAlive</key> — loop won't self-heal on crash")
            except OSError:
                pass
            return True
        _safe_print(f"{WARN} launchd agent installed but not loaded (run: launchctl load {dest})")
        return False
    if osn == "Linux":
        unit = "m3-cognitive-loop.service"
        dest = os.path.expanduser(f"~/.config/systemd/user/{unit}")
        if not os.path.exists(dest):
            _safe_print(f"{FAIL} systemd --user unit not installed: {dest}")
            return False
        active = subprocess.run(
            ["systemctl", "--user", "is-active", unit], capture_output=True, text=True
        )
        state = (active.stdout or "").strip()
        if state == "active":
            _safe_print(f"{OK} systemd --user unit installed and active: {unit}")
            return True
        _safe_print(f"{WARN} systemd --user unit installed but {state or 'inactive'}")
        return False
    _safe_print(f"{WARN} verify: unsupported OS {osn}")
    return False


def verify_schedules(selector: str | None, m3_memory_root: str) -> bool:
    """Verify the registered scheduled job(s) match what the installer intends.
    Cross-platform: Windows tasks, macOS launchd, Linux systemd. Returns True if
    everything verified. Replaces the old ad-hoc PowerShell verify helper."""
    osn = _os_name()
    if osn == "Windows":
        tasks = _filter_tasks(get_schedule_specs(m3_memory_root), selector)
        if not tasks:
            _safe_print(f"{FAIL} No schedule matches selector={selector!r}.")
            return False
        # Check EVERY task (don't short-circuit) so all failures are reported at
        # once, not just the first — a verify tool should surface the full picture.
        results = [_verify_windows_task(t["name"]) for t in tasks]
        return all(results)
    # On Unix only the cognitive loop is a managed service; the rest are cron
    # lines. Verify the loop (the one with the self-heal semantics).
    if selector is None or selector.lower().replace("-", "").replace("_", "") in (
        "cognitiveloop", "agentoscognitiveloop"
    ):
        return _verify_unix_cognitive_loop()
    _safe_print(f"{WARN} verify on {osn} currently covers only the cognitive loop.")
    return True


def list_schedules(m3_memory_root):
    """List all configured schedules."""
    specs = get_schedule_specs(m3_memory_root)
    print("\nConfigured Schedules:")
    print("=" * 80)
    for spec in specs:
        print(f"  {spec['name']}")
        print(f"    Description: {spec['description']}")
        print(f"    Schedule: {spec['schedule']} (modifier: {spec['modifier'] or 'N/A'})")
        print()

def main():
    script_dir = pathlib.Path(__file__).parent.resolve()
    m3_memory_root = str(script_dir.parent)

    parser = argparse.ArgumentParser(
        description="Install / remove / list m3-memory scheduled tasks.",
        epilog="Pass --add NAME to install a single task. Running with no flags is a no-op "
               "(prevents accidental mass-install). Use --add all to install every schedule.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="List configured schedules and exit.")
    group.add_argument("--add", metavar="NAME",
                       help="Install one schedule by name (e.g. chatlog-embed-sweep) or 'all'.")
    group.add_argument("--remove", metavar="NAME",
                       help="Remove one schedule by name, or 'all'.")
    group.add_argument("--repair", action="store_true",
                       help="Re-install every configured schedule in place (alias for --add all).")
    group.add_argument("--verify", metavar="NAME", nargs="?", const="all",
                       help="Verify the registered job(s) match the spec (Windows task / "
                            "macOS launchd / Linux systemd). NAME or 'all' (default). "
                            "Exit code is non-zero if verification fails.")
    parser.add_argument("--port", type=int, default=8088,
                        help="Port for the dashboard service (with --add dashboard). Default 8088.")
    args = parser.parse_args()

    if args.list:
        list_schedules(m3_memory_root)
        return

    if args.verify:
        sel = None if args.verify == "all" else args.verify
        ok = verify_schedules(sel, m3_memory_root)
        sys.exit(0 if ok else 1)

    os_name = _os_name()
    _safe_print(f"M3 Memory: Detecting platform... {os_name}")
    _safe_print(f"Project root: {m3_memory_root}")

    if not args.add and not args.remove and not args.repair:
        _safe_print("Nothing to do. Use --list, --add NAME, --remove NAME, or --repair.")
        _safe_print("(Running with no flags used to install everything — now a no-op for safety.)")
        return

    if args.repair:
        args.add = "all"

    # Seed the governor tuning config (.governor_config.json) at install time so
    # the live threshold knob always exists and is discoverable. Idempotent —
    # never clobbers an existing file. Best-effort: a failure must not block task
    # installation.
    if args.add:
        try:
            sys.path.insert(0, str(script_dir))
            from m3_sdk import ensure_governor_config
            ensure_governor_config()
        except Exception:
            pass

    selector = None if (args.add == "all" or args.remove == "all") else (args.add or args.remove)

    if args.add:
        if os_name == "Windows":
            install_windows_tasks(m3_memory_root, selector, dashboard_port=getattr(args, "port", 8088) or 8088)
        elif os_name in ("Darwin", "Linux"):
            if selector:
                _sel = selector.lower().replace("-", "").replace("_", "")
                if _sel in ("cognitiveloop", "agentoscognitiveloop"):
                    # The cognitive loop is a service, not a cron entry —
                    # support installing it on its own.
                    install_unix_cognitive_loop(m3_memory_root)
                elif _sel in ("dashboard", "agentosdashboard"):
                    # The dashboard is a launchd/systemd user service (like the
                    # cognitive loop), not a cron entry — install it on its own.
                    install_unix_dashboard(m3_memory_root, port=getattr(args, "port", 8088) or 8088)
                else:
                    _safe_print(f"{WARN} Unix crontab installer currently rewrites all entries. "
                                "Single-task add on Unix is not supported yet — use --add all or edit "
                                "crontab directly. (cognitive-loop and dashboard can be added on their own.)")
            else:
                install_unix_crontab(m3_memory_root)
                # The cognitive loop runs as a launchd/systemd service, not a
                # cron entry — install it alongside the crontab.
                install_unix_cognitive_loop(m3_memory_root)
        else:
            _safe_print(f"Unsupported OS: {os_name}")
        return

    if args.remove:
        if os_name == "Windows":
            remove_windows_tasks(selector, m3_memory_root)
        elif os_name in ("Darwin", "Linux"):
            if selector and selector.lower().replace("-", "").replace("_", "") in (
                "cognitiveloop", "agentoscognitiveloop"
            ):
                remove_unix_cognitive_loop()
            elif selector:
                _safe_print(f"{WARN} Unix removal of individual cron entries: edit crontab "
                            "directly with `crontab -e`. (cognitive-loop can be removed on its own.)")
            else:
                remove_unix_cognitive_loop()
                _safe_print(f"{WARN} Cron entries: edit crontab directly with `crontab -e` to remove them.")
        else:
            _safe_print(f"Unsupported OS: {os_name}")
        return


if __name__ == "__main__":
    main()
