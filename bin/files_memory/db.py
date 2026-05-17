"""Connection helpers + schema lifecycle for files.db.

Distinct from `bin/memory/db.py` because files.db has its own schema,
its own path resolution, and its own initialization lifecycle.

Public API:
    _db(path=None)        — context manager yielding a sqlite3.Connection
    _conn(path=None)      — alias for _db (matches memory.db convention)
    init_db(path=None)    — idempotent schema initialization
    integrity_check(path) — PRAGMA integrity_check; returns ok|errors

Design points:
    - Per-DB-path init tracking via _initialized_dbs (matches memory/db.py).
    - On a fresh DB, the entire SCHEMA_V1 string is applied in one
      executescript() inside a transaction. Idempotent: every statement
      uses IF NOT EXISTS so a re-init is a no-op.
    - Connection cache is per-thread (sqlite3 connections are not
      cross-thread by default).
    - WAL mode is set in the schema script, applied on first init.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config
from .schema import SCHEMA_V1

logger = logging.getLogger("files_memory.db")

# ──────────────────────────────────────────────────────────────────────────────
# Per-path init tracking
# ──────────────────────────────────────────────────────────────────────────────
_init_lock = threading.RLock()
_initialized_dbs: set[str] = set()

# Per-thread connection cache. SQLite connections aren't safe to share across
# threads (without check_same_thread=False, and even then it's a bad idea).
_local = threading.local()


def _ensure_parent_dir(db_path: str) -> None:
    """Create the parent directory for db_path if it doesn't exist.

    The default path is ~/.m3/files_database.db; .m3/ probably doesn't
    exist on a fresh install. Create it lazily on first DB open.
    """
    parent = os.path.dirname(db_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
        logger.info("files_memory: created parent directory %s", parent)


def _resolve_path(path: str | None) -> str:
    """Resolve the files.db path. Explicit arg > env > config default."""
    if path:
        return os.path.abspath(path)
    return config.FILES_DB_PATH


def _new_connection(db_path: str) -> sqlite3.Connection:
    """Open a fresh connection with row_factory + busy timeout."""
    _ensure_parent_dir(db_path)
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # autocommit-style for safety; explicit transactions where needed
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: str | None = None) -> None:
    """Initialize the schema if not already done for this path.

    Idempotent: every CREATE uses IF NOT EXISTS. Safe to call repeatedly.
    Records in schema_migrations track applied versions; future migrations
    will compare config.SCHEMA_VERSION against the max applied version.
    """
    db_path = _resolve_path(path)
    with _init_lock:
        if db_path in _initialized_dbs:
            return
        try:
            conn = _new_connection(db_path)
            try:
                # executescript runs all statements with implicit commits;
                # IF NOT EXISTS makes this safe to repeat.
                conn.executescript(SCHEMA_V1)
                # Migration version check: if a future version exists in the
                # DB but our code is older, log a warning. Inverse (DB old,
                # code new) requires real migrations — phase 1 only has v1.
                cur = conn.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                )
                row = cur.fetchone()
                db_version = (row[0] if row and row[0] is not None else 0)
                if db_version > config.SCHEMA_VERSION:
                    logger.warning(
                        "files.db schema version %s is newer than code expects (%s). "
                        "Some features may not be available.",
                        db_version, config.SCHEMA_VERSION,
                    )
            finally:
                conn.close()
            _initialized_dbs.add(db_path)
            logger.debug("files_memory: schema initialized at %s", db_path)
        except Exception:
            # Don't trap init in a permanently-failed state.
            _initialized_dbs.discard(db_path)
            raise


@contextmanager
def _db(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a sqlite3.Connection to files.db. Auto-initializes schema.

    Each call opens a fresh connection in the current thread. Commits on
    clean exit, rolls back on exception. Use a single _db() block per
    logical transaction.

    We do NOT pool aggressively — sqlite WAL mode handles many concurrent
    readers cheaply, and the ingester is single-writer by design.
    """
    db_path = _resolve_path(path)
    init_db(db_path)
    conn = _new_connection(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


@contextmanager
def _conn(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Alias for _db — matches the memory.db naming convention."""
    with _db(path) as c:
        yield c


def integrity_check(path: str | None = None) -> dict:
    """Run PRAGMA integrity_check + a few invariant queries.

    Returns:
      {
        'ok': bool,
        'sqlite_integrity': 'ok' | error message,
        'fts5_in_sync': bool,
        'orphan_leaves': int,
        'orphan_runs': int,
      }
    """
    db_path = _resolve_path(path)
    init_db(db_path)
    out: dict = {"ok": True}
    conn = _new_connection(db_path)
    try:
        # PRAGMA integrity_check returns 'ok' or a list of issues.
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        result = "; ".join(r[0] for r in rows)
        out["sqlite_integrity"] = result
        if result.strip().lower() != "ok":
            out["ok"] = False

        # FTS5 row count must match base table row count.
        leaf_count = conn.execute("SELECT COUNT(*) FROM leaves").fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM leaves_fts").fetchone()[0]
        out["fts5_in_sync"] = (leaf_count == fts_count)
        if not out["fts5_in_sync"]:
            out["ok"] = False
            out["fts5_drift"] = {"leaves": leaf_count, "fts": fts_count}

        # Orphans: leaves with no file_node, runs with no file_node.
        out["orphan_leaves"] = conn.execute(
            "SELECT COUNT(*) FROM leaves "
            "WHERE file_node NOT IN (SELECT uuid FROM file_nodes)"
        ).fetchone()[0]
        out["orphan_runs"] = conn.execute(
            "SELECT COUNT(*) FROM ingestion_runs "
            "WHERE file_node NOT IN (SELECT uuid FROM file_nodes)"
        ).fetchone()[0]
        if out["orphan_leaves"] or out["orphan_runs"]:
            out["ok"] = False
    finally:
        conn.close()
    return out


def rebuild_fts(path: str | None = None) -> None:
    """Force-rebuild the FTS5 indexes. Use after schema drift is detected."""
    db_path = _resolve_path(path)
    init_db(db_path)
    conn = _new_connection(db_path)
    try:
        conn.execute("INSERT INTO leaves_fts(leaves_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO file_summaries_fts(file_summaries_fts) VALUES('rebuild')")
        logger.info("files_memory: rebuilt FTS5 indexes for %s", db_path)
    finally:
        conn.close()
