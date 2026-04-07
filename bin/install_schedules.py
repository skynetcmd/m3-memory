#!/usr/bin/env python3
"""
M3 Max Agentic OS: Cross-Platform Schedule Installer.
Automatically configures crontab (macOS/Linux) or schtasks (Windows).
Uses project virtual environment paths and ensures log directories exist.
"""

import os
import sys
import platform
import subprocess
import tempfile
import pathlib

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
        print("✅ Successfully installed crontab schedules for macOS/Linux.")
        print(f"   Logs available in: {log_dir}")
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to install crontab: {e}")
    finally:
        os.unlink(tmp_path)

def install_windows_tasks(m3_memory_root):
    # Determine the project-specific python executable for Windows
    python_exe = os.path.join(m3_memory_root, ".venv", "Scripts", "python.exe")
    if not os.path.exists(python_exe):
        # Fallback to sys.executable if .venv not present (less reliable)
        python_exe = sys.executable
        print(f"⚠️ Warning: Using system Python {python_exe} because .venv was not found.")
    
    # Ensure logs directory exists
    log_dir = os.path.join(m3_memory_root, "logs")
    os.makedirs(log_dir, exist_ok=True)

    tasks = [
        {
            "name": "AgentOS_WeeklyAuditor",
            "cmd": f'"{python_exe}" "{os.path.join(m3_memory_root, "bin", "weekly_auditor.py")}" >> "{os.path.join(log_dir, "auditor.log")}" 2>&1',
            "schedule": "WEEKLY",
            "modifier": "FRI",
            "time": "16:00"
        },
        {
            "name": "AgentOS_HourlySync",
            "cmd": f'"{python_exe}" "{os.path.join(m3_memory_root, "bin", "sync_all.py")}" >> "{os.path.join(log_dir, "sync_all.log")}" 2>&1',
            "schedule": "HOURLY",
            "modifier": "1",
            "time": "00:00"
        },
        {
            "name": "AgentOS_Maintenance",
            "cmd": f'"{python_exe}" -c "import sys; sys.path.insert(0, \'{m3_memory_root.replace("\\", "/")}/bin\'); import memory_maintenance; memory_maintenance.memory_maintenance_impl()" >> "{os.path.join(log_dir, "maintenance.log")}" 2>&1',
            "schedule": "DAILY",
            "modifier": "",
            "time": "03:00"
        },
        {
            "name": "AgentOS_SecretRotator",
            "cmd": f'"{python_exe}" "{os.path.join(m3_memory_root, "bin", "secret_rotator.py")}" >> "{os.path.join(log_dir, "secret_rotator.log")}" 2>&1',
            "schedule": "MONTHLY",
            "modifier": "1",
            "time": "02:00"
        }
    ]

    success = True
    for task in tasks:
        # Delete existing task if it exists
        subprocess.run(["schtasks", "/Delete", "/TN", task["name"], "/F"], capture_output=True)
        
        # Create new task
        schtasks_cmd = [
            "schtasks", "/Create", "/TN", task["name"],
            "/TR", task["cmd"],
            "/SC", task["schedule"],
            "/ST", task["time"],
            "/F"
        ]
        
        if task["modifier"]:
            schtasks_cmd.extend(["/D", task["modifier"]])
            
        result = subprocess.run(schtasks_cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ Created Windows Task: {task['name']}")
        else:
            print(f"❌ Failed to create task {task['name']}: {result.stderr}")
            success = False

    if success:
        print("✅ Successfully installed Windows scheduled tasks.")
        print(f"   Logs available in: {log_dir}")

def main():
    # Resolve the absolute path of the project root
    script_dir = pathlib.Path(__file__).parent.resolve()
    m3_memory_root = str(script_dir.parent)
    
    os_name = platform.system()

    print(f"M3 Max Agentic OS: Detecting platform... {os_name}")
    print(f"Project root: {m3_memory_root}")
    
    if os_name == "Windows":
        install_windows_tasks(m3_memory_root)
    elif os_name in ["Darwin", "Linux"]:
        install_unix_crontab(m3_memory_root)
    else:
        print(f"Unsupported OS for automated scheduling: {os_name}")
        print("Please configure your native task scheduler manually using bin/crontab.template as a guide.")

if __name__ == "__main__":
    main()
