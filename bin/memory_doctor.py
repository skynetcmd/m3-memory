#!/usr/bin/env python3
"""m3-memory doctor — thin CLI dispatcher over the three doctor phases.

Phases (each in its own module under bin/doctor/):

  - db_repair          legacy DB fixes (timestamps, relationships, JSON)
  - cascade_probe      embedding-cascade health (delegates to memory.doctor)
  - embed_server_probe Rust-side `m3-embed-server doctor` subprocess

Each phase can be skipped via --skip-*. Exit code is the maximum across
the non-skipped phases (most-severe wins).

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
    args = parser.parse_args()

    exit_code = 0

    if not args.skip_repair:
        from doctor import db_repair
        exit_code = max(exit_code, db_repair.run(args.database))

    if not args.skip_cascade:
        from doctor import cascade_probe
        exit_code = max(exit_code, cascade_probe.run())

    if not args.skip_embed_server:
        from doctor import embed_server_probe
        # Rust-side probe doesn't bump exit code on its own — operators
        # legitimately run m3 without `m3 embedder install`, and a missing
        # binary is not a Python-side failure.
        embed_server_probe.run()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
