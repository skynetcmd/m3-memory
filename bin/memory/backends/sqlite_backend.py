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

from .base import BackendName, Capabilities, KeywordHit, VectorHit
from .dialect import SQLITE, Dialect


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
            # Lazy import: search_routing already owns the canonical probe
            # (SELECT vec_version()); reuse it rather than re-implementing.
            from .. import db as _db_mod
            from ..search_routing import _detect_sqlite_vec

            with _db_mod._db() as conn:
                if _detect_sqlite_vec(conn):
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
        """Baseline vector search: fetch BLOB embeddings, score via Rust cosine.

        The extension-free path (no sqlite-vec required) — identical scoring to
        the non-vec branch of the existing search. Restricts to the compatible
        embed identity and dim, then delegates ranking to the shared scorer so
        the ordering matches the Postgres backend for the same rows.
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
