#!/usr/bin/env python3
"""PostgreSQL PRIMARY-store migration runner — the PG analogue of migrate_memory.py.

The SQLite primary store evolves via numbered ``memory/migrations/NNN_*.up.sql``
files applied by ``migrate_memory.py``. Those files use SQLite-only DDL
(AUTOINCREMENT, FTS5, rowid) and cannot run on PostgreSQL, so PG got a
hand-translated cumulative baseline (``postgres/pg_primary_v1.sql``, stamped
version 39). This runner continues the sequence from 40 with **PG-native**
incremental files:

    memory/migrations/postgres/pg_NNN_<name>.up.sql     (required)
    memory/migrations/postgres/pg_NNN_<name>.down.sql   (optional)

Contract mirrors migrate_memory.py deliberately (discover → order by NNN → track
applied in ``schema_versions`` → apply/stamp; ``down`` reverts + un-stamps), so
the two runners behave the same way. Differences that are intrinsic to the engine:

  * No SAVEPOINT/executescript dance. psycopg2 runs a multi-statement string in one
    real transaction; a file either commits whole or rolls back whole. Migration
    files therefore MUST NOT contain their own COMMIT/ROLLBACK/BEGIN (same rule as
    the SQLite runner; enforced by ``_validate_migration_sql``).
  * DSN resolution goes through the PRIMARY-store resolver + forbidden-host guard
    (``resolve_primary_pg_dsn`` / ``M3_PG_FORBIDDEN_HOSTS``) so the runner can
    NEVER migrate the data-warehouse hub by accident (the PG_URL-split invariant).

Commands: ``up`` (apply pending), ``status``, ``down --to N`` (revert to N),
``plan`` (print pending SQL). ``ensure_schema()`` on the backend still applies the
v39 baseline; this runner takes it from 40 onward. They compose: run the backend's
``ensure_schema`` once to get the baseline, then ``migrate_pg up`` for increments —
or call :func:`run_pending_pg_migrations` programmatically (what the backend does).
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import sys
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(name)s: [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_pg")

# ── Paths ────────────────────────────────────────────────────────────────────
# bin/migrate_pg.py -> repo root is one dir up from bin/.
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PG_MIGRATIONS_DIR = os.path.join(_BASE_DIR, "memory", "migrations", "postgres")

# The cumulative baseline is version 39 (pg_primary_v1.sql). Incremental files
# continue from 40; anything <= this is owned by the baseline, not this runner.
_BASELINE_VERSION = 39

# PG-native incremental files: pg_NNN_<name>.up.sql / .down.sql (the `pg_` prefix
# distinguishes them from the SQLite NNN_*.sql set living one dir up).
_FNAME_RE = re.compile(r"^pg_(\d+)_(.+?)(?:\.(up|down))?\.sql$")

# Migration files run inside psycopg2's implicit transaction; their own
# transaction-control statements would break atomicity (same rule as the SQLite
# runner). Detected after stripping comments.
_FORBIDDEN_STATEMENTS_RE = re.compile(r"(?im)^\s*(COMMIT|ROLLBACK|BEGIN|START\s+TRANSACTION)\b")


# ── DSN resolution (primary store only, guarded) ─────────────────────────────
def _resolve_dsn(explicit: Optional[str] = None) -> str:
    """Resolve the PRIMARY-store DSN, fail loud if absent.

    ``explicit`` (from --dsn) wins; else the primary resolver
    (M3_PRIMARY_PG_URL > M3_PG_URL). NEVER reads PG_URL / any CDW var, and the
    result is checked against M3_PG_FORBIDDEN_HOSTS — so this runner cannot touch
    the warehouse hub. Reuses the exact guards from the backend."""
    sys.path.insert(0, os.path.join(_BASE_DIR, "bin"))
    from memory.backends.postgres_backend import _reject_forbidden_host
    from m3_sdk import resolve_primary_pg_dsn

    dsn = (explicit or resolve_primary_pg_dsn("") or "").strip()
    if not dsn:
        raise SystemExit(
            "No PostgreSQL PRIMARY DSN. Pass --dsn, or set M3_PRIMARY_PG_URL "
            "(or M3_PG_URL). This runner never reads PG_URL (the warehouse var)."
        )
    _reject_forbidden_host(dsn)  # refuses a warehouse-hub DSN
    return dsn


def _connect(dsn: str):
    try:
        import psycopg2
    except ImportError as e:  # fail loud, actionable
        raise SystemExit(
            "psycopg2 is required for the PG migration runner. "
            "Install it: pip install 'psycopg2-binary'."
        ) from e
    return psycopg2.connect(dsn)


# ── Discovery ────────────────────────────────────────────────────────────────
def discover_migrations(migrations_dir: str = _PG_MIGRATIONS_DIR):
    """Return {version: {'name', 'up': path|None, 'down': path|None}} for every
    pg_NNN_*.sql file. Only versions > the cumulative baseline (39) are runner-
    owned; the baseline file itself (pg_primary_v1.sql) has no NNN prefix and is
    ignored here. Sorted glob so duplicate-prefix resolution is deterministic."""
    out: dict[int, dict[str, Optional[str]]] = {}
    seen: dict[tuple[int, str], str] = {}
    for filepath in sorted(glob.glob(os.path.join(migrations_dir, "pg_*.sql"))):
        fname = os.path.basename(filepath)
        m = _FNAME_RE.match(fname)
        if not m:
            # pg_primary_v1.sql and pg_warehouse_*.sql intentionally don't match.
            continue
        version = int(m.group(1))
        if version <= _BASELINE_VERSION:
            logger.warning(
                f"Ignoring {fname}: version {version} <= baseline "
                f"{_BASELINE_VERSION} (owned by pg_primary_v1.sql)."
            )
            continue
        name = m.group(2)
        direction = m.group(3) or "up"
        key = (version, direction)
        if key in seen:
            logger.warning(
                f"Duplicate pg migration for v{version} {direction}: "
                f"{os.path.basename(seen[key])} vs {fname} — using {fname}."
            )
        seen[key] = filepath
        entry = out.setdefault(version, {"name": name, "up": None, "down": None})
        if direction == "down":
            entry["down"] = filepath
        else:
            entry["up"] = filepath
            entry["name"] = name
    return out


# ── Version tracking ─────────────────────────────────────────────────────────
def get_applied_versions(conn) -> "list[int]":
    """Integer version markers from schema_versions (skips non-numeric bench
    labels), or [] if the table is absent (baseline not yet applied)."""
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('public.schema_versions')")
    if cur.fetchone()[0] is None:
        return []
    cur.execute("SELECT version FROM schema_versions ORDER BY version")
    out: list[int] = []
    for (v,) in cur.fetchall():
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def current_version(conn) -> int:
    applied = get_applied_versions(conn)
    return max(applied) if applied else 0


# ── Validation / apply / revert ──────────────────────────────────────────────
def _validate_migration_sql(filepath: str, sql: str) -> None:
    """Reject files that issue their own COMMIT/ROLLBACK/BEGIN — those break the
    single-transaction-per-file atomicity. Comments stripped first so prose using
    those words doesn't trip it."""
    stripped = re.sub(r"--[^\n]*", "", sql)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    m = _FORBIDDEN_STATEMENTS_RE.search(stripped)
    if m:
        raise ValueError(
            f"Migration {os.path.basename(filepath)} contains a top-level "
            f"{m.group(1).upper()} statement, which conflicts with the "
            f"one-transaction-per-file model of migrate_pg.py. Remove it — each "
            f"file runs inside an implicit transaction."
        )


def apply_migration(conn, version: int, up_path: str) -> None:
    """Apply one up file atomically and stamp schema_versions. All-or-nothing:
    psycopg2 runs the whole script in one transaction; on error we roll back and
    the version is NOT stamped."""
    logger.info(f"Applying pg migration {version}: {os.path.basename(up_path)}")
    with open(up_path, encoding="utf-8") as f:
        sql = f.read()
    _validate_migration_sql(up_path, sql)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        cur.execute(
            "INSERT INTO schema_versions (version, filename) VALUES (%s, %s) "
            "ON CONFLICT (version) DO NOTHING",
            (version, os.path.basename(up_path)),
        )
        conn.commit()
        logger.info(f"  -> applied v{version}")
    except Exception:
        conn.rollback()
        logger.error(f"Failed to apply v{version}; rolled back.")
        raise


def revert_migration(conn, version: int, down_path: str) -> None:
    """Run a down file and un-stamp the version, atomically."""
    logger.info(f"Reverting pg migration {version}: {os.path.basename(down_path)}")
    with open(down_path, encoding="utf-8") as f:
        sql = f.read()
    _validate_migration_sql(down_path, sql)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        cur.execute("DELETE FROM schema_versions WHERE version = %s", (version,))
        conn.commit()
        logger.info(f"  -> reverted v{version}")
    except Exception:
        conn.rollback()
        logger.error(f"Failed to revert v{version}; rolled back.")
        raise


# ── Programmatic entry (used by the backend's ensure_schema) ─────────────────
def run_pending_pg_migrations(conn, migrations_dir: str = _PG_MIGRATIONS_DIR) -> "list[int]":
    """Apply every pending pg_NNN migration (> current applied version) in order,
    on an already-open connection. Returns the list of versions applied (possibly
    empty). Raises on the first failure (that file rolled back; earlier ones stay
    committed). Idempotent — a second call with nothing pending returns []."""
    migs = discover_migrations(migrations_dir)
    if not migs:
        return []
    applied = set(get_applied_versions(conn))
    pending = [v for v in sorted(migs) if v not in applied]
    done: list[int] = []
    for v in pending:
        up = migs[v]["up"]
        if not up:
            logger.error(f"pg migration v{v} has no up file — stopping.")
            break
        apply_migration(conn, v, up)
        done.append(v)
    return done


# ── CLI subcommands ──────────────────────────────────────────────────────────
def cmd_status(args) -> None:
    migs = discover_migrations()
    conn = _connect(_resolve_dsn(args.dsn))
    try:
        applied = set(get_applied_versions(conn))
        cur = current_version(conn)
    finally:
        conn.close()
    print(f"Baseline (pg_primary_v1.sql): v{_BASELINE_VERSION}")
    print(f"Current applied version:      v{cur}")
    if not migs:
        print("Incremental pg migrations:    (none on disk)")
        return
    print("Incremental pg migrations:")
    for v in sorted(migs):
        mark = "[x]" if v in applied else "[ ]"
        down = "down" if migs[v]["down"] else "no-down"
        print(f"  {mark} v{v}  {migs[v]['name']}  ({down})")


def cmd_plan(args) -> None:
    migs = discover_migrations()
    conn = _connect(_resolve_dsn(args.dsn))
    try:
        applied = set(get_applied_versions(conn))
    finally:
        conn.close()
    pending = [v for v in sorted(migs) if v not in applied]
    if not pending:
        print("No pending pg migrations.")
        return
    print(f"--- pending pg migrations ({len(pending)}) ---")
    for v in pending:
        up = migs[v]["up"]
        print(f"\n# v{v} ({migs[v]['name']}): {os.path.basename(up) if up else '<no up>'}")
        if up:
            with open(up, encoding="utf-8") as f:
                print(f.read().rstrip())
    print("\n--- end of plan ---")


def cmd_up(args) -> None:
    conn = _connect(_resolve_dsn(args.dsn))
    try:
        migs = discover_migrations()
        applied = set(get_applied_versions(conn))
        cur = current_version(conn)
        all_versions = sorted(migs)
        target_ver = args.to if args.to is not None else (max(all_versions) if all_versions else cur)
        pending = [v for v in all_versions if v not in applied and v <= target_ver]
        if not pending:
            logger.info("Database is up to date. No pending pg migrations.")
            return
        print(f"\nCurrent version: v{cur}")
        print(f"Target version:  v{target_ver}")
        print("Will apply:")
        for v in pending:
            down = "has down" if migs[v]["down"] else "NO down (irreversible)"
            print(f"  + v{v}  {migs[v]['name']}  [{down}]")
        if args.dry_run:
            logger.info("Dry run — no changes made.")
            return
        if not args.yes:
            resp = input(f"\nApply {len(pending)} pg migration(s) to the PRIMARY store? [y/N] ")
            if resp.strip().lower() not in ("y", "yes"):
                logger.info("Aborted by user.")
                return
        for v in pending:
            up = migs[v]["up"]
            if not up:
                logger.error(f"v{v} has no up file — skipping")
                continue
            apply_migration(conn, v, up)
        logger.info(f"Done. Now at v{current_version(conn)}.")
    finally:
        conn.close()


def cmd_down(args) -> None:
    if args.to is None:
        raise SystemExit("down requires --to N (the version to roll back TO)")
    conn = _connect(_resolve_dsn(args.dsn))
    try:
        migs = discover_migrations()
        applied = sorted(get_applied_versions(conn))
        # Revert versions strictly greater than the target, newest first.
        to_revert = [v for v in reversed(applied) if v > args.to and v > _BASELINE_VERSION]
        if not to_revert:
            logger.info(f"Nothing to revert above v{args.to}.")
            return
        missing = [v for v in to_revert if not (migs.get(v) or {}).get("down")]
        if missing:
            raise SystemExit(
                f"Cannot roll back: no down file for version(s) {missing}. "
                f"Add pg_NNN_*.down.sql or choose a higher --to."
            )
        print("Will revert (newest first):")
        for v in to_revert:
            print(f"  - v{v}  {migs[v]['name']}")
        if not args.yes:
            resp = input(f"\nRevert {len(to_revert)} pg migration(s)? [y/N] ")
            if resp.strip().lower() not in ("y", "yes"):
                logger.info("Aborted by user.")
                return
        for v in to_revert:
            revert_migration(conn, v, migs[v]["down"])
        logger.info(f"Done. Now at v{current_version(conn)}.")
    finally:
        conn.close()


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description="PostgreSQL PRIMARY-store migration runner.")
    p.add_argument("--dsn", default=None, help="Explicit DSN (else M3_PRIMARY_PG_URL/M3_PG_URL).")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_up = sub.add_parser("up", help="Apply pending pg migrations.")
    p_up.add_argument("--to", type=int, default=None, help="Stop at this version.")
    p_up.add_argument("--yes", action="store_true", help="Skip confirmation.")
    p_up.add_argument("--dry-run", action="store_true", help="Show plan, apply nothing.")
    p_up.set_defaults(func=cmd_up)

    p_st = sub.add_parser("status", help="Show applied vs pending pg migrations.")
    p_st.set_defaults(func=cmd_status)

    p_pl = sub.add_parser("plan", help="Print the SQL of pending pg migrations.")
    p_pl.set_defaults(func=cmd_plan)

    p_dn = sub.add_parser("down", help="Revert pg migrations down to --to N.")
    p_dn.add_argument("--to", type=int, default=None, help="Version to roll back TO (required).")
    p_dn.add_argument("--yes", action="store_true", help="Skip confirmation.")
    p_dn.set_defaults(func=cmd_down)

    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
