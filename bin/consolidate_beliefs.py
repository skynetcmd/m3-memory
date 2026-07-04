#!/usr/bin/env python3
"""Autonomous episodic->semantic belief consolidation (knowledge-maintenance P4).

Rolls up large groups of episodic `observation` memories into stable, high-order
`belief` memories using the local LLM — the engine is memory_consolidate_impl;
this script is the *trigger and policy* around it. Beliefs link back to their
sources via `consolidates` edges and the sources are soft-deleted (never purged),
so a belief is always reversible and its provenance reconstructable.

Gated by M3_CONSOLIDATION_AUTO (default off): when the flag is unset, this runs in
DRY-RUN regardless of --apply, so a scheduled invocation is a safe no-op until the
operator opts in. Pass --apply AND set M3_CONSOLIDATION_AUTO=1 to actually write.

Scheduled weekly (see crontab.template / install_schedules.py). Protected types
(preference/user_fact/task/plan) are never consolidated — inherited from
memory_consolidate_impl's defaults.

Usage:
    python bin/consolidate_beliefs.py [--apply] [--threshold N] [--stale-days N]
                                      [--source-type observation] [--log-file PATH]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Default policy: only consolidate when a group has well past a session's worth of
# observations, and only ones that have aged a little (so live conversation isn't
# rolled up out from under an active agent).
DEFAULT_THRESHOLD = 50      # mirrors M3_REFLECTOR_THRESHOLD — a session's worth
DEFAULT_STALE_DAYS = 7      # leave the last week of episodic memory intact
DEFAULT_SOURCE_TYPE = "observation"
DEFAULT_BELIEF_CAP = 20     # max belief groups written per run (anti-runaway)


def _should_yield_to_user(min_idle_s: float = 30.0) -> "str | None":
    """Return a skip-reason if the host is too busy or the user is active to run
    an LLM-heavy consolidation right now, else None. Mirrors the background-yield
    contract every other expensive m3 pass honors (memory_maintenance,
    cognitive loop): never contend with an interactive session or a hot box.
    Best-effort — if telemetry/governor are unavailable, do NOT block."""
    try:
        import time

        # _LAST_USER_INTERACTION is lazily re-exported from m3_core.governor via
        # m3_sdk's module __getattr__, so it's invisible to static analysis.
        from m3_sdk import _LAST_USER_INTERACTION  # type: ignore[attr-defined]
        if time.time() - _LAST_USER_INTERACTION < min_idle_s:
            return f"user active in the last {min_idle_s:.0f}s"
    except Exception:  # noqa: BLE001 — never block on a telemetry import error
        return None
    try:
        from m3_sdk import M3Context, get_governor_pacing
        ctx = M3Context.for_db(os.environ.get("M3_DATABASE"))
        pacing = get_governor_pacing(ctx.get_system_telemetry())
        if pacing.get("background") == "HALTED":
            return "host load/activity critical (governor HALTED)"
    except Exception:  # noqa: BLE001
        pass
    return None


async def _run(apply: bool, threshold: int, stale_days: int, source_type: str) -> str:
    import memory_maintenance

    # Hard gate: writing requires BOTH --apply and M3_CONSOLIDATION_AUTO=1.
    auto = os.environ.get("M3_CONSOLIDATION_AUTO", "0") == "1"
    dry_run = not (apply and auto)
    if apply and not auto:
        prefix = ("[skipped-apply] M3_CONSOLIDATION_AUTO is not set — running DRY-RUN. "
                  "Set M3_CONSOLIDATION_AUTO=1 to enable autonomous belief writes.\n")
    else:
        prefix = ""

    # Governor/activity yield — ONLY for real writes. A dry-run is cheap and
    # read-only, so it always runs (gating it would make the scheduled no-op
    # confusing). A real consolidation is LLM-heavy, so it defers to an active
    # user or a loaded host, exactly like memory_maintenance and the cognitive
    # loop. The next scheduled/loop pass picks the work back up.
    if not dry_run:
        reason = _should_yield_to_user()
        if reason:
            return (f"[deferred] belief consolidation skipped — {reason}. "
                    "Will retry on the next pass.\n")

    out = await memory_maintenance.memory_consolidate_impl(
        type_filter=source_type,
        threshold=threshold,
        stale_days=stale_days,
        dry_run=dry_run,
        target_type="belief",
    )
    return prefix + out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Autonomous episodic->semantic belief consolidation."
    )
    parser.add_argument("--apply", action="store_true",
                        help="Write beliefs (requires M3_CONSOLIDATION_AUTO=1); else dry-run.")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                        help=f"Min group size before consolidating (default {DEFAULT_THRESHOLD}).")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS,
                        help=f"Only consolidate items older than N days (default {DEFAULT_STALE_DAYS}).")
    parser.add_argument("--source-type", default=DEFAULT_SOURCE_TYPE,
                        help=f"Episodic source memory type (default '{DEFAULT_SOURCE_TYPE}').")

    from _task_runtime import add_log_file_arg, setup_task_runtime
    add_log_file_arg(parser)
    args = parser.parse_args()

    setup_task_runtime(
        args.log_file,
        lock_name="consolidate_beliefs",
        logger_name="consolidate_beliefs",
    )

    print(asyncio.run(_run(args.apply, args.threshold, args.stale_days, args.source_type)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
