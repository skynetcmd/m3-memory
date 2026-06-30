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
    not_migratable = [name for name, _ in NOT_MIGRATABLE if name in installed]
    eligible = [t for t in GOVERNOR_ELIGIBLE if t in installed]
    # Legacy / hand-named tasks (Windows, detected by action) are neither in
    # GOVERNOR_ELIGIBLE nor NOT_MIGRATABLE — they carry their real task name. They
    # ARE governor-eligible (an old sync task IS the thing the governor owns), so
    # fold them into `eligible` so the CLI removes them by their actual name.
    # Guard against a hand-named task colliding with a canonical name (already
    # counted) or a not-migratable name (don't reclassify it for removal).
    extra = [
        n for n in sorted(installed)
        if n not in GOVERNOR_ELIGIBLE
        and n not in {nm for nm, _ in NOT_MIGRATABLE}
        and n not in not_migratable
    ]
    eligible.extend(extra)
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
        # Also catch legacy / hand-named tasks (e.g. the pre-bf110222
        # `m3-memory-sync`) that the canonical-name query above misses. These are
        # surfaced under their ACTUAL task name so removal targets the right task.
        names |= detect_windows_legacy_action_tasks()
        return names

    # Unix: cron entries carry the invoked command, not the AgentOS_* name.
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        cron = r.stdout if r.returncode == 0 else ""
    except FileNotFoundError:
        cron = ""
    return _unix_installed_from_cron(cron)


def _canonical_task_names() -> set[str]:
    """Every task name the current installer emits — canonical AgentOS_* set."""
    return set(GOVERNOR_ELIGIBLE) | {name for name, _ in NOT_MIGRATABLE}


def _leaf_task_name(task_name: str) -> str:
    """schtasks prints `\\Folder\\Name` (or `\\Name` at root) — return the leaf."""
    return task_name.strip().rsplit("\\", 1)[-1]


def detect_windows_legacy_action_tasks() -> set[str]:
    """Names of Windows tasks whose ACTION runs an m3 entrypoint but whose name is
    NOT a canonical AgentOS_* one (legacy / hand-named, e.g. `m3-memory-sync`).

    Returned as ACTUAL task names so the caller removes the right task. Matches on
    the "Task To Run" field via _WINDOWS_ACTION_MARKERS (which point at the Python
    entrypoints the action invokes directly, NOT the Unix `pg_sync.sh` wrapper).

    Read-only and never raises: a missing `schtasks` or a parse hiccup yields an
    empty set. Canonical-named tasks are excluded — they are already detected by
    the name-based query, and re-listing them here would be redundant.
    """
    try:
        r = subprocess.run(
            ["schtasks", "/Query", "/FO", "LIST", "/V"],
            capture_output=True, text=True,
        )
    except (FileNotFoundError, OSError):
        return set()
    if r.returncode != 0 or not r.stdout:
        return set()

    canonical = _canonical_task_names()
    markers = tuple(_WINDOWS_ACTION_MARKERS.values())
    found: set[str] = set()
    cur_name: str | None = None
    for raw in r.stdout.splitlines():
        line = raw.strip()
        if line.startswith("TaskName:"):
            cur_name = line.split(":", 1)[1].strip()
        elif line.startswith("Task To Run:") and cur_name:
            action = line.split(":", 1)[1]
            leaf = _leaf_task_name(cur_name)
            # Skip canonical tasks (already counted) and Microsoft/OS tasks that
            # merely mention a marker substring would be a non-issue — the markers
            # are m3 script filenames, vanishingly unlikely to appear elsewhere.
            if leaf not in canonical and any(m in action for m in markers):
                # Use the leaf name: schtasks /Delete /TN works with the leaf for
                # root-level tasks, which is where the installer (and the legacy
                # hand-named tasks) live.
                found.add(leaf)
            cur_name = None
    return found


def _unix_installed_from_cron(cron: str) -> set[str]:
    """Unix detection body — split out so the Windows path can return early."""
    names: set[str] = set()
    for name, marker in _UNIX_CRON_MARKERS.items():
        if marker and marker in cron:
            names.add(name)
    # The cognitive loop is a launchd/systemd service, not a cron line — detect
    # it by service-file presence.
    for name, path in _unix_service_paths().items():
        if path and os.path.exists(path):
            names.add(name)
    return names


# Map AgentOS_* names to the token that appears in the Unix CRON line, so
# detection works on macOS/Linux where cron carries the invoked command, not the
# AgentOS_* task name. NOTE the HourlySync line invokes the `pg_sync.sh` wrapper
# (which delegates to sync_all.py) — matching `sync_all.py` would MISS it, since
# only the .sh path appears in the crontab. See bin/crontab.template.
#
# AgentOS_CognitiveLoop is intentionally absent: on Unix it is NOT a cron entry —
# it is a launchd agent / systemd --user unit (see _UNIX_SERVICE_PATHS). It is
# detected by service-file presence instead.
_UNIX_CRON_MARKERS = {
    "AgentOS_HourlySync": "pg_sync.sh",
    "AgentOS_ChatlogEmbedSweep": "chatlog_embed_sweeper.py",
    "AgentOS_ObservationDrain": "m3_enrich.py",
    "AgentOS_Maintenance": "memory_maintenance.py",
    "AgentOS_WeeklyAuditor": "weekly_auditor.py",
    "AgentOS_SecretRotator": "secret_rotator.py",
}

# Map AgentOS_* names to the token that appears in the WINDOWS task ACTION
# ("Task To Run"), so we can recognise a legacy/hand-named scheduled task by what
# it RUNS even when its task name is not the canonical AgentOS_* one. This is the
# Windows analogue of _UNIX_CRON_MARKERS, but the markers DIFFER by design: the
# Windows task action invokes the Python entrypoint DIRECTLY (e.g. sync_all.py),
# whereas the Unix crontab invokes the `pg_sync.sh` wrapper. Reusing
# _UNIX_CRON_MARKERS here would make HourlySync look for `pg_sync.sh`, which never
# appears in a Windows action — so a legacy task like the old `m3-memory-sync`
# (action: ...\\sync_all.py) would be missed. Keep these two maps independent.
#
# AgentOS_CognitiveLoop is intentionally absent: it is a not-migratable keepalive
# the governor already paces, so we do not want a legacy-action match to flag it
# for removal.
_WINDOWS_ACTION_MARKERS = {
    "AgentOS_HourlySync": "sync_all.py",
    "AgentOS_ChatlogEmbedSweep": "chatlog_embed_sweeper.py",
    "AgentOS_ObservationDrain": "m3_enrich.py",
    "AgentOS_Maintenance": "memory_maintenance.py",
    "AgentOS_WeeklyAuditor": "weekly_auditor.py",
    "AgentOS_SecretRotator": "secret_rotator.py",
}


def _unix_service_paths() -> dict[str, str]:
    """AgentOS_* names → the launchd/systemd file path that means "installed".

    macOS uses a launchd plist; Linux uses a systemd --user unit. Only the
    cognitive loop is a service (the rest are cron). Resolved at call time so
    ``~`` expands for the current user."""
    if _os_name() == "Darwin":
        return {
            "AgentOS_CognitiveLoop":
                os.path.expanduser("~/Library/LaunchAgents/com.m3memory.cognitiveloop.plist"),
        }
    if _os_name() == "Linux":
        return {
            "AgentOS_CognitiveLoop":
                os.path.expanduser("~/.config/systemd/user/m3-cognitive-loop.service"),
        }
    return {}


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

    Returned as copy-pasteable lines for the wizard's end-of-run summary (and
    `m3 doctor`) when in-process removal lacked permission. Handles all three
    supported OSes:
      - Windows: `schtasks /Delete` per task (run elevated / as the task owner).
      - macOS / Linux: per-task `crontab` one-liners (cron entries), plus
        `launchctl`/`systemctl` for the cognitive-loop service if it appears.
    """
    if not names:
        return []
    os_name = _os_name()

    if os_name == "Windows":
        # Run from an ELEVATED (Administrator) PowerShell/cmd, or as the user
        # that owns the task.
        return [f'schtasks /Delete /TN "{name}" /F' for name in names]

    # Unix (Darwin / Linux). Split cron-backed tasks from the service-backed
    # cognitive loop so each gets the right removal command.
    cron_names = [n for n in names if n in _UNIX_CRON_MARKERS]
    service_names = [n for n in names if n in _unix_service_paths()]

    cmds: list[str] = []
    if cron_names:
        cmds.append("# Cron tasks — edit your crontab and delete the m3 lines:")
        cmds.append("crontab -e")
        cmds.append("# ...or remove each non-interactively by its command marker:")
        for name in cron_names:
            marker = _UNIX_CRON_MARKERS[name]
            cmds.append(f"crontab -l | grep -v '{marker}' | crontab -   # {name}")
        cmds.append("# If it was installed as another user's / a system crontab, use sudo:")
        cmds.append("#   sudo crontab -u <user> -l | grep -v 'm3-memory' | sudo crontab -u <user> -")

    if service_names:
        # Only reached if a service-backed task is in the failed set; in practice
        # the cognitive loop is not-migratable, but keep this correct.
        if os_name == "Darwin":
            cmds.append("# Cognitive-loop launchd agent:")
            cmds.append("launchctl unload ~/Library/LaunchAgents/com.m3memory.cognitiveloop.plist")
            cmds.append("rm ~/Library/LaunchAgents/com.m3memory.cognitiveloop.plist")
        else:  # Linux
            cmds.append("# Cognitive-loop systemd --user unit:")
            cmds.append("systemctl --user disable --now m3-cognitive-loop.service")
            cmds.append("rm ~/.config/systemd/user/m3-cognitive-loop.service")
            cmds.append("systemctl --user daemon-reload")
    return cmds


def not_migratable_lines() -> list[str]:
    """Human-readable 'these stay scheduled, and why' lines for the summary."""
    return [f"  • {name} — {reason}" for name, reason in NOT_MIGRATABLE]
