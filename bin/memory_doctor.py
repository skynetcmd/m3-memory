#!/usr/bin/env python3
"""m3-memory doctor — thin CLI dispatcher over the doctor phases.

Phases (each in its own module under bin/doctor/):

  - db_repair          legacy DB fixes (timestamps, relationships, JSON)
  - cascade_probe      embedding-cascade health (delegates to memory.doctor)
  - embed_server_probe Rust-side `m3-embed-server doctor` subprocess
  - oxidation_probe    m3_core_rs native-extension presence/staleness report
  - embed_space_probe  mixed embed-space check (vectors from >1 model in one index)

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
        "--skip-locks", action="store_true",
        help="Skip the single-instance lock health check.",
    )
    parser.add_argument(
        "--skip-embed-space", action="store_true",
        help="Skip the mixed embed-space check (vectors from >1 model in one index).",
    )
    parser.add_argument(
        "--skip-schedule", action="store_true",
        help="Skip the dangling scheduled-task interpreter check.",
    )
    parser.add_argument(
        "--skip-shared-embedder", action="store_true",
        help="Skip the shared-embedder-mode check (config + server + keep-alive task).",
    )
    parser.add_argument(
        "--skip-plugin", action="store_true",
        help="Skip the Claude Code plugin version/enabled check.",
    )
    parser.add_argument(
        "--skip-agent-paths", action="store_true",
        help="Skip the cross-agent dead-path check (Gemini/OpenCode/Hermes/...).",
    )
    parser.add_argument(
        "--skip-dashboard", action="store_true",
        help="Skip the web-dashboard liveness check (registry + port probe).",
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

        # Shared-embedder repair lives in its own probe (it writes config, starts
        # the server, and registers the keep-alive task — actions outside the DB-
        # focused memory_doctor_fix_impl). Run it with fix=True so `m3 doctor --fix`
        # heals shared mode too. Dry-run only reports (no side effects).
        shared_rc = 0
        if not args.skip_shared_embedder:
            from doctor import shared_embedder_probe
            shared_rc = shared_embedder_probe.run(brief=False, fix=not args.dry_run)

        # Dashboard self-heal: kill a wedged instance, reap its stale registry
        # entry, and restart it on its recorded host/port. Dry-run only reports.
        if not args.skip_dashboard:
            from doctor import dashboard_probe
            dashboard_probe.run(brief=False, fix=not args.dry_run)

        if res["summary"] == "failed" or shared_rc != 0:
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

    if not getattr(args, "skip_locks", False):
        from doctor import lock_probe
        # Report-only: a degraded lock (running without single-instance
        # enforcement) or a flapping one (duplicate launchers) is a warning, not a
        # hard failure — the services still run (fail-safe). Surfaces the state
        # from the lock event log that is otherwise invisible.
        lock_probe.run(brief=brief)

    if not getattr(args, "skip_embed_space", False):
        from doctor import embed_space_probe
        # Report-only: a store mixing two embedding models still "works" — cosine
        # just returns nonsense for the minority rows, silently. Re-embedding is a
        # deliberate one-time cost the operator chooses, so this warns and never
        # bumps the exit code.
        embed_space_probe.run(brief=brief)

    if not args.skip_schedule:
        from doctor import schedule_probe
        # Report-only: a task pointing at a deleted interpreter is a recoverable
        # state (re-register via `m3 setup`), so this never bumps the exit code —
        # it nags and prints the fix when a registered AgentOS_* task is dangling.
        schedule_probe.run(brief=brief)

    if not args.skip_shared_embedder:
        from doctor import shared_embedder_probe
        # DOES bump the exit code: shared mode is the shipped default, so a missing
        # config / dead server / unregistered keep-alive task is a real degraded
        # state (silent fleet-wide embedding outage), not a supported variant.
        # `m3 doctor --fix` repairs it (see the --fix branch above).
        exit_code = max(exit_code, shared_embedder_probe.run(brief=brief, fix=False))

    if not args.skip_plugin:
        from doctor import plugin_version_probe
        # Report-only: a stale or disabled Claude Code plugin is user-recoverable
        # (the fix is client-side /plugin + /reload-plugins commands doctor can't
        # invoke), so it nags with the exact commands but never bumps the exit code.
        plugin_version_probe.run(brief=brief)

    if not args.skip_agent_paths:
        from doctor import agent_paths_probe
        # DOES bump the exit code: a dead-path agent config means m3 silently does
        # not load in that host (Gemini/OpenCode/Hermes/... after a relocation).
        # `m3 setup` self-heals it (installer owns the canonical write path).
        exit_code = max(exit_code, agent_paths_probe.run(brief=brief))

    if not args.skip_dashboard:
        from doctor import dashboard_probe
        # Report-only and exit-code-neutral: a stopped dashboard is a supported
        # state (the user simply hasn't launched it), not a degraded fleet. It
        # prints the URL when healthy, and on a wedged/dead entry nags with
        # `m3 doctor --fix`. The kill/restart happens only in the --fix branch.
        dashboard_probe.run(brief=brief, fix=False)

    # On a failure in brief mode, point the user at the full detail (§3: an
    # error should tell you how to see more, not dead-end).
    if brief and exit_code != 0:
        print("\nFor full detail, run:  m3 doctor --verbose")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
