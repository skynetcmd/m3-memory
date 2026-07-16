"""PostgreSQL `StorageBackend` — pooled, baseline (extension-free) implementation.

Phase 1, baseline-only. This backend exists so the same memory core can serve
10-100s of concurrent readers/writers on one live store (the SQLite single-writer
ceiling is the reason PostgreSQL is offered at all — plan §0). It is opt-in;
SQLite remains the default (DESIGN_PHILOSOPHIES §1).

Design contract — identical to the SQLite path so callers don't branch:
  * ``connection()`` is a ``@contextmanager`` yielding a DB-API connection, with
    the SAME commit-on-success / rollback-on-exception / return-to-pool
    discipline as ``memory.db._db()`` (see ``M3Context.get_sqlite_conn``).
  * A real CONNECTION POOL (``psycopg2.pool.ThreadedConnectionPool``) — the
    Phase 1 blocker: single-connect-per-request cannot serve the concurrency
    this backend is for.

Baseline means NO server extensions required (plan §5): vectors are BYTEA +
Rust cosine, keyword is tsvector/GIN — both work on a vanilla, locked-down
federal PostgreSQL. pgvector / pg_search are Phase 4 accelerators behind probes.

Cycle-break (§2): no top-level import of ``memory_core``; resolve lazily.
Fail loud (§3): a missing DSN or driver raises with an actionable message, never
a silent fallback to SQLite.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator

from m3_sdk import getenv_compat

from .base import BackendName, Capabilities, KeywordHit, VectorHit
from .dialect import POSTGRES, Dialect

if TYPE_CHECKING:  # avoid importing the driver at module load
    from psycopg2.pool import ThreadedConnectionPool


# Pool sizing. minconn keeps a few warm; maxconn caps concurrency so a runaway
# fan-out can't exhaust the server's max_connections. Overridable by env for the
# federal 10-100s-user tier. These are conservative dev defaults.
_DEFAULT_MINCONN = 1
_DEFAULT_MAXCONN = 16


class _SqliteCompatConnection:
    """Wrap a psycopg2 connection to present the SQLite-connection surface the
    memory core expects, so ``db.execute(...)`` and ``row["col"]`` work unchanged.

    The core was written against ``sqlite3.Connection`` with ``row_factory=Row``:
      * ``conn.execute(sql, params)`` is a shortcut that creates a cursor, runs
        the statement, and returns the CURSOR (so ``.fetchone()``/``.fetchall()``/
        ``.rowcount`` work on the result). psycopg2 has no connection-level
        ``execute`` — only cursors do.
      * rows are accessed by name (``row["content"]``). psycopg2's default cursor
        returns tuples; a ``RealDictCursor`` returns name-keyed dicts.
    This adapter closes both gaps. It is intentionally minimal — only the surface
    the write/search paths actually use — not a general DB-API façade.
    """

    def __init__(self, raw_conn: object) -> None:
        self._raw = raw_conn

    def execute(self, sql: str, params: "tuple | list" = ()):  # noqa: ANN001
        from psycopg2.extras import RealDictCursor

        cur = self._raw.cursor(cursor_factory=RealDictCursor)  # type: ignore[attr-defined]
        cur.execute(sql, params)
        return cur

    def cursor(self, *args, **kwargs):
        return self._raw.cursor(*args, **kwargs)  # type: ignore[attr-defined]

    def commit(self):
        return self._raw.commit()  # type: ignore[attr-defined]

    def rollback(self):
        return self._raw.rollback()  # type: ignore[attr-defined]

    def __getattr__(self, name):
        # Anything not explicitly adapted falls through to the raw connection.
        return getattr(self._raw, name)


def _resolve_dsn() -> str:
    """Resolve the PostgreSQL DSN, fail loud if absent.

    Precedence mirrors the existing pg tooling (``pg_sync``/``pg_setup``):
    ``M3_PG_URL`` env, then the legacy ``PG_URL`` alias, then the encrypted
    vault. No default — selecting the postgres backend without a DSN is a
    configuration error, not a reason to silently do something else.
    """
    url = (getenv_compat("M3_PG_URL", "PG_URL", "") or "").strip()
    if url:
        return url
    # vault fallback, resolved lazily to avoid a core import at module load
    try:
        from m3_core.context import M3Context

        secret = M3Context().get_secret("PG_URL")
        if secret:
            return secret.strip()
    except Exception:
        pass
    raise RuntimeError(
        "PostgreSQL backend selected (M3_DB_BACKEND=postgres) but no DSN found. "
        "Set M3_PG_URL (or legacy PG_URL) to a postgresql:// URL, or store it in "
        "the encrypted vault as PG_URL. Refusing to fall back to SQLite silently."
    )


class PostgresBackend:
    """Pooled PostgreSQL adapter satisfying the `StorageBackend` seam."""

    name: BackendName = "postgres"

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or _resolve_dsn()
        self._pool: "ThreadedConnectionPool | None" = None
        self._lock = threading.Lock()
        self._caps: Capabilities | None = None
        self._schema_ready = False

    # -- schema init ---------------------------------------------------------
    def ensure_schema(self) -> None:
        """Apply the PG primary schema once, idempotently — the PG analogue of
        SQLite's lazy `_lazy_init`.

        SQLite auto-creates its schema on first `_db()` touch; a PG deployment had
        no equivalent (tables had to be made by hand via psql). This reads
        ``pg_primary_v1.sql`` (all ``CREATE TABLE/INDEX IF NOT EXISTS``, safe to
        re-run) and applies it in one transaction. Runs at most once per backend
        instance (guarded by ``_schema_ready``); the SQL's own IF NOT EXISTS makes
        a concurrent double-apply harmless.

        psycopg2 executes a multi-statement string in a single ``execute`` inside
        a real transaction — so no SQLite ``executescript``/SAVEPOINT dance is
        needed (that machinery stays SQLite-only in migrate_memory.py).
        """
        if self._schema_ready:
            return
        # Acquire the pool BEFORE taking _lock — _ensure_pool takes the same
        # (non-reentrant) lock, so nesting them would deadlock.
        pool = self._ensure_pool()
        with self._lock:
            if self._schema_ready:
                return
            import os

            from memory import config as _cfg

            sql_path = os.path.join(
                _cfg.BASE_DIR, "memory", "migrations", "postgres", "pg_primary_v1.sql"
            )
            if not os.path.exists(sql_path):
                raise RuntimeError(
                    f"PG schema file not found at {sql_path}. Cannot initialize the "
                    f"PostgreSQL primary schema."
                )
            with open(sql_path, encoding="utf-8") as f:
                schema_sql = f.read()
            conn = pool.getconn()
            try:
                cur = conn.cursor()
                cur.execute(schema_sql)
                conn.commit()
                self._schema_ready = True
            except Exception:
                conn.rollback()
                raise
            finally:
                pool.putconn(conn)

    # -- pool lifecycle ------------------------------------------------------
    def _ensure_pool(self) -> "ThreadedConnectionPool":
        if self._pool is not None:
            return self._pool
        with self._lock:
            if self._pool is not None:
                return self._pool
            try:
                from psycopg2.pool import ThreadedConnectionPool
            except ImportError as e:  # fail loud, actionable
                raise RuntimeError(
                    "psycopg2 is required for the PostgreSQL backend. "
                    "Install it: pip install 'psycopg2-binary'."
                ) from e
            minconn = int(getenv_compat("M3_PG_POOL_MIN", "", str(_DEFAULT_MINCONN)) or _DEFAULT_MINCONN)
            maxconn = int(getenv_compat("M3_PG_POOL_MAX", "", str(_DEFAULT_MAXCONN)) or _DEFAULT_MAXCONN)
            if maxconn < minconn:
                raise ValueError(f"M3_PG_POOL_MAX ({maxconn}) < M3_PG_POOL_MIN ({minconn})")
            self._pool = ThreadedConnectionPool(minconn, maxconn, dsn=self._dsn)
            return self._pool

    def close(self) -> None:
        """Close all pooled connections. For clean shutdown / test teardown."""
        with self._lock:
            if self._pool is not None:
                self._pool.closeall()
                self._pool = None

    # -- StorageBackend surface ---------------------------------------------
    def dialect(self) -> Dialect:
        return POSTGRES

    @contextmanager
    def connection(self) -> Iterator["object"]:
        """Yield a pooled psycopg2 connection with SQLite-identical discipline.

        Commit on clean exit, rollback on exception, return to the pool always.
        This mirrors ``M3Context.get_sqlite_conn`` so call sites are backend-blind.
        """
        pool = self._ensure_pool()
        conn = pool.getconn()
        try:
            # Wrap so the memory core's sqlite-style db.execute()/row["col"] works
            # unchanged; commit/rollback operate on the raw connection underneath.
            yield _SqliteCompatConnection(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)

    def placeholder(self, n: int = 1) -> str:
        """Positional binds for psycopg: ``placeholder(3) -> "%s, %s, %s"``."""
        return POSTGRES.placeholder(n)

    def schema_version(self) -> "int | None":
        """MAX(version) from schema_versions, or None if the table is absent."""
        try:
            with self.connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT to_regclass('public.schema_versions')"
                )
                if cur.fetchone()[0] is None:
                    return None
                cur.execute("SELECT MAX(version) FROM schema_versions")
                row = cur.fetchone()
                return int(row[0]) if row and row[0] is not None else None
        except Exception:
            return None

    def keyword_search(
        self,
        conn: object,
        query: str,
        *,
        limit: int,
        tenancy_sql: str = "",
        tenancy_params: tuple = (),
    ) -> "list[KeywordHit]":
        """tsvector keyword search — the Postgres analogue of SQLite FTS5+bm25.

        Compiles the query to a tsquery (same sanitization pipeline as the FTS5
        path), matches the generated ``search_vector`` column with ``@@``, and
        ranks with ``ts_rank``. ts_rank is higher-is-better, but the seam
        contract is LOWER-is-better (bm25 convention), so the score is NEGATED —
        callers sort ascending identically on both backends. Empty compile -> [].

        ``tenancy_sql`` must already be in this backend's ``%s`` placeholder style.
        """
        from ..fts import _compile_tsquery

        tsquery, ok = _compile_tsquery(query, "fts5")
        if not ok or not tsquery:
            return []
        cur = conn.cursor()  # type: ignore[attr-defined]
        # to_tsquery parses the compiled string; bind it as a parameter. Negate
        # ts_rank so lower = more relevant (matches the bm25 seam convention).
        cur.execute(
            f"""
            SELECT mi.id AS id,
                   -ts_rank(mi.search_vector, to_tsquery('english', %s)) AS score
            FROM memory_items mi
            WHERE mi.search_vector @@ to_tsquery('english', %s)
              AND mi.is_deleted = 0{tenancy_sql}
            ORDER BY score ASC
            LIMIT %s
            """,
            (tsquery, tsquery, *tenancy_params, limit),
        )
        return [KeywordHit(memory_id=r[0], score=float(r[1])) for r in cur.fetchall()]

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
        """Baseline vector search: BYTEA embeddings + Rust cosine (no pgvector).

        Identical to the SQLite path except for placeholder style and that BYTEA
        comes back as memoryview/bytes (normalized in the shared scorer). Works
        on vanilla, locked-down federal Postgres — pgvector/HNSW is a Phase-4
        opt-in behind the capability probe, never required here.
        """
        from ._vector import score_and_rank

        params: list = []
        model_sql = ""
        if embed_models:
            model_sql = " AND me.embed_model IN (%s)" % ", ".join(["%s"] * len(embed_models))
            params.extend(embed_models)
        params.extend(tenancy_params)
        cur = conn.cursor()  # type: ignore[attr-defined]
        cur.execute(
            f"""
            SELECT mi.id AS id, me.embedding AS embedding
            FROM memory_items mi
            JOIN memory_embeddings me ON mi.id = me.memory_id
            WHERE mi.is_deleted = 0 AND me.dim = %s{model_sql}{tenancy_sql}
            """,
            (dim, *params),
        )
        rows = [(r[0], r[1]) for r in cur.fetchall()]
        return score_and_rank(query_vector, rows, dim, limit)

    def capabilities(self) -> Capabilities:
        """Probe optional accelerators; baseline (tsvector + Rust cosine) always holds.

        pgvector is detected but NOT required — absence means the BYTEA + Rust
        cosine baseline, which is always correct. Never raises from capability
        discovery (a missing extension is normal on locked-down federal PG).
        """
        if self._caps is not None:
            return self._caps
        vector_accel = "none"
        try:
            with self.connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
                )
                if cur.fetchone() is not None:
                    vector_accel = "pgvector"
        except Exception:
            vector_accel = "none"
        self._caps = Capabilities(
            backend="postgres",
            keyword="tsvector",
            vector_accelerator=vector_accel,  # type: ignore[arg-type]
        )
        return self._caps
