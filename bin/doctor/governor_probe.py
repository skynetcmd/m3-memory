"""Governor-schedule probe — are legacy cron/schtasks entries still installed?

The Adaptive Background Workload Governor paces periodic background work by host
load + idle time instead of a rigid clock (see docs/M3V3_OXIDATION.md). Once the
governor is active, leftover cron/schtasks entries for governor-eligible tasks
**double-fire** the work — defeating the whole point (they ignore load and run
on the clock).

This probe makes that state visible (DESIGN §3 fail-loud): if any
governor-eligible scheduled task is still installed, it nags and prints the
one-command fix, plus the privileged OS-specific commands if removal needs
elevation. It never fails the doctor run — leaving the cron entries in place is a
supported (if suboptimal) choice — it only reports, returning 0 always.
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger("memory.doctor.governor_probe")


def run() -> int:
    """Report whether governor-eligible scheduled tasks are still installed.

    Always returns 0 (report-only). Prints a nag + fix command when legacy
    schedules are found that the governor should own.
    """
    print()
    print("=== background governor (scheduled-task migration) ===")

    # bin/ is on sys.path when run via memory_doctor; be defensive anyway.
    bin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    try:
        import governor_migration as gm
    except Exception as e:  # noqa: BLE001 — probe must never crash the doctor
        print(f"  status   : could not load governor_migration: {type(e).__name__}: {e}")
        return 0

    try:
        detected = gm.detect_scheduled_tasks()
    except Exception as e:  # noqa: BLE001
        print(f"  status   : detection failed: {type(e).__name__}: {e}")
        return 0

    eligible = detected.get("eligible", [])

    if not eligible:
        print("  status   : OK — no governor-eligible cron/schtasks entries found.")
        print("             Background work is paced by the governor (load + idle aware).")
        return 0

    # NAG: legacy schedules present that the governor should own.
    print(f"  status   : NAG — {len(eligible)} legacy scheduled task(s) still installed that")
    print("             the governor should own. They run on a rigid clock and will")
    print("             double-fire alongside the load-aware governor:")
    for name in eligible:
        print(f"             - {name}")
    print()
    print("  why      : a scheduler asks 'what time is it?'; the governor asks 'is now")
    print("             a good time?'. Leaving these scheduled means background sync /")
    print("             embedding / maintenance can fire mid-session and contend for")
    print("             CPU/GPU/WAL — exactly what the governor exists to prevent.")
    print()
    print("  fix      : run the migration (removes them with your current privileges):")
    print("               m3 governor migrate")
    print("             or via the setup wizard:")
    print("               m3 setup")

    # If we can already tell removal will need elevation, surface the commands.
    cmds = gm.privileged_removal_commands(eligible)
    if cmds:
        print()
        print("  if the above lacks permission, run these PRIVILEGED, OS-specific")
        print("  commands (elevated / as the task owner), then re-run `m3 doctor`:")
        for c in cmds:
            print(f"             {c}")
    return 0
