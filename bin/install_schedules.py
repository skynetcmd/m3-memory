#!/usr/bin/env python3
"""
M3 Memory: Cross-Platform Schedule Installer.
Automatically configures crontab (macOS/Linux) or schtasks (Windows).
Uses project virtual environment paths and ensures log directories exist.
"""

import argparse
import os
import pathlib
import platform
import subprocess
import sys
import tempfile


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

def get_schedule_specs(m3_memory_root):
    """Return list of schedule specifications (normalized for Windows & Unix)."""
    python_exe = os.path.join(m3_memory_root, ".venv", "Scripts", "python.exe")
    if not os.path.exists(python_exe):
        python_exe = sys.executable

    log_dir = os.path.join(m3_memory_root, "logs")
    bin_posix = m3_memory_root.replace("\\", "/") + "/bin"
    maintenance_log = os.path.join(log_dir, "maintenance.log")

    return [
        {
            "name": "AgentOS_WeeklyAuditor",
            "cmd": f'"{python_exe}" "{os.path.join(m3_memory_root, "bin", "weekly_auditor.py")}" >> "{os.path.join(log_dir, "auditor.log")}" 2>&1',
            "schedule": "WEEKLY",
            "modifier": "FRI",
            "time": "16:00",
            "description": "Run weekly auditor on Fridays at 4pm"
        },
        {
            "name": "AgentOS_HourlySync",
            "cmd": f'"{python_exe}" "{os.path.join(m3_memory_root, "bin", "sync_all.py")}" >> "{os.path.join(log_dir, "sync_all.log")}" 2>&1',
            "schedule": "HOURLY",
            "modifier": "1",
            "time": "00:00",
            "description": "Hourly sync: SQLite <-> PostgreSQL + ChromaDB"
        },
        {
            "name": "AgentOS_Maintenance",
            "cmd": f'"{python_exe}" -c "import sys; sys.path.insert(0, \'{bin_posix}\'); import memory_maintenance; memory_maintenance.memory_maintenance_impl()" >> "{maintenance_log}" 2>&1',
            "schedule": "DAILY",
            "modifier": "",
            "time": "03:00",
            "description": "Daily memory maintenance (decay, prune orphans)"
        },
        {
            "name": "AgentOS_SecretRotator",
            "cmd": f'"{python_exe}" "{os.path.join(m3_memory_root, "bin", "secret_rotator.py")}" >> "{os.path.join(log_dir, "secret_rotator.log")}" 2>&1',
            "schedule": "MONTHLY",
            "modifier": "1",
            "time": "02:00",
            "description": "Monthly automated secret rotation"
        },
        {
            "name": "AgentOS_ChatlogEmbedSweep",
            "cmd": f'"{python_exe}" "{os.path.join(m3_memory_root, "bin", "chatlog_embed_sweeper.py")}" --batch 256 --max-per-run 10000 >> "{os.path.join(log_dir, "chatlog_embed_sweep.log")}" 2>&1',
            "schedule": "MINUTE",
            "modifier": "30",
            "time": "00:00",
            "description": "Embed un-embedded chat_log rows using the local embedding server"
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


def install_windows_tasks(m3_memory_root, selector: str | None = None):
    python_exe = os.path.join(m3_memory_root, ".venv", "Scripts", "python.exe")
    if not os.path.exists(python_exe):
        python_exe = sys.executable
        _safe_print(f"{WARN} Using system Python {python_exe} because .venv was not found.")

    log_dir = os.path.join(m3_memory_root, "logs")
    os.makedirs(log_dir, exist_ok=True)

    tasks = _filter_tasks(get_schedule_specs(m3_memory_root), selector)
    if not tasks:
        _safe_print(f"{FAIL} No schedule matches selector={selector!r}. Try --list to see all.")
        return

    success = True
    for task in tasks:
        subprocess.run(["schtasks", "/Delete", "/TN", task["name"], "/F"], capture_output=True)
        schtasks_cmd = [
            "schtasks", "/Create", "/TN", task["name"],
            "/TR", task["cmd"],
            "/SC", task["schedule"],
            "/ST", task["time"],
            "/F",
        ]
        if task["modifier"]:
            # /D for WEEKLY+MONTHLY day-of-week, /MO for interval-based (MINUTE/HOURLY).
            flag = "/D" if task["schedule"] in ("WEEKLY", "MONTHLY") else "/MO"
            schtasks_cmd.extend([flag, task["modifier"]])

        result = subprocess.run(schtasks_cmd, capture_output=True, text=True)
        if result.returncode == 0:
            _safe_print(f"{OK} Created Windows Task: {task['name']}")
        else:
            _safe_print(f"{FAIL} Failed to create task {task['name']}: {result.stderr.strip()}")
            success = False

    if success:
        _safe_print(f"{OK} Finished installing {len(tasks)} Windows scheduled task(s).")
        _safe_print(f"   Logs available in: {log_dir}")


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
    args = parser.parse_args()

    if args.list:
        list_schedules(m3_memory_root)
        return

    os_name = platform.system()
    _safe_print(f"M3 Memory: Detecting platform... {os_name}")
    _safe_print(f"Project root: {m3_memory_root}")

    if not args.add and not args.remove:
        _safe_print("Nothing to do. Use --list, --add NAME, or --remove NAME.")
        _safe_print("(Running with no flags used to install everything — now a no-op for safety.)")
        return

    selector = None if (args.add == "all" or args.remove == "all") else (args.add or args.remove)

    if args.add:
        if os_name == "Windows":
            install_windows_tasks(m3_memory_root, selector)
        elif os_name in ("Darwin", "Linux"):
            if selector:
                _safe_print(f"{WARN} Unix crontab installer currently rewrites all entries. "
                            "Single-task add on Unix is not supported yet — use --add all or edit "
                            "crontab directly.")
            else:
                install_unix_crontab(m3_memory_root)
        else:
            _safe_print(f"Unsupported OS: {os_name}")
        return

    if args.remove:
        if os_name == "Windows":
            remove_windows_tasks(selector, m3_memory_root)
        else:
            _safe_print(f"{WARN} Unix removal: edit crontab directly with `crontab -e`.")
        return


if __name__ == "__main__":
    main()
