"""Migration runner for the files store — backend-aware, seam-routed.

Applies the numbered migrations in ``files_memory/migrations/`` (SQLite) or
``files_memory/migrations/postgres/`` (PostgreSQL) in version order, tracking
applied versions in a ``schema_versions`` table INSIDE the files store (the SQLite
file, or the ``files`` schema on PG). Mirrors bin/migrate_memory.py /
bin/migrate_pg.py but scoped to the files store and driven through a caller-supplied
connection so it works on whichever backend the caller opened.

Design:
  - The caller (db.init_db) opens the files-store connection and hands it here, so
    this module never decides the backend — it reads it from the active dialect for
    the two places SQL genuinely diverges (schema-qualified table name for PG's
    ``files`` schema; the version-stamp upsert).
  - up-only for auto-init (init_db applies pending ups). down files exist for a
    manual/test revert path (``revert_to``), not the hot init path.
  - Idempotent: a version already in schema_versions is skipped.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger("files_memory.migrate")

# SQLite idempotency errors: DDL that is a no-op because the target already
# exists. SQLite has no `ADD COLUMN IF NOT EXISTS`, so a version re-applied
# against a DB that already carries its columns raises one of these. On PG the
# migrations use `IF NOT EXISTS` and never hit this; this list gives the SQLite
# runner the same "already satisfied" tolerance (see run_pending).
_SQLITE_ALREADY_APPLIED = (
    "duplicate column name",  # ALTER TABLE ... ADD COLUMN (no IF NOT EXISTS in SQLite)
    "already exists",         # CREATE TABLE/INDEX/TRIGGER without IF NOT EXISTS
)


def _is_sqlite_idempotency_error(exc: Exception) -> bool:
    """True iff exc is a SQLite OperationalError reporting that the migration's
    objects already exist — i.e. the version is effectively already applied and
    only its schema_versions stamp is missing (a crash mid-migration, or a test
    that cleared the _initialized_dbs cache without recreating the DB file)."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return any(marker in msg for marker in _SQLITE_ALREADY_APPLIED)

_HERE = Path(__file__).resolve().parent
_SQLITE_DIR = _HERE / "migrations"
_PG_DIR = _HERE / "migrations" / "postgres"

# Version prefix: 001_x.up.sql (SQLite) or pg_001_x.up.sql (PG).
_VERSION_RE = re.compile(r"(?:pg_)?(\d+)_.*\.up\.sql$")


def _is_postgres() -> bool:
    try:
        from memory.backends import active_backend
        return active_backend().name != "sqlite"
    except Exception:
        return False


def _schema_versions_ref() -> str:
    """The files-store schema_versions table, qualified for the active backend."""
    from memory.backends import dialect

    from .config import files_table  # local import: avoids cycle at module load
    del dialect  # files_table already routes through the active dialect
    return files_table("schema_versions")


def _migrations_dir() -> Path:
    return _PG_DIR if _is_postgres() else _SQLITE_DIR


def discover_up_migrations(directory: Path | None = None) -> "list[tuple[int, Path]]":
    """[(version, up_path)] sorted by version. Filesystem glob order is not
    guaranteed, so sort by the numeric prefix."""
    directory = directory or _migrations_dir()
    out: list[tuple[int, Path]] = []
    for fp in glob.glob(str(directory / "*.up.sql")):
        m = _VERSION_RE.search(os.path.basename(fp))
        if m:
            out.append((int(m.group(1)), Path(fp)))
    out.sort(key=lambda t: t[0])
    return out


def _ensure_versions_table(conn) -> None:
    """Create the files-store schema_versions table if absent (both backends).

    On PG the table lives in the `files` schema — which the pg_001 migration
    creates, but the version table must exist BEFORE any migration runs (to record
    them). So ensure the schema first (idempotent CREATE SCHEMA), then the table.
    On SQLite there is no schema namespace and this is a plain CREATE TABLE.
    """
    from .config import FILES_SCHEMA
    if _is_postgres():
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {FILES_SCHEMA}")
    ref = _schema_versions_ref()
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {ref} ("
        f"  version INTEGER PRIMARY KEY,"
        f"  filename TEXT,"
        f"  applied_at TEXT"
        f")"
    )


def _applied_versions(conn) -> "set[int]":
    ref = _schema_versions_ref()
    try:
        rows = conn.execute(f"SELECT version FROM {ref}").fetchall()
    except Exception:
        return set()
    return {int(r[0]) for r in rows}


def run_pending(conn) -> "list[int]":
    """Apply every pending up migration (version not yet in schema_versions) in
    order, on the caller's OPEN connection. Returns versions applied. Each file is
    executed then stamped; the caller owns the surrounding transaction/commit.

    SQLite executes multi-statement scripts via executescript; psycopg2 (through
    the seam's _SqliteCompatConnection) executes a multi-statement string in one
    cursor.execute. Both are handled by conn.execute here because on SQLite the
    files-store connection is a raw sqlite3.Connection (executescript needed) and
    on PG it is the compat wrapper (execute handles multi-statement)."""
    from memory.backends import dialect as _dialect
    _p = _dialect().param()
    _is_pg = _is_postgres()

    _ensure_versions_table(conn)
    done = _applied_versions(conn)
    applied: list[int] = []
    ref = _schema_versions_ref()

    for version, up_path in discover_up_migrations():
        if version in done:
            continue
        sql = up_path.read_text(encoding="utf-8")
        logger.info("files_memory: applying migration v%s (%s)", version, up_path.name)
        # SQLite needs executescript for multi-statement DDL; the PG compat
        # connection runs a multi-statement string via execute.
        if _is_pg:
            conn.execute(sql)
        else:
            try:
                conn.executescript(sql)
            except sqlite3.OperationalError as e:
                # The version isn't stamped in schema_versions, yet its objects
                # already exist (crash mid-migration before the stamp, or a test
                # that cleared the init cache without recreating the DB file).
                # PG tolerates this via `IF NOT EXISTS`; SQLite has no
                # `ADD COLUMN IF NOT EXISTS`, so give the runner the same
                # tolerance: treat the version as already applied and stamp it,
                # LOUDLY — never silently. Any other OperationalError is a real
                # migration failure and re-raises.
                if not _is_sqlite_idempotency_error(e):
                    raise
                logger.warning(
                    "files_memory: migration v%s (%s) objects already present "
                    "(%s) — schema was ahead of the version ledger; stamping "
                    "without re-applying.", version, up_path.name, e,
                )
        conn.execute(
            f"INSERT INTO {ref} (version, filename, applied_at) "
            f"VALUES ({_p}, {_p}, {_dialect().now()})",
            (version, up_path.name),
        )
        applied.append(version)

    if applied:
        logger.info("files_memory: applied migrations %s", applied)
    return applied
