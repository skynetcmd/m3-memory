#!/usr/bin/env python3
"""Seed a fresh SQLite DB with the full m3-memory schema for test isolation.

Applies every forward migration in ``memory/migrations/`` (skipping the
``.down.sql`` rollback files) so the resulting DB is schema-complete and can
back the live MCP server, the CLI scripts, and the test suites.

Usage:
    python bin/setup_test_db.py --database memory/_test.db
    M3_DATABASE=memory/_test.db python bin/test_memory_bridge.py

Exits non-zero if any migration fails.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))

from m3_sdk import add_database_arg, resolve_db_path  # noqa: E402

MIGRATIONS_DIR = os.path.join(BASE_DIR, "memory", "migrations")

# migrate_memory.py wraps each migration in a SAVEPOINT; executescript opens
# its own implicit transaction instead, so top-level BEGIN / COMMIT confuse
# it. Strip them.
_BEGIN_RE  = re.compile(r"\bBEGIN\s*(TRANSACTION)?\s*;", re.IGNORECASE)
_COMMIT_RE = re.compile(r"\bCOMMIT\s*;", re.IGNORECASE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_database_arg(parser)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Wipe the target DB file before seeding (default: append to existing).",
    )
    args = parser.parse_args()

    db_path = resolve_db_path(args.database)
    if args.force:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except FileNotFoundError:
                pass

    files = sorted(f for f in glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")) if not f.endswith(".down.sql"))
    if not files:
        print(f"No migrations found in {MIGRATIONS_DIR}", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    applied = 0
    failed: list[str] = []
    for f in files:
        sql = open(f, encoding="utf-8").read()
        sql = _BEGIN_RE.sub("", sql)
        sql = _COMMIT_RE.sub("", sql)
        try:
            conn.executescript(sql)
            applied += 1
        except sqlite3.OperationalError as e:
            failed.append(f"{os.path.basename(f)}: {e}")
    conn.commit()
    conn.close()

    print(f"{applied}/{len(files)} migrations applied to {db_path}")
    for msg in failed:
        print(f"  FAIL {msg}", file=sys.stderr)
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
