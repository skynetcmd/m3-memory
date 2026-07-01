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


def _render_template(template_path: str, m3_memory_root: str, python_exe: str) -> str:
    """Read a template file and substitute the [M3_MEMORY_ROOT] / [M3_PYTHON]
    placeholders. Used for the launchd plist and systemd unit."""
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()
    return (content
            .replace("[M3_MEMORY_ROOT]", m3_memory_root)
            .replace("[M3_PYTHON]", python_exe))


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


def get_schedule_specs(m3_memory_root):
    """Return list of schedule specifications (normalized for Windows & Unix).

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
            "description": "Hourly sync: SQLite <-> PostgreSQL + ChromaDB"
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
            "args": [_script("chatlog_embed_sweeper.py"),
                     "--batch", "256", "--max-per-run", "10000",
                     "--log-file", _log("chatlog_embed_sweep.log")],
            "schedule": "MINUTE",
            "modifier": "30",
            "time": "00:00",
            "description": "Embed un-embedded chat_log rows using the local embedding server"
        },
        {
            "name": "AgentOS_ObservationDrain",
            "args": [_script("m3_enrich.py"),
                     "--drain-queue", "--drain-batch", "200",
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
            "args": [_script("m3_cognitive_loop.py"),
                     "--interval", "300", "--background",
                     "--log-file", _log("cognitive_loop.log")],
            "schedule": "ONSTART",
            "modifier": "",
            "time": "00:00",
            "description": "Autonomous heartbeat: entity extraction, observations, and reflection (continuous)"
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

# Only the long-lived ONSTART cognitive loop needs a self-heal repetition: it is
# a single continuous process, so if it dies it never restarts until the next
# boot. The MINUTE/HOURLY/DAILY/WEEKLY/MONTHLY tasks already re-fire on their own
# cadence. 30-minute repetition, no duration => repeat indefinitely. Safe because
# every task sets MultipleInstances=IgnoreNew (a re-fire while alive is a no-op).
_SELF_HEAL_TASKS = {"AgentOS_CognitiveLoop": "PT30M"}


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
        rep = _SELF_HEAL_TASKS.get(task["name"])
        rep_xml = _xml_repetition(rep) if rep else ""
        return (
            "<BootTrigger>"
            "<Enabled>true</Enabled>"
            f"{rep_xml}"
            "</BootTrigger>"
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
        # LeastPrivilege == run as the logged-in user, non-elevated (matches the
        # prior schtasks default; no elevation is needed by any pass).
        "<RunLevel>LeastPrivilege</RunLevel>"
        "<LogonType>InteractiveToken</LogonType>"
        "</Principal>"
        "</Principals>"
        "<Settings>"
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


def install_windows_tasks(m3_memory_root, selector: str | None = None):
    # pythonw.exe (GUI subsystem) — Task Scheduler draws NO console window for
    # it. python.exe (console subsystem) flashes a window every fire even
    # without a cmd.exe wrapper. Entrypoints self-log via _task_runtime, so
    # pythonw.exe having no stdout is fine.
    python_exe = _venv_python(m3_memory_root, windowless=True)
    if python_exe == sys.executable and not os.path.exists(os.path.join(m3_memory_root, ".venv")):
        _safe_print(f"{WARN} Using system Python {python_exe} because .venv was not found.")

    log_dir = os.path.join(m3_memory_root, "logs")
    os.makedirs(log_dir, exist_ok=True)

    tasks = _filter_tasks(get_schedule_specs(m3_memory_root), selector)
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
            # Always clean up the temp file — no orphaned artifacts (§14).
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
            _safe_print(
                f"{FAIL} Failed to create task {task['name']}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
            success = False

    if success:
        _safe_print(f"{OK} Finished installing {len(tasks)} Windows scheduled task(s).")
        _safe_print(f"   Logs available in: {log_dir}")
    else:
        _safe_print(f"{WARN} One or more Windows tasks failed to install (see above).")


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
    args = parser.parse_args()

    if args.list:
        list_schedules(m3_memory_root)
        return

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
            install_windows_tasks(m3_memory_root, selector)
        elif os_name in ("Darwin", "Linux"):
            if selector:
                if selector.lower().replace("-", "").replace("_", "") in (
                    "cognitiveloop", "agentoscognitiveloop"
                ):
                    # The cognitive loop is a service, not a cron entry —
                    # support installing it on its own.
                    install_unix_cognitive_loop(m3_memory_root)
                else:
                    _safe_print(f"{WARN} Unix crontab installer currently rewrites all entries. "
                                "Single-task add on Unix is not supported yet — use --add all or edit "
                                "crontab directly. (cognitive-loop can be added on its own.)")
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
