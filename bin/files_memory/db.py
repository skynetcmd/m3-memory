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
from typing import Iterator

from . import config

# NOTE: schema.SCHEMA_V1/2/3 are no longer applied here — the schema now lives in
# numbered migrations (files_memory/migrations/, run by files_memory.migrate). The
# blobs remain in schema.py as the historical source those migrations were
# generated from (byte-identical); do not re-introduce executescript(SCHEMA_*).

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
    """Resolve the files.db path. Explicit arg > env > config default.

    The env var is read at CALL time (not just the import-time config constant) so
    a caller/test that sets M3_FILES_DB_PATH after import is honored — matching the
    documented precedence. (config.FILES_DB_PATH is frozen at import, so relying on
    it alone silently ignored a later env change — a real footgun in tests that
    point the store at a tmpdir.)"""
    if path:
        return os.path.abspath(path)
    env = os.environ.get("M3_FILES_DB_PATH")
    if env:
        return os.path.abspath(env)
    return config.FILES_DB_PATH


def _is_postgres() -> bool:
    """True when the active m3 backend is NOT sqlite (the files store then lives
    in the `files` schema of the primary PG database, not a separate file)."""
    try:
        from memory.backends import active_backend
        return active_backend().name != "sqlite"
    except Exception:
        return False


def readonly_probe(sql: str, params: tuple = (), *, db_path: str | None = None) -> list:
    """Run a SIDE-EFFECT-FREE read against the files store; return its rows, or []
    if the store (or the queried table) does not exist yet.

    The files-container analogue of a work-gate probe. It must NEVER create or
    migrate the store, so it stays a cheap, silent no-op on installs that don't use
    file ingestion. Backend routing lives HERE (the files store's db layer), not in
    feature code — the caller passes backend-neutral SQL (use ``files_table()`` for
    names) and this method picks the connection:
      - SQLite: a separate DB FILE. If the file is absent there is no store —
        return [] WITHOUT opening it (opening would create + migrate an empty DB).
        Otherwise open and SELECT, tolerating a not-yet-created table.
      - PostgreSQL / other SQL backends: the files tables live in the ``files``
        schema of the primary DB; probe via the primary pool WITHOUT running the
        files-schema migration, tolerating a missing table.
    Any error (no store, locked, unknown backend) → [] (safe no-op)."""
    try:
        if _is_postgres():
            from memory.db import _db as _seam_db  # primary pool; no files-schema init
            try:
                with _seam_db() as conn:
                    return list(conn.execute(sql, params).fetchall())
            except Exception:  # noqa: BLE001 — missing files schema/table → no rows
                return []
        path = _resolve_path(db_path)
        if not path or not os.path.exists(path):
            return []
        conn = sqlite3.connect(path)  # file exists → opens, never creates; SELECT-only
        try:
            return list(conn.execute(sql, params).fetchall())
        except sqlite3.OperationalError:  # table not created yet
            return []
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — resolution/backend error → safe no-op
        return []


def _new_connection(db_path: str) -> sqlite3.Connection:
    """Open a fresh SQLite connection to the separate files DB file.

    SQLite ONLY — on PostgreSQL the files store is a schema in the primary
    database and connections come from that backend's pool (see _db). Kept for the
    SQLite path + integrity_check/rebuild_fts which are SQLite-specific.
    """
    _ensure_parent_dir(db_path)
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    # foreign_keys is OFF by default in SQLite and is a PER-CONNECTION setting —
    # the `PRAGMA foreign_keys = ON` in the schema DDL only applies while the
    # schema is built, not to later connections. Without this, every
    # `ON DELETE CASCADE` in the schema silently no-ops, so corpus_delete(cascade)
    # (and any parent delete) leaves orphaned leaves / facts / ingestion_runs /
    # embeddings behind. Enable it so the schema's declared cascades actually run.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Track PG-side files-schema init separately from SQLite per-path init: on PG there
# is no path, so key on a sentinel.
_PG_INIT_SENTINEL = "<postgres:files-schema>"


def init_db(path: str | None = None) -> None:
    """Initialize the files-store schema if not already done, via numbered
    migrations (files_memory/migrations/), backend-aware.

    SQLite: applies pending up migrations to the separate files DB file.
    PostgreSQL: applies pending pg_ up migrations into the `files` schema of the
    primary database (a single DSN; qualified names, no search_path).
    Idempotent — the migration runner skips versions already in schema_versions.
    """
    is_pg = _is_postgres()
    key = _PG_INIT_SENTINEL if is_pg else _resolve_path(path)
    with _init_lock:
        if key in _initialized_dbs:
            return
        try:
            from . import migrate as _migrate
            if is_pg:
                # Borrow a connection from the primary backend's pool; the files
                # schema is a namespace in the same database, so nothing store-
                # specific to open. The migration runner qualifies every name with
                # `files.` — no search_path is set, so this pooled connection is
                # safe to return and reuse for public-schema work.
                from memory.db import _db as _seam_db
                with _seam_db() as conn:
                    _migrate.run_pending(conn)
            else:
                # SQLite: the runner applies each up file via executescript, which
                # manages its own transaction (and implicitly commits) — so we do
                # NOT wrap it in an outer BEGIN/COMMIT (that would leave no active
                # txn for our COMMIT to close). The connection is autocommit-style
                # (isolation_level=None); each executescript + stamp is durable.
                conn = _new_connection(_resolve_path(path))
                try:
                    _migrate.run_pending(conn)
                finally:
                    conn.close()
            _initialized_dbs.add(key)
            logger.debug("files_memory: schema initialized (%s)", key)
        except Exception:
            _initialized_dbs.discard(key)
            raise


@contextmanager
def _db(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a files-store connection. Auto-initializes the schema.

    SQLite: a fresh sqlite3.Connection to the separate files DB file, wrapped in a
    single BEGIN/COMMIT (rollback on exception).
    PostgreSQL: a connection from the primary backend's pool (single DSN). The
    files tables live in the `files` schema and are addressed by qualified names
    (files_table()), so this pooled connection carries NO session state (no
    search_path) and is safe to hand back for public-schema work.

    `path` is a SQLite file path; it is meaningless on PG and IGNORED there.
    """
    init_db(path)
    if _is_postgres():
        # The seam's _db() already applies commit-on-clean-exit / rollback-on-error
        # and returns the connection to the pool. We just yield through it.
        from memory.db import _db as _seam_db
        with _seam_db() as conn:
            yield conn
        return

    conn = _new_connection(_resolve_path(path))
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
    init_db(path)
    out: dict = {"ok": True}
    # SQLite-only: PRAGMA integrity_check + the FTS5 sync check have no PG analogue
    # (PG has its own integrity guarantees; FTS lands in Phase 2). Report a clean
    # skip on PG rather than run SQLite-specific SQL against the wrong backend.
    if _is_postgres():
        out["skipped"] = "integrity_check is SQLite-only (PG has no PRAGMA/FTS5)"
        return out
    db_path = _resolve_path(path)
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
    """Force-rebuild the FTS5 indexes. Use after schema drift is detected.

    SQLite-only: FTS5 exists only on SQLite. On PG full-text is a GIN tsvector
    (Phase 2) that self-maintains via a generated column, so there is nothing to
    rebuild — this is a clean no-op there."""
    init_db(path)
    if _is_postgres():
        logger.debug("files_memory: rebuild_fts skipped (no FTS5 on PostgreSQL)")
        return
    db_path = _resolve_path(path)
    conn = _new_connection(db_path)
    try:
        conn.execute("INSERT INTO leaves_fts(leaves_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO file_summaries_fts(file_summaries_fts) VALUES('rebuild')")
        logger.info("files_memory: rebuilt FTS5 indexes for %s", db_path)
    finally:
        conn.close()
