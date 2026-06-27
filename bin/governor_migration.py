#!/usr/bin/env python3
"""Detect legacy m3 scheduled tasks (cron / schtasks / launchd / systemd) and
migrate the governor-eligible ones to the Adaptive Background Workload Governor.

The governor paces periodic, interruptible, resource-using background work based
on live host load + idle time (see docs/M3V3_OXIDATION.md). Once a task runs
under the governor's loop, its rigid scheduler entry is redundant and should be
removed so the two don't double-fire.

Not every scheduled task is a governor candidate. We split them:

  GOVERNOR-ELIGIBLE (periodic, interruptible, resource-using — safe to migrate):
    AgentOS_HourlySync          PG/Chroma sync (WAL + network)
    AgentOS_ChatlogEmbedSweep   GPU embedding backfill
    AgentOS_ObservationDrain    LLM fact-extraction from chatlog
    AgentOS_Maintenance         decay / prune (DB writes)
    AgentOS_WeeklyAuditor       audit pass (low-priority)

  NOT MIGRATABLE (left on their schedule — the governor cannot/should not own them):
    AgentOS_SecretRotator       security/compliance-anchored: rotation must happen
                                on a fixed cadence, NOT "whenever the host is idle"
                                (an always-busy machine would defer rotation
                                indefinitely — a security regression).
    AgentOS_CognitiveLoop       already a keepalive service that calls the governor
                                INSIDE its loop (m3_cognitive_loop.py). It is not a
                                periodic scheduler entry to remove; the governor
                                already paces it.

This module is pure detection + command-generation + best-effort removal. It does
NOT register the governor loops (that is install_schedules / the daemon's job) —
it only clears the legacy schedules that would otherwise double-fire.
"""
from __future__ import annotations

import os
import subprocess
import sys

# Task names that the governor can take over. Keep in sync with
# install_schedules.get_schedule_specs().
GOVERNOR_ELIGIBLE = (
    "AgentOS_HourlySync",
    "AgentOS_ChatlogEmbedSweep",
    "AgentOS_ObservationDrain",
    "AgentOS_Maintenance",
    "AgentOS_WeeklyAuditor",
)

# (name, reason) — surfaced to the user so they know WHY these stay scheduled.
NOT_MIGRATABLE = (
    ("AgentOS_SecretRotator",
     "security/compliance-anchored — rotation must run on a fixed cadence, "
     "not when the host happens to be idle"),
    ("AgentOS_CognitiveLoop",
     "already a governor-paced keepalive service (m3_cognitive_loop.py calls "
     "get_governor_pacing), not a periodic scheduler entry"),
)


def _os_name() -> str:
    """Constant-time OS branch (no WMI; matches install_schedules._os_name)."""
    if os.name == "nt":
        return "Windows"
    if sys.platform == "darwin":
        return "Darwin"
    return "Linux"


def detect_scheduled_tasks() -> dict[str, list[str]]:
    """Return {"eligible": [...], "not_migratable_present": [...]}.

    `eligible` lists the governor-eligible task names that are CURRENTLY
    installed on this host. `not_migratable_present` lists installed tasks we
    deliberately leave alone (reported so the user has the full picture).

    Detection is read-only and never raises — a missing scheduler tool yields
    empty lists.
    """
    installed = _list_installed_task_names()
    eligible = [t for t in GOVERNOR_ELIGIBLE if t in installed]
    not_migratable = [name for name, _ in NOT_MIGRATABLE if name in installed]
    return {"eligible": eligible, "not_migratable_present": not_migratable}


def _list_installed_task_names() -> set[str]:
    """Best-effort set of installed AgentOS_* scheduler entries on this host."""
    os_name = _os_name()
    names: set[str] = set()
    candidates = [n for n in GOVERNOR_ELIGIBLE] + [n for n, _ in NOT_MIGRATABLE]

    if os_name == "Windows":
        try:
            # /FO LIST is stable to parse; /TN filters to one task at a time so a
            # single missing task doesn't blank the whole query.
            for name in candidates:
                r = subprocess.run(
                    ["schtasks", "/Query", "/TN", name],
                    capture_output=True, text=True,
                )
                if r.returncode == 0 and name in r.stdout:
                    names.add(name)
        except FileNotFoundError:
            pass
        return names

    # Unix: cron entries carry the script basenames, not the AgentOS_* names.
    # Map each scheduler name to the script its crontab line invokes.
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        cron = r.stdout if r.returncode == 0 else ""
    except FileNotFoundError:
        cron = ""
    for name, marker in _UNIX_CRON_MARKERS.items():
        if marker and marker in cron:
            names.add(name)
    return names


# Map AgentOS_* names to the script basename that appears in the Unix crontab
# line, so detection works on macOS/Linux where cron carries scripts not names.
_UNIX_CRON_MARKERS = {
    "AgentOS_HourlySync": "sync_all.py",
    "AgentOS_ChatlogEmbedSweep": "chatlog_embed_sweeper.py",
    "AgentOS_ObservationDrain": "m3_enrich.py",
    "AgentOS_Maintenance": "memory_maintenance.py",
    "AgentOS_WeeklyAuditor": "weekly_auditor.py",
    "AgentOS_SecretRotator": "secret_rotator.py",
    "AgentOS_CognitiveLoop": "m3_cognitive_loop.py",
}


def try_remove_scheduled_tasks(names: list[str]) -> tuple[list[str], list[str]]:
    """Best-effort removal of the named scheduler entries with CURRENT privileges.

    Returns (removed, failed). On Windows a task owned by another user or
    requiring elevation fails cleanly (non-zero schtasks exit) and lands in
    `failed`, so the caller can surface the privileged commands. On Unix we can
    only rewrite the CURRENT user's crontab; system crontabs need the privileged
    commands.
    """
    if not names:
        return [], []
    os_name = _os_name()
    removed: list[str] = []
    failed: list[str] = []

    if os_name == "Windows":
        for name in names:
            r = subprocess.run(
                ["schtasks", "/Delete", "/TN", name, "/F"],
                capture_output=True, text=True,
            )
            (removed if r.returncode == 0 else failed).append(name)
        return removed, failed

    # Unix: rewrite the user crontab, dropping the matched lines.
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if r.returncode != 0:
            return [], list(names)
        lines = r.stdout.splitlines()
    except FileNotFoundError:
        return [], list(names)

    markers = {n: _UNIX_CRON_MARKERS.get(n, "") for n in names}
    kept, dropped = [], set()
    for line in lines:
        hit = next((n for n, m in markers.items() if m and m in line), None)
        if hit:
            dropped.add(hit)
        else:
            kept.append(line)
    if dropped:
        import tempfile
        new_cron = "\n".join(kept).strip() + "\n"
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
            tmp.write(new_cron)
            tmp_path = tmp.name
        try:
            w = subprocess.run(["crontab", tmp_path], capture_output=True, text=True)
            if w.returncode == 0:
                removed.extend(sorted(dropped))
            else:
                failed.extend(sorted(dropped))
        finally:
            os.unlink(tmp_path)
    # Anything we couldn't find a marker line for is reported failed.
    failed.extend(n for n in names if n not in dropped)
    return removed, sorted(set(failed))


def privileged_removal_commands(names: list[str]) -> list[str]:
    """OS-specific commands an admin can run to remove the named tasks cleanly.

    Returned as copy-pasteable lines (one command per task) for the wizard's
    end-of-run summary when in-process removal lacked permission.
    """
    if not names:
        return []
    os_name = _os_name()
    if os_name == "Windows":
        # Run from an ELEVATED (Administrator) PowerShell/cmd.
        return [f'schtasks /Delete /TN "{name}" /F' for name in names]
    if os_name == "Darwin":
        # macOS cron is per-user; a system entry would be under /etc or a
        # LaunchDaemon. Most installs are user crontab — `crontab -e` is the
        # clean editor path; the grep -v one-liner removes by script marker.
        cmds = ["# Edit your crontab and delete the m3 lines:", "crontab -e"]
        for name in names:
            m = _UNIX_CRON_MARKERS.get(name, name)
            cmds.append(f"# or remove {name} non-interactively:")
            cmds.append(f"crontab -l | grep -v '{m}' | crontab -")
        return cmds
    # Linux
    cmds = ["# Edit your crontab and delete the m3 lines:", "crontab -e"]
    for name in names:
        m = _UNIX_CRON_MARKERS.get(name, name)
        cmds.append(f"# or remove {name} non-interactively:")
        cmds.append(f"crontab -l | grep -v '{m}' | crontab -")
    cmds.append("# If installed as a system/root crontab, prefix with sudo:")
    cmds.append("#   sudo crontab -l -u <user> | grep -v 'm3' | sudo crontab -u <user> -")
    return cmds


def not_migratable_lines() -> list[str]:
    """Human-readable 'these stay scheduled, and why' lines for the summary."""
    return [f"  • {name} — {reason}" for name, reason in NOT_MIGRATABLE]
