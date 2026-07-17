#!/usr/bin/env python3
"""Autonomous procedural distillation (tasks → reusable `procedure` memories).

Rolls up successful (completed) task runs — a task plus its step/result
memories — into reusable `procedure` memories using a pluggable, local-first
(cloud-capable) model. The engine is memory_distill_procedures_impl; this script
is the *trigger and policy* around it. Procedures link back to their source
memories via `distills_from` edges, and — unlike belief consolidation — the
sources are PRESERVED (never soft-deleted): a procedure augments history, it
doesn't replace it.

Model selection (M3_DISTILL_MODEL): unset/"slm" → the local `procedure_local`
SLM profile (sovereign default); "llm" → largest local model; any other value →
a profile name (another local model, or a cloud endpoint via a
`backend: anthropic|openai` profile). Local-first by default, cloud by config.

Gated by M3_DISTILL_AUTO (default off): when the flag is unset, this runs in
DRY-RUN regardless of --apply, so a scheduled/loop invocation is a safe no-op
until the operator opts in. Pass --apply AND set M3_DISTILL_AUTO=1 to write.

Usage:
    python bin/distill_procedures.py [--apply] [--threshold N] [--stale-days N]
                                     [--max-procedures N] [--log-file PATH]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Default policy: give a completed task a few days to settle before distilling,
# and cap procedures per run so a backlog can't runaway on first enable.
DEFAULT_THRESHOLD = 1
DEFAULT_STALE_DAYS = 3
DEFAULT_MAX_PROCEDURES = 20


def _should_yield_to_user(min_idle_s: float = 30.0) -> "str | None":
    """Return a skip-reason if the host is too busy / the user is active to run
    a model-heavy distillation right now, else None. Mirrors the background-yield
    contract every other expensive m3 pass honors. Best-effort — never block on a
    telemetry/governor error."""
    try:
        import time

        from m3_sdk import _LAST_USER_INTERACTION  # type: ignore[attr-defined]
        if time.time() - _LAST_USER_INTERACTION < min_idle_s:
            return f"user active in the last {min_idle_s:.0f}s"
    except Exception:  # noqa: BLE001
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


async def _run(apply: bool, threshold: int, stale_days: int, max_procedures: int) -> str:
    import memory_maintenance

    # Hard gate: writing requires BOTH --apply and M3_DISTILL_AUTO=1.
    auto = os.environ.get("M3_DISTILL_AUTO", "0") == "1"
    dry_run = not (apply and auto)
    if apply and not auto:
        prefix = ("[skipped-apply] M3_DISTILL_AUTO is not set — running DRY-RUN. "
                  "Set M3_DISTILL_AUTO=1 to enable autonomous procedure writes.\n")
    else:
        prefix = ""

    # Governor/activity yield — ONLY for real writes (a dry-run is cheap + read-only).
    if not dry_run:
        reason = _should_yield_to_user()
        if reason:
            return (f"[deferred] procedural distillation skipped — {reason}. "
                    "Will retry on the next pass.\n")

    out = await memory_maintenance.memory_distill_procedures_impl(
        stale_days=stale_days,
        threshold=threshold,
        max_procedures=max_procedures,
        dry_run=dry_run,
    )
    return prefix + out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Autonomous procedural distillation (tasks → procedures)."
    )
    parser.add_argument("--apply", action="store_true",
                        help="Write procedures (requires M3_DISTILL_AUTO=1); else dry-run.")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                        help=f"Min completed tasks before distilling (default {DEFAULT_THRESHOLD}).")
    parser.add_argument("--stale-days", type=int, default=DEFAULT_STALE_DAYS,
                        help=f"Only distill tasks completed > N days ago (default {DEFAULT_STALE_DAYS}).")
    parser.add_argument("--max-procedures", type=int, default=DEFAULT_MAX_PROCEDURES,
                        help=f"Max procedures written per run (default {DEFAULT_MAX_PROCEDURES}).")

    from _task_runtime import add_log_file_arg, setup_task_runtime
    add_log_file_arg(parser)
    args = parser.parse_args()

    setup_task_runtime(
        args.log_file,
        lock_name="distill_procedures",
        logger_name="distill_procedures",
    )

    print(asyncio.run(_run(args.apply, args.threshold, args.stale_days, args.max_procedures)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
