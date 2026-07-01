#!/usr/bin/env python3
"""m3-memory doctor — thin CLI dispatcher over the doctor phases.

Phases (each in its own module under bin/doctor/):

  - db_repair          legacy DB fixes (timestamps, relationships, JSON)
  - cascade_probe      embedding-cascade health (delegates to memory.doctor)
  - embed_server_probe Rust-side `m3-embed-server doctor` subprocess
  - oxidation_probe    m3_core_rs native-extension presence/staleness report

Each phase can be skipped via --skip-*. Exit code is the maximum across
the non-skipped phases (most-severe wins). The embed-server and oxidation
phases are report-only and never bump the exit code.

Design note: this file is intentionally thin — narrow CLI + phase
dispatch only. Logic lives in the bin/doctor/ submodules so each can be
tested in isolation.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format='%(name)s: [%(levelname)s] %(message)s')
logger = logging.getLogger("memory_doctor")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))


def main() -> int:
    # Quiet the llama.cpp/GGML backend process-wide BEFORE any probe loads the
    # bge-m3 GGUF. The cascade probe loads the embedder IN-PROCESS and the
    # embed-server probe loads it in a SUBPROCESS; both inherit this. Without it,
    # llama.cpp dumps its full per-tensor load trace + Metal teardown to stderr
    # (hundreds of lines), burying the doctor's readable summary. Level 4 =
    # error-only; harmless for an embedding model. setdefault() respects an
    # operator-set value so power users can opt back into the verbose logs.
    os.environ.setdefault("GGML_LOG_LEVEL", "4")

    from m3_sdk import add_database_arg

    parser = argparse.ArgumentParser(
        description="m3-memory doctor: DB repair + cascade health + Rust-side service check.",
    )
    add_database_arg(parser)
    parser.add_argument(
        "--skip-repair", action="store_true",
        help="Skip the legacy DB-repair phase (read-only health check).",
    )
    parser.add_argument(
        "--skip-cascade", action="store_true",
        help="Skip the embedding-cascade health probe.",
    )
    parser.add_argument(
        "--skip-embed-server", action="store_true",
        help="Skip the Rust-side m3-embed-server doctor subprocess.",
    )
    parser.add_argument(
        "--skip-oxidation", action="store_true",
        help="Skip the m3_core_rs native-extension status report.",
    )
    parser.add_argument(
        "--skip-governor", action="store_true",
        help="Skip the governor scheduled-task migration check.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Show the full detail (DB-repair steps + each probe's expanded "
             "report + model-load logs). Default is a compact one-line-per-check "
             "summary of high-yield verdicts.",
    )
    parser.add_argument(
        "--fix", action="store_true",
        help="Run quick-repair mode to auto-fix common deployment issues.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Use with --fix to simulate repair steps without making changes.",
    )
    args = parser.parse_args()

    if args.fix:
        import asyncio

        from memory.doctor import memory_doctor_fix_impl

        mode = "Dry-Run " if args.dry_run else ""
        print(f"==> Running m3-memory {mode}self-repair...")
        res = asyncio.run(memory_doctor_fix_impl(dry_run=args.dry_run))

        print(f"\nRepair Summary: {res['summary'].upper()}")
        print("-" * 50)
        for act in res["actions"]:
            status_char = "[OK]" if act["status"] == "ok" else "[SKIP]" if act["status"] == "skipped" else "[ERR]"
            print(f"  {status_char} {act['action']}")
            print(f"         Detail: {act['detail']}")
        print("-" * 50)

        if res["summary"] == "failed":
            return 1
        return 0

    exit_code = 0
    brief = not args.verbose  # brief is the DEFAULT; --verbose opts into detail

    # In brief mode the DB-repair phase's [INFO] chatter is noise — the overall
    # health line (from installer.doctor) already reflects DB health. Skip it
    # unless the operator explicitly asked to repair.
    if not args.skip_repair and not brief:
        from doctor import db_repair
        exit_code = max(exit_code, db_repair.run(args.database))

    if not args.skip_cascade:
        from doctor import cascade_probe
        exit_code = max(exit_code, cascade_probe.run(brief=brief))

    if not args.skip_embed_server:
        from doctor import embed_server_probe
        # Rust-side probe doesn't bump exit code on its own — operators
        # legitimately run m3 without `m3 embedder install`, and a missing
        # binary is not a Python-side failure.
        embed_server_probe.run(brief=brief)

    if not args.skip_oxidation:
        from doctor import oxidation_probe
        # Report-only: a pure-Python deployment (no/old wheel) is supported, so
        # this never bumps the exit code — it surfaces a stale wheel that would
        # otherwise degrade silently.
        oxidation_probe.run(brief=brief)

    if not args.skip_governor:
        from doctor import governor_probe
        # Report-only: leftover cron/schtasks entries are a suboptimal-but-
        # supported state, so this never bumps the exit code — it nags and
        # prints the fix command when the governor should own scheduled work.
        governor_probe.run(brief=brief)

    # On a failure in brief mode, point the user at the full detail (§3: an
    # error should tell you how to see more, not dead-end).
    if brief and exit_code != 0:
        print("\nFor full detail, run:  m3 doctor --verbose")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
