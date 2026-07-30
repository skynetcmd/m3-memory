"""Cognitive-loop dormancy probe — is the background derived-knowledge engine
installed, running, and actually producing entities?

Nothing else surfaces this. ``schedule_probe`` only flags DANGLING jobs (an
installed job whose interpreter moved on disk); a cognitive loop that was NEVER
installed, or is installed but not loaded, or is running but has extracted zero
entities, all read as "✅ schedules: OK". Yet with the loop dormant the entire
DERIVED layer — entity extraction, belief consolidation, reflection — is inert:
the store hoards raw memories but never distills them (``m3 entity
entity_search`` stays empty, the knowledge graph never fills).

Report-only (DESIGN §3): always returns 0 and never crashes the doctor. Whether
to run the loop is the user's operational choice, so this NAGS with the
one-command fix rather than failing the run. Cross-OS detection (launchd /
systemd --user / Task Scheduler) so it degrades gracefully rather than going
blind on 2 of 3 platforms (DESIGN §1). The entity/memory counts come through the
storage seam's ``open_readonly`` so the same probe works on SQLite and Postgres.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

# Fixed install destinations — mirror install_schedules.py / schedule_probe.py so
# "where the loop lives" is single-sourced across the three files that care.
_LAUNCHD_LABEL = "com.m3memory.cognitiveloop"
_LAUNCHD_PLIST = "~/Library/LaunchAgents/com.m3memory.cognitiveloop.plist"
_SYSTEMD_UNIT = "m3-cognitive-loop.service"
_SYSTEMD_PATH = "~/.config/systemd/user/m3-cognitive-loop.service"
_WINDOWS_TASK = "AgentOS_CognitiveLoop"
_SCHTASKS_NOT_FOUND_RC = 1


def _installed_active() -> tuple[bool, Optional[bool], str]:
    """(installed, active, backend_label). ``active`` is True/False when the OS
    can tell us (launchd loaded / systemd active / schtask present+enabled), or
    None when it cannot. Never raises — a missing tool yields (False, None, …)."""
    if sys.platform == "darwin":
        dest = os.path.expanduser(_LAUNCHD_PLIST)
        if not os.path.exists(dest):
            return False, None, "launchd"
        try:
            loaded = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
            return True, (_LAUNCHD_LABEL in (loaded.stdout or "")), "launchd"
        except Exception:  # noqa: BLE001 — probe never crashes the doctor
            return True, None, "launchd"
    if sys.platform.startswith("linux"):
        dest = os.path.expanduser(_SYSTEMD_PATH)
        if not os.path.exists(dest):
            return False, None, "systemd --user"
        try:
            active = subprocess.run(
                ["systemctl", "--user", "is-active", _SYSTEMD_UNIT],
                capture_output=True, text=True,
            )
            return True, ((active.stdout or "").strip() == "active"), "systemd --user"
        except Exception:  # noqa: BLE001
            return True, None, "systemd --user"
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["schtasks", "/Query", "/TN", _WINDOWS_TASK],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                return False, None, "Task Scheduler"
            # ONSTART tasks read "Ready" between boots; installed+Ready is the best
            # signal Task Scheduler gives without a process check (done separately).
            return True, None, "Task Scheduler"
        except Exception:  # noqa: BLE001
            return False, None, "Task Scheduler"
    return False, None, sys.platform


def _process_running() -> Optional[bool]:
    """Best-effort: is an ``m3_cognitive_loop`` process alive right now? Returns
    None when we can't tell (no pgrep/tasklist). Complements the service-state
    check — a Windows ONSTART task in particular is "installed" long before we
    can confirm the daemon is actually up."""
    try:
        if sys.platform == "win32":
            # tasklist can't match on the script arg; WMIC can read the command
            # line. Best-effort — absence of WMIC just yields None (unknown).
            proc = subprocess.run(
                ["wmic", "process", "where",
                 "name like '%python%'", "get", "commandline"],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                return None
            return "m3_cognitive_loop" in (proc.stdout or "")
        proc = subprocess.run(
            ["pgrep", "-f", "m3_cognitive_loop"], capture_output=True, text=True
        )
        # pgrep: rc 0 = match, 1 = no match, 2/3 = usage/error → unknown.
        if proc.returncode in (0, 1):
            return proc.returncode == 0
        return None
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — probe never crashes the doctor
        return None


def _entity_stats() -> Optional[tuple[int, int]]:
    """(durable_memories, extracted_entities) via the storage seam, or None if
    the store/tables aren't readable yet. Backend-agnostic (SQLite/PG) through
    ``open_readonly``. A fresh store with no ``entities`` table reads as 0."""
    bin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    try:
        from m3_core.paths import resolve_engine_file
        from memory.backends import active_backend
    except Exception:  # noqa: BLE001
        return None
    try:
        db_path = resolve_engine_file("agent_memory.db")
        with active_backend().open_readonly(db_path) as conn:
            mem = conn.execute(
                "SELECT COUNT(*) FROM memory_items WHERE is_deleted = 0"
            ).fetchone()
            memories = int(mem[0]) if mem else 0
            try:
                ent = conn.execute("SELECT COUNT(*) FROM entities").fetchone()
                entities = int(ent[0]) if ent else 0
            except Exception:  # noqa: BLE001 — no entities table yet == 0 extracted
                entities = 0
        return memories, entities
    except Exception:  # noqa: BLE001 — missing store on a fresh box is fine
        return None


def _fix_lines() -> list[str]:
    return [
        "  fix      : install + start the cognitive loop:",
        "               m3 setup                      # answer yes at the prompt",
        "             or, non-interactively:",
        "               m3 install-m3 --cognitive-loop",
    ]


def run(brief: bool = False) -> int:
    """Report cognitive-loop dormancy. Always returns 0 (report-only)."""
    try:
        installed, active, backend = _installed_active()
        running = _process_running()
        stats = _entity_stats()
    except Exception as e:  # noqa: BLE001 — a probe must never crash the doctor
        if not brief:
            print()
            print("=== cognitive loop ===")
            print(f"  status   : could not run probe: {type(e).__name__}: {e}")
        return 0

    memories = entities = None
    if stats is not None:
        memories, entities = stats

    # Only NAG on a POSITIVE "not running" signal so a probe blind spot can't cry
    # wolf: an installed service that the OS reports inactive AND no live process.
    # Unknown states (None) never downgrade the verdict.
    definitely_down = installed and (active is False) and (running is not True)

    if brief:
        if not installed and running is not True:
            print("⚠️  cognitive loop: not installed — derived knowledge won't build; `m3 setup`")
        elif definitely_down:
            print("⚠️  cognitive loop: installed but not running; `m3 setup`")
        elif memories and entities == 0:
            print("⚠️  cognitive loop: 0 entities extracted from "
                  f"{memories} memories (loop hasn't distilled yet)")
        else:
            suffix = f" · {entities} entities" if entities else ""
            print(f"✅ cognitive loop: OK ({backend}{suffix})")
        return 0

    print()
    print("=== cognitive loop ===")
    print(f"  backend  : {backend}")
    state = "installed" if installed else "NOT installed"
    if active is True:
        state += ", active"
    elif active is False:
        state += ", inactive"
    if running is True:
        state += ", process up"
    elif running is False and installed:
        state += ", no process"
    print(f"  service  : {state}")
    if memories is not None:
        print(f"  entities : {entities} extracted from {memories} durable memories")

    if not installed and running is not True:
        print("  status   : NAG — the cognitive loop is not installed. Entity")
        print("             extraction, belief consolidation, and reflection are the")
        print("             loop's job; without it the derived layer stays empty no")
        print("             matter how many memories are captured.")
        for ln in _fix_lines():
            print(ln)
        return 0

    if definitely_down:
        print("  status   : NAG — installed but not running. The derived-knowledge")
        print("             passes won't fire until it is loaded again.")
        for ln in _fix_lines():
            print(ln)
        return 0

    if memories and entities == 0:
        print("  status   : NOTE — the loop is live but no entities have been")
        print("             extracted yet. This is normal right after install (the")
        print("             backlog drains over time) or when no LLM endpoint is")
        print("             reachable for the extractor. Backfill the existing store")
        print("             in one pass with:")
        print("               m3 admin extract_pending --yes")
        return 0

    print("  status   : OK — the loop is live and the derived layer is populating.")
    return 0
