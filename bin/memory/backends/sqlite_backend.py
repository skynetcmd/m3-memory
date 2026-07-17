"""SQLite `StorageBackend` — delegates to the existing, proven machinery.

Phase 0 deliberately makes this a THIN adapter over `M3Context` and the current
`_db()` connection flow. It introduces no new SQLite behavior: the pool, WAL
pragmas, busy-timeout, transaction discipline, and lazy-init are all exactly
today's code, reached through the same `M3Context`. The point of Phase 0 is to
prove the seam's shape against the working backend with zero behavior change
before any PostgreSQL code exists.

Cycle-break (§2): resolve `M3Context` lazily; do not top-level-import
`memory_core`.
"""
from __future__ import annotations

from contextlib import AbstractContextManager

from dataclasses import dataclass

from .base import BackendName, Capabilities, KeywordHit, VectorHit
from .dialect import Dialect, ParamStyle
from .registry import register_backend


# ── SQLite SQL dialect (co-located with the backend it belongs to) ───────────
# Lives HERE, not in dialect.py, so that adding/altering a backend is one file
# (DESIGN_PHILOSOPHIES §2). dialect.py holds only the base Dialect + validation
# wrappers; the concrete subclass and its frozen singleton are the backend's own.
@dataclass(frozen=True)
class SqliteDialect(Dialect):
    """SQLite SQL surface (separate-file chatlog, qmark binds)."""

    backend: BackendName = "sqlite"
    param_style: ParamStyle = "qmark"

    def insert_or_ignore(self) -> str:
        return "INSERT OR IGNORE INTO"

    def on_conflict_ignore(
        self, *, conflict_target: str = "", index_predicate: str = ""
    ) -> str:
        return ""  # the OR IGNORE prefix already handled it

    def now(self) -> str:
        return "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

    def now_minus_days(self, days_placeholder: str) -> str:
        # `?` binds an int; build the '-N days' modifier string in SQL.
        return f"datetime('now', '-' || {days_placeholder} || ' days')"

    def empty_json_default(self) -> "str | None":
        return ""  # metadata_json is TEXT on SQLite; '' is fine (historical value)

    def returning_id_clause(self) -> str:
        return ""  # id read afterward via last_insert_id (cur.lastrowid)

    def last_insert_id(self, cursor: object) -> object:
        return cursor.lastrowid  # type: ignore[attr-defined]

    def _json_extract_text_expr(self, column: str, json_path: str) -> str:
        return f"json_extract({column}, '$.{json_path}')"

    def _json_extract_int_expr(self, column: str, json_path: str) -> str:
        return f"CAST(json_extract({column}, '$.{json_path}') AS INTEGER)"

    def _temporal_open_clause_expr(self, column: str, op: str, p: str) -> str:
        return f"({column} IS NULL OR {column} = '' OR {column} {op} {p})"

    def coalesce_open_timestamp(self, column: str, fill_placeholder: str) -> str:
        return f"COALESCE(NULLIF({column}, ''), {fill_placeholder})"

    def _table_exists_query(self, table: str) -> "tuple[str, tuple]":
        return (
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        )

    def _columns_of_query(self, table: str) -> "tuple[str, tuple]":
        # pragma_table_info('t') is a table-valued function (SQLite >= 3.16);
        # its `name` column is the column name. Caller reads row[0].
        return (f"SELECT name FROM pragma_table_info('{table}')", ())


# The one shared frozen singleton for SQLite. Obtain via dialect_for / dialect(),
# not by constructing per call site.
SQLITE = SqliteDialect()


@register_backend("sqlite", dialect=SQLITE)
class SqliteBackend:
    """Adapter exposing the current SQLite path through the `StorageBackend` seam."""

    name: BackendName = "sqlite"

    def dialect(self) -> Dialect:
        """The SQLite SQL dialect (qmark placeholders, PRAGMA introspection)."""
        return SQLITE

    def ensure_schema(self) -> None:
        """No-op: SQLite auto-creates its schema on first `_db()` touch via
        ``memory.db._lazy_init``. Present for seam symmetry with PostgreSQL."""
        return

    def schema_version(self) -> "int | None":
        """MAX(version) from schema_versions, or None if the table is absent."""
        from .. import db as _db_mod

        try:
            with _db_mod._db() as conn:
                row = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='schema_versions'"
                ).fetchone()
                if row is None:
                    return None
                vrow = conn.execute(
                    "SELECT MAX(version) FROM schema_versions"
                ).fetchone()
                return int(vrow[0]) if vrow and vrow[0] is not None else None
        except Exception:
            return None

    def capabilities(self) -> Capabilities:
        """Probe optional accelerators; baseline (FTS5 + Rust cosine) always holds.

        The sqlite-vec probe reuses the existing detector so behavior matches the
        current search path exactly. Absence of sqlite-vec is not an error — the
        baseline Rust BLOB cosine is always correct.
        """
        vector_accel = "none"
        try:
            from .. import db as _db_mod

            with _db_mod._db() as conn:
                if self._detect_vector_accelerator(conn):
                    vector_accel = "sqlite_vec"
        except Exception:
            # Any probe failure -> stay on the add-on-free baseline. Never raise
            # from capability discovery; a missing accelerator is normal.
            vector_accel = "none"
        return Capabilities(
            backend="sqlite",
            keyword="fts5",
            vector_accelerator=vector_accel,  # type: ignore[arg-type]
        )

    @staticmethod
    def _detect_vector_accelerator(conn: object) -> bool:
        """True iff sqlite-vec is loadable on ``conn``. Probes the GIVEN connection
        (never opens a new one), so a caller that already holds a conn — e.g.
        ``vector_search`` — checks against the same session without touching global
        pool state. Reuses ``search_routing``'s canonical ``vec_version()`` probe.
        Never raises: any failure means "no accelerator", the always-correct floor.
        """
        try:
            from ..search_routing import _detect_sqlite_vec

            return bool(_detect_sqlite_vec(conn))
        except Exception:
            return False

    def connection(self) -> AbstractContextManager:
        """The pooled SQLite connection context manager used everywhere today.

        Delegates to `memory.db._db()`, so this is byte-for-byte the current
        behavior: same pool, same pragmas, same commit/rollback discipline.
        """
        from .. import db as _db_mod

        return _db_mod._db()

    def open_readonly(self, db_path: str) -> AbstractContextManager:
        """A READ-ONLY connection to a SPECIFIC db file (SQLite-only semantics).

        Some tools (m3_entities/m3_enrich eligible-row scans) read a PARTICULAR
        SQLite file — ``_run_db`` iterates several DBs and tests pass explicit
        paths — via a read-only URI (``file:...?mode=ro``). This honors that
        db_path. On pooled backends there is one store and db_path is meaningless,
        so THEY ignore it and yield a pooled connection (see PostgresBackend); a
        caller uses ``with backend.open_readonly(db_path) as conn:`` and stays
        backend-blind instead of branching on the backend name.
        """
        from contextlib import contextmanager
        import sqlite3

        @contextmanager
        def _ro():
            c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                yield c
            finally:
                c.close()

        return _ro()

    def placeholder(self, n: int = 1) -> str:
        """SQLite qmark placeholders: ``placeholder(3) -> "?, ?, ?"``."""
        if n < 1:
            raise ValueError(f"placeholder count must be >= 1, got {n}")
        return ", ".join(["?"] * n)

    def keyword_search(
        self,
        conn: object,
        query: str,
        *,
        limit: int,
        tenancy_sql: str = "",
        tenancy_params: tuple = (),
        table: str = "memory_items",
    ) -> "list[KeywordHit]":
        """FTS5 keyword search — a faithful extraction of the existing query.

        Compiles the query with the same ``_compile_fts_query`` used everywhere,
        runs the identical ``memory_items_fts MATCH ? ... bm25()`` SELECT, and
        returns ``KeywordHit(id, bm25)`` ordered by bm25 ascending (lower =
        better) — byte-for-byte the behavior of the inline block in search.py.
        An empty/no-token compile yields ``[]``.

        ``table`` is accepted for seam parity but IGNORED on SQLite: the chatlog
        store is a SEPARATE FILE whose tables reuse the core names
        (``memory_items``/``memory_items_fts``), and ``conn`` already points at the
        right file — so the SQL is the same regardless of core-vs-chatlog. (Only
        PostgreSQL, where chatlog is ``chat_log_*`` in the shared database, uses the
        ``table`` argument.)
        """
        del table  # SQLite: same names, right file via conn — parameter unused
        from ..fts import _compile_fts_query

        fts_query, ok = _compile_fts_query(query, "fts5")
        if not ok or not fts_query:
            return []
        rows = conn.execute(  # type: ignore[attr-defined]
            f"""
            SELECT mi.id AS id, bm25(memory_items_fts) AS _bm25
            FROM memory_items_fts fts
            JOIN memory_items mi ON fts.rowid = mi.rowid
            WHERE memory_items_fts MATCH ? AND mi.is_deleted = 0{tenancy_sql}
            ORDER BY _bm25 ASC
            LIMIT ?
            """,
            (fts_query, *tenancy_params, limit),
        ).fetchall()
        # rows may be sqlite3.Row or tuple; index by position to be safe.
        return [KeywordHit(memory_id=r[0], score=float(r[1])) for r in rows]

    def vector_search(
        self,
        conn: object,
        query_vector: list,
        *,
        limit: int,
        dim: int,
        embed_models: tuple = (),
        tenancy_sql: str = "",
        tenancy_params: tuple = (),
    ) -> "list[VectorHit]":
        """Vector search, dispatched by capability to the best available path.

        The result SHAPE is identical regardless of which path runs (base.py
        invariant): an accelerator changes *speed*, never the returned list. Today
        only the add-on-free baseline arm exists; the ``if caps.has(...)`` fork is
        the declared SEAM POINT so a future accelerator (sqlite-vec ANN) is a NEW
        ARM in this file — not a signature change across the seam (§1: the
        universal CPU-only floor never regresses; accelerators are opt-in behind
        the probe).
        """
        if self._detect_vector_accelerator(conn):
            # Placeholder for the sqlite-vec ANN arm (Phase-4 opt-in). It MUST
            # return the same list[VectorHit] shape as the baseline. Until it is
            # implemented, fall through to the always-correct baseline rather than
            # silently degrading — the probe being present doesn't yet mean an ANN
            # index exists. (No behavior change vs today.) Probes the CALLER'S conn,
            # never a fresh global connection.
            pass
        return self._vector_search_baseline(
            conn,
            query_vector,
            limit=limit,
            dim=dim,
            embed_models=embed_models,
            tenancy_sql=tenancy_sql,
            tenancy_params=tenancy_params,
        )

    def _vector_search_baseline(
        self,
        conn: object,
        query_vector: list,
        *,
        limit: int,
        dim: int,
        embed_models: tuple = (),
        tenancy_sql: str = "",
        tenancy_params: tuple = (),
    ) -> "list[VectorHit]":
        """The extension-free arm: fetch BLOB embeddings, score via Rust cosine.

        No sqlite-vec required — identical scoring to the non-vec branch of the
        existing search. Restricts to the compatible embed identity and dim, then
        delegates ranking to the shared scorer so the ordering matches the Postgres
        backend for the same rows. This is the CPU-only floor the conformance test
        asserts; every future accelerator arm is measured against it.
        """
        from ._vector import score_and_rank

        params: list = []
        model_sql = ""
        if embed_models:
            model_sql = " AND me.embed_model IN (%s)" % ", ".join(["?"] * len(embed_models))
            params.extend(embed_models)
        params.extend(tenancy_params)
        rows = conn.execute(  # type: ignore[attr-defined]
            f"""
            SELECT mi.id AS id, me.embedding AS embedding
            FROM memory_items mi
            JOIN memory_embeddings me ON mi.id = me.memory_id
            WHERE mi.is_deleted = 0 AND me.dim = ?{model_sql}{tenancy_sql}
            """,
            (dim, *params),
        ).fetchall()
        return score_and_rank(query_vector, [(r[0], r[1]) for r in rows], dim, limit)
