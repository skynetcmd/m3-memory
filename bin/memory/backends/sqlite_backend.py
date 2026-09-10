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

from contextlib import AbstractContextManager, contextmanager
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

    def now_minus_minutes(self, minutes_placeholder: str) -> str:
        return f"datetime('now', '-' || {minutes_placeholder} || ' minutes')"

    def age_days_gt(self, ts_column: str, days_expr: str) -> str:
        return f"(julianday('now') - julianday({ts_column})) > {days_expr}"

    def all_rows_after_offset(self, offset_placeholder: str) -> str:
        return f"LIMIT -1 OFFSET {offset_placeholder}"

    def group_concat(self, expr: str, separator: str = ",") -> str:
        return f"GROUP_CONCAT({expr}, '{separator}')"

    def greatest(self, *exprs: str) -> str:
        return f"MAX({', '.join(exprs)})"

    def least(self, *exprs: str) -> str:
        return f"MIN({', '.join(exprs)})"

    def is_undefined_object_error(self, exc: BaseException) -> bool:
        # SQLite has no SQLSTATE for this; classify by the OperationalError text.
        for e in (exc, getattr(exc, "__cause__", None)):
            if e is None:
                continue
            msg = str(e).lower()
            if ("no such column" in msg or "no such table" in msg
                    or "no column named" in msg):
                return True
        return False

    def is_integrity_error(self, exc: BaseException) -> bool:
        # SQLite raises sqlite3.IntegrityError for unique/PK/NOT NULL/FK conflicts.
        import sqlite3 as _sqlite3
        for e in (exc, getattr(exc, "__cause__", None)):
            if isinstance(e, _sqlite3.IntegrityError):
                return True
        return False

    def day_bucket(self, column: str) -> str:
        return f"substr({column},1,10)"

    def byte_length(self, column: str) -> str:
        # LENGTH(CAST(x AS BLOB)) is the SQLite byte-length idiom and works on
        # every SQLite version; octet_length() needs 3.43+, which is newer than
        # the interpreters m3 supports.
        return f"LENGTH(CAST({column} AS BLOB))"

    def has_content(self, column: str) -> str:
        return f"LENGTH(TRIM(COALESCE({column}, ''))) > 0"

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

    def _json_is_valid_expr(self, column: str) -> str:
        # json_valid() ships with SQLite's JSON1 extension, compiled in by
        # default since 3.38 and present in every interpreter m3 supports.
        # Returns NULL (not 0) for a NULL input — see json_is_valid's docstring.
        return f"json_valid({column})"

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

    def _column_exists_query(self, table: str, column: str) -> "tuple[str, tuple]":
        # pragma_table_info takes the table inline (can't bind); the column name
        # IS bound. Zero rows for a missing table or missing column.
        return (f"SELECT 1 FROM pragma_table_info('{table}') WHERE name = ?", (column,))

    def _glob_fragment(self, column: str, placeholder: str, pattern: str) -> "tuple[str, str]":
        # SQLite has a native case-sensitive GLOB; the pattern passes through.
        return (f"{column} GLOB {placeholder}", pattern)

    def _qualified_table_expr(self, name: str, schema: str) -> str:
        # A separate logical store is a separate DB FILE on SQLite — opened
        # directly — so there is no schema to qualify: the reference is bare.
        # SQLite states this explicitly (the base is abstract) so no dialect is
        # privileged as "the default". `schema` is intentionally unused here.
        return name

    def begin_immediate(self, conn: object) -> None:
        # Take the RESERVED lock now. A deferred BEGIN takes it at the first
        # write, so two read-modify-write passes can both read and then one
        # fails "database is locked" after doing its work.
        conn.execute("BEGIN IMMEDIATE")  # type: ignore[attr-defined]

    def compact_storage(self, *, sqlite_path: "str | None" = None,
                        max_bytes: int = 500 * 1024 * 1024) -> str:
        # VACUUM rewrites the file to reclaim free pages. It needs a fresh
        # connection OUTSIDE any open transaction (VACUUM cannot run inside one)
        # and the *active* path — hence sqlite_path from the caller, not a constant.
        import os
        import sqlite3
        if not sqlite_path:
            return "VACUUM skipped: no active SQLite path supplied"
        try:
            db_size = os.path.getsize(sqlite_path)
        except OSError as e:
            return f"VACUUM skipped: {e}"
        # Size-gated (#46): a multi-hundred-MB VACUUM can hang for minutes.
        if db_size > max_bytes:
            return f"VACUUM skipped: database too large ({db_size / 1e9:.2f} GB)"
        try:
            vconn = sqlite3.connect(sqlite_path)
            try:
                vconn.execute("VACUUM")
            finally:
                vconn.close()
            return "Space reclaimed (VACUUM)"
        except Exception as e:  # noqa: BLE001 — VACUUM is best-effort maintenance
            return f"VACUUM skipped: {e}"


# The one shared frozen singleton for SQLite. Obtain via dialect_for / dialect(),
# not by constructing per call site.
SQLITE = SqliteDialect()


@register_backend("sqlite", dialect=SQLITE)
class SqliteBackend:
    """Adapter exposing the current SQLite path through the `StorageBackend` seam.

    ``SqliteBackend()`` addresses the ACTIVE store — whatever `active_database()`
    / `M3_DATABASE` / the default resolver points at — which is what every
    existing caller wants and what the class has always done.

    ``SqliteBackend(db_path=...)`` addresses ONE SPECIFIC file. Sync needs this:
    it holds a local store and a remote warehouse open at the same time, and the
    local one is not necessarily the process's configured store (the agent_memory
    path sweeps main AND chatlog in one run). Without it, a backend built "for"
    another file would silently read and write the default database — the exact
    class of silent-wrong-store bug this whole effort exists to remove, which is
    why `backend_for()` refused to return one until this existed.

    Binding is by M3Context, not a second pool: `M3Context.for_db(path)` is
    process-registered and already owns a pool per path, so a path-bound backend
    reuses the same connection machinery, pragmas and lazy-init as the default
    path rather than opening a parallel one.
    """

    name: BackendName = "sqlite"

    def __init__(self, db_path: "str | None" = None) -> None:
        self._db_path = db_path

    def _ctx(self):
        """The M3Context for this backend's store (pinned, or the active one)."""
        from m3_sdk import M3Context, resolve_db_path

        return M3Context.for_db(self._db_path or resolve_db_path(None))

    def dialect(self) -> Dialect:
        """The SQLite SQL dialect (qmark placeholders, PRAGMA introspection)."""
        return SQLITE

    def ensure_schema(self) -> None:
        """No-op: SQLite auto-creates its schema on first `_db()` touch via
        ``memory.db._lazy_init``. Present for seam symmetry with PostgreSQL."""
        return

    def schema_version(self) -> "int | None":
        """MAX(version) from schema_versions, or None if the table is absent.

        Reads THIS backend's store: via connection(), so a pinned instance
        reports the pinned file's version rather than the process-active one.
        Answering about a different database than the caller addressed is the
        silent-wrong-store bug the pinning exists to prevent.
        """
        try:
            with self.connection() as conn:
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
            # connection(), not _db(): capabilities are a property of THIS store.
            with self.connection() as conn:
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

    @contextmanager
    def _pinned_connection(self):
        """Read/write connection to the PINNED store, with _db()'s discipline.

        Mirrors `memory.db._db()`'s SQLite arm exactly — lazy-init, pooled
        connection, commit on clean exit, rollback on exception — but against
        this instance's path instead of the process-active one. Kept in lockstep
        with that arm deliberately: if the pooling or commit discipline there
        changes, this must follow, which is why it delegates to the same
        `_lazy_init` and `get_sqlite_conn` rather than reimplementing them.
        """
        from .. import db as _db_mod

        ctx = self._ctx()
        _db_mod._lazy_init(ctx.db_path)
        with ctx.get_sqlite_conn() as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def connection(self) -> AbstractContextManager:
        """The pooled SQLite connection context manager used everywhere today.

        With no pinned path, delegates to `memory.db._db()` — byte-for-byte the
        current behavior: same pool, same pragmas, same commit/rollback
        discipline, and still honouring `active_database()` at call time.

        With a pinned path, opens that store instead. The unpinned branch is left
        untouched rather than routed through the pinned one so the default path
        every existing caller uses keeps its exact semantics, including the
        ContextVar re-resolution that a captured path would freeze.
        """
        from .. import db as _db_mod

        if self._db_path is None:
            return _db_mod._db()
        return self._pinned_connection()

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
        import sqlite3
        from contextlib import contextmanager

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

    def list_tables(self, conn: object) -> "set[str]":
        """User tables from sqlite_master, minus SQLite's own internal names.

        `sqlite_%` covers sqlite_sequence, sqlite_stat*, and the autoindex
        entries. FTS5 shadow tables (``*_fts_data`` etc.) are NOT filtered here:
        they are real tables in this store, and whether they count as drift is
        the caller's decision, not the backend's.
        """
        cur = conn.cursor()  # type: ignore[attr-defined]
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        return {r[0] for r in cur.fetchall()}

    def bulk_upsert(
        self,
        conn: object,
        table: str,
        columns: "list[str]",
        rows: "list[tuple]",
        *,
        conflict_target: str,
        update_columns: "list[str]",
        guard_sql: str = "",
    ) -> int:
        """`executemany` with an explicit placeholder tuple.

        SQLite has no multi-row-expanding helper like psycopg2's execute_values,
        but it does not need one: the engine is in-process, so per-statement
        overhead is microseconds rather than a network round trip.
        """
        if not rows:
            return 0
        placeholders = self.dialect().placeholder(len(columns))
        col_list = ", ".join(columns)
        set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_columns)
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT {conflict_target} DO UPDATE SET {set_clause}"
        )
        if guard_sql:
            sql += f" {guard_sql}"
        cur = conn.cursor()  # type: ignore[attr-defined]
        cur.executemany(sql, rows)
        return len(rows)

    def maintenance_checkpoint(self, conn: object, *, final: bool = False) -> None:
        """WAL checkpoint: PASSIVE mid-batch, TRUNCATE at clean exit (§10).

        Delegates to bin/sqlite_pragmas.py so the PRAGMA text lives in exactly
        one place. Best-effort by contract: a checkpoint is housekeeping, and a
        failure here (a reader holding a read txn blocks TRUNCATE, a closed
        conn) must never abort the batch that called it.
        """
        try:
            import sqlite_pragmas
        except ImportError:  # payload not importable — nothing to do
            return
        try:
            if final:
                sqlite_pragmas.checkpoint_truncate(conn)  # type: ignore[arg-type]
            else:
                sqlite_pragmas.checkpoint_passive(conn)  # type: ignore[arg-type]
        except Exception:
            pass

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

    def keyword_search_with_row_data(
        self,
        conn: object,
        query: str,
        *,
        limit: int,
        tenancy_sql: str = "",
        tenancy_params: tuple = (),
        table: str = "memory_items",
        extra_columns: tuple = (),
        search_mode: str = "fts5",
    ) -> "list[dict]":
        """FTS5 keyword search projecting the row body (see the base contract).

        The SAME MATCH + bm25 query as :meth:`keyword_search`, selecting the row
        columns instead of just the id — this is the query that previously lived
        inline, twice, in memory/search.py. Nothing is fetched twice: the row
        data comes from the one query that already ran, which is why this is
        ``_with_row_data`` and not ``_hydrated``.

        The bm25 column is stripped before returning — it is an internal ranking
        artifact and the row ORDER already carries the ranking. Leaking it would
        also imply bm25 and ts_rank are comparable scales; they are not (see
        :class:`KeywordHit`).
        """
        del table  # SQLite: same names, right file via conn (see keyword_search)
        from ..fts import _compile_fts_query

        fts_query, ok = _compile_fts_query(query, search_mode)
        if not ok or not fts_query:
            return []
        extra_sql = "".join(f", mi.{c}" for c in extra_columns)
        rows = conn.execute(  # type: ignore[attr-defined]
            f"""
            SELECT mi.id, mi.content, mi.title, mi.type, mi.importance{extra_sql},
                   bm25(memory_items_fts) AS _bm25
            FROM memory_items_fts fts
            JOIN memory_items mi ON fts.rowid = mi.rowid
            WHERE memory_items_fts MATCH ? AND mi.is_deleted = 0{tenancy_sql}
            ORDER BY _bm25 ASC
            LIMIT ?
            """,
            (fts_query, *tenancy_params, limit),
        ).fetchall()
        out = []
        for r in rows:
            hit = dict(r)
            hit.pop("_bm25", None)
            out.append(hit)
        return out

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
