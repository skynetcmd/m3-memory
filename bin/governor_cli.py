#!/usr/bin/env python3
"""`m3 governor <status|migrate>` — inspect and migrate legacy scheduled tasks
to the Adaptive Background Workload Governor.

  status  — report which governor-eligible cron/schtasks entries are still
            installed (the same nag `m3 doctor` prints), plus the not-migratable
            tasks and why.
  migrate — remove the governor-eligible entries with current privileges; print
            the privileged OS-specific commands for any that need elevation.

Thin CLI over bin/governor_migration.py so the detection/removal logic stays in
one tested module. Always exits 0 on `status`; `migrate` exits 0 unless every
removal failed (so scripts can detect a no-op-due-to-permission).
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import governor_migration as gm  # noqa: E402


def _print_not_migratable() -> None:
    lines = gm.not_migratable_lines()
    if lines:
        print("\nLeft on their schedule (the governor cannot take these over):")
        for line in lines:
            print(line)


def cmd_status() -> int:
    detected = gm.detect_scheduled_tasks()
    eligible = detected.get("eligible", [])
    if not eligible:
        print("OK — no governor-eligible cron/schtasks entries found.")
        print("Background work is paced by the governor (load + idle aware).")
        _print_not_migratable()
        return 0
    print(f"NAG — {len(eligible)} legacy scheduled task(s) the governor should own:")
    for name in eligible:
        print(f"  - {name}")
    print("\nFix: run `m3 governor migrate` (or `m3 setup`).")
    cmds = gm.privileged_removal_commands(eligible)
    if cmds:
        print("\nIf removal needs elevation, run these PRIVILEGED commands:")
        for c in cmds:
            print(f"  {c}")
    _print_not_migratable()
    return 0


def cmd_migrate(*, yes: bool) -> int:
    detected = gm.detect_scheduled_tasks()
    eligible = detected.get("eligible", [])
    if not eligible:
        print("Nothing to migrate — no governor-eligible scheduled tasks installed.")
        _print_not_migratable()
        return 0

    print(f"Found {len(eligible)} governor-eligible scheduled task(s):")
    for name in eligible:
        print(f"  - {name}")

    if not yes:
        try:
            ans = input("\nRemove these and let the governor own them? [Y/n] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans in ("n", "no"):
            print("Aborted — no changes made.")
            return 0

    removed, failed = gm.try_remove_scheduled_tasks(eligible)
    for name in removed:
        print(f"  [OK] removed {name}")
    for name in failed:
        print(f"  [WARN] could not remove {name} (insufficient privilege?)")

    if failed:
        print("\nRun these PRIVILEGED, OS-specific commands to remove the rest")
        print("(elevated / as the task owner), then re-run `m3 doctor`:")
        for c in gm.privileged_removal_commands(failed):
            print(f"  {c}")

    _print_not_migratable()
    # Exit non-zero only if we removed nothing AND something failed (pure perm fail).
    return 1 if (failed and not removed) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="m3 governor",
        description="Inspect / migrate legacy scheduled tasks to the background governor.",
    )
    sub = parser.add_subparsers(dest="action", metavar="<status|migrate>")
    sub.add_parser("status", help="Report governor-eligible scheduled tasks still installed.")
    p_mig = sub.add_parser("migrate", help="Remove governor-eligible scheduled tasks.")
    p_mig.add_argument("--yes", "-y", action="store_true",
                       help="Skip the confirmation prompt (headless use).")
    args = parser.parse_args(argv)

    if args.action == "migrate":
        return cmd_migrate(yes=args.yes)
    # Default (no action or 'status') -> status.
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
