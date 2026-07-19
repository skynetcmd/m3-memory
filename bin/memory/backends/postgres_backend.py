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

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator

from m3_sdk import getenv_compat, resolve_primary_pg_dsn

from .base import BackendName, Capabilities, KeywordHit, VectorHit
from .dialect import Dialect, ParamStyle
from .registry import register_backend

if TYPE_CHECKING:  # avoid importing the driver at module load
    from psycopg2.pool import ThreadedConnectionPool


# Pool sizing. minconn keeps a few warm; maxconn caps concurrency so a runaway
# fan-out can't exhaust the server's max_connections. Overridable by env for the
# federal 10-100s-user tier. These are conservative dev defaults.
_DEFAULT_MINCONN = 1
_DEFAULT_MAXCONN = 16


def _make_compat_cursor_factory():
    """A psycopg2 cursor whose rows behave like ``sqlite3.Row``: subscriptable by
    BOTH position (``row[0]``) AND column name (``row["col"]``), and iterable /
    len-able as a tuple.

    The memory core (written against ``sqlite3.Row``) mixes both access styles —
    the write path uses ``row["content"]`` while trust/graph/etc. use ``row[0]``.
    psycopg2's default cursor gives tuples (no name access); ``RealDictCursor``
    gives dicts (no positional access). Neither alone matches ``sqlite3.Row``, so
    a row that fails one style is a latent bug on PG (e.g. ``row[0]`` -> KeyError
    under RealDictCursor). This factory returns rows supporting both.
    """
    import psycopg2.extensions

    class _DualRow(tuple):
        """A result row: a tuple (positional) that also maps column name -> value.

        No ``__slots__`` — a tuple subclass can't have non-empty slots, so
        ``_columns`` lives in the instance ``__dict__``.
        """

        def __new__(cls, values, columns):
            self = super().__new__(cls, values)
            self._columns = columns
            return self

        def __getitem__(self, key):
            if isinstance(key, str):
                return tuple.__getitem__(self, self._columns[key])
            return tuple.__getitem__(self, key)

        def get(self, key, default=None):
            try:
                return self[key]
            except (KeyError, IndexError):
                return default

        def keys(self):
            return list(self._columns.keys())

    class _DualCursor(psycopg2.extensions.cursor):
        """Cursor emitting _DualRow instances (sqlite3.Row-like access)."""

        def _colmap(self):
            if self.description is None:
                return {}
            return {d[0]: i for i, d in enumerate(self.description)}

        def fetchone(self):
            row = super().fetchone()
            return None if row is None else _DualRow(row, self._colmap())

        def fetchall(self):
            cols = self._colmap()
            return [_DualRow(r, cols) for r in super().fetchall()]

        def fetchmany(self, size=None):
            cols = self._colmap()
            rows = super().fetchmany(size) if size is not None else super().fetchmany()
            return [_DualRow(r, cols) for r in rows]

        def __iter__(self):
            cols = self._colmap()
            for r in super().__iter__():
                yield _DualRow(r, cols)

    return _DualCursor


class _SqliteCompatConnection:
    """Wrap a psycopg2 connection to present the SQLite-connection surface the
    memory core expects, so ``db.execute(...)`` and ``row["col"]``/``row[0]`` both
    work unchanged.

    The core was written against ``sqlite3.Connection`` with ``row_factory=Row``:
      * ``conn.execute(sql, params)`` is a shortcut that creates a cursor, runs
        the statement, and returns the CURSOR (so ``.fetchone()``/``.fetchall()``/
        ``.rowcount`` work on the result). psycopg2 has no connection-level
        ``execute`` — only cursors do.
      * rows are accessed by NAME (``row["content"]``) AND by POSITION (``row[0]``)
        across the codebase, exactly as ``sqlite3.Row`` allows. A ``_DualCursor``
        (see ``_make_compat_cursor_factory``) emits rows supporting both.
    This adapter closes those gaps. It is intentionally minimal — only the surface
    the memory paths actually use — not a general DB-API façade.
    """

    def __init__(self, raw_conn: object) -> None:
        self._raw = raw_conn

    def execute(self, sql: str, params: "tuple | list" = ()):  # noqa: ANN001
        cur = self._raw.cursor(  # type: ignore[attr-defined]
            cursor_factory=_make_compat_cursor_factory()
        )
        cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, seq_of_params):  # noqa: ANN001
        """sqlite3.Connection.executemany shortcut, which psycopg2 connections
        lack (executemany is a cursor method there). The memory/chatlog write
        paths call conn.executemany(sql, rows); mirror execute() so they work
        unchanged on PG. psycopg2's cursor.executemany runs the statement per row.
        """
        cur = self._raw.cursor(  # type: ignore[attr-defined]
            cursor_factory=_make_compat_cursor_factory()
        )
        cur.executemany(sql, seq_of_params)
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


def _forbidden_pg_hosts() -> "list[str]":
    """Hosts the PRIMARY store must never connect to (defense in depth).

    The data-warehouse / CDW hub is a shared fan-in mirror; a primary-store DSN
    pointing at it would read/write production warehouse data. Configurable via
    ``M3_PG_FORBIDDEN_HOSTS`` (comma-separated). Empty by default here — the
    deployment supplies its known warehouse host(s); tests supply the dev hub.
    """
    return [h.strip() for h in os.environ.get("M3_PG_FORBIDDEN_HOSTS", "").split(",") if h.strip()]


def _reject_forbidden_host(url: str) -> None:
    """Raise if ``url`` targets a forbidden host (see ``_forbidden_pg_hosts``).

    Matches on the PARSED host (exact, case-insensitive), so a forbidden
    ``198.51.100.5`` does not spuriously reject ``198.51.100.51``, and a forbidden
    host string appearing only inside a password does not trigger. Falls back to
    a raw substring check only when the DSN can't be parsed — better to over-
    refuse an unparseable DSN than to let a forbidden host slip through."""
    forbidden = _forbidden_pg_hosts()
    if not forbidden:
        return
    parsed_host = ""
    try:
        from urllib.parse import urlparse

        parsed_host = (urlparse(url.strip()).hostname or "").lower()
    except Exception:
        parsed_host = ""

    for host in forbidden:
        if not host:
            continue
        hit = (parsed_host == host.lower()) if parsed_host else (host in url)
        if hit:
            raise RuntimeError(
                f"PostgreSQL PRIMARY-store DSN targets a forbidden host {host!r} "
                f"(M3_PG_FORBIDDEN_HOSTS). This host is the data-warehouse/CDW "
                f"mirror, not a primary store — refusing to connect. Point "
                f"M3_PRIMARY_PG_URL at a dedicated primary database."
            )


def _resolve_dsn() -> str:
    """Resolve the PRIMARY-store PostgreSQL DSN, fail loud if absent.

    Role-separated (see m3_core.paths): ``M3_PRIMARY_PG_URL`` > ``M3_PG_URL`` >
    encrypted vault. It NEVER reads ``PG_URL`` or any CDW/warehouse var — the
    warehouse DSN must not reach the primary store. The resolved DSN is then
    checked against ``M3_PG_FORBIDDEN_HOSTS`` (the known warehouse host(s)). No
    default — selecting the postgres backend without a DSN is a configuration
    error, not a reason to silently do something else.
    """
    url = (resolve_primary_pg_dsn("") or "").strip()
    if not url:
        # Vault fallback, resolved lazily to avoid a core import at module load.
        # ONLY the primary-specific vault key — deliberately NOT the legacy PG_URL
        # key, which stores the WAREHOUSE DSN (pg_setup/pg_sync write it there). If
        # we fell back to PG_URL here, a stored warehouse secret would silently
        # become the primary store — the exact vault-level re-coupling the role
        # split exists to prevent. Store the primary DSN as M3_PRIMARY_PG_URL.
        try:
            from m3_core.context import M3Context

            secret = M3Context().get_secret("M3_PRIMARY_PG_URL")
            if secret:
                url = secret.strip()
        except Exception:
            pass
    if not url:
        raise RuntimeError(
            "PostgreSQL backend selected (M3_DB_BACKEND=postgres) but no DSN found. "
            "Set M3_PRIMARY_PG_URL (or M3_PG_URL) to a postgresql:// URL, or store "
            "it in the encrypted vault as M3_PRIMARY_PG_URL. Refusing to fall back "
            "to SQLite silently. NOTE: the primary store does not read PG_URL — "
            "that is the data-warehouse DSN (now M3_CDW_PG_URL)."
        )
    _reject_forbidden_host(url)
    _reject_same_as_warehouse(url)
    return url


def _dsn_identity(url: str) -> "tuple[str, int | None, str] | None":
    """Normalize a DSN to its (host, port, dbname) identity for comparison.

    Two DSNs that name the SAME database differ byte-for-byte in benign ways
    (trailing slash, param order, an explicit vs. implicit default port, or
    different credentials to the same DB). Comparing the parsed identity instead
    of the raw string catches those. Returns None if the URL can't be parsed
    (caller treats an unparseable DSN as "can't prove same" — the forbidden-host
    guard and the rename remain the primary defenses)."""
    try:
        from urllib.parse import urlparse

        p = urlparse(url.strip())
        host = (p.hostname or "").lower()
        port = p.port if p.port is not None else (5432 if p.scheme.startswith("postgres") else None)
        dbname = (p.path or "").lstrip("/").rstrip("/")
        if not host and not dbname:
            return None
        return (host, port, dbname)
    except Exception:
        return None


def _resolve_warehouse_dsn_for_guard() -> str:
    """Resolve the warehouse DSN the same way the warehouse consumers do —
    env (M3_CDW_PG_URL > PG_URL) AND the vault key PG_URL — so the same-DSN guard
    sees a vault-stored warehouse DSN too (the standard 'creds in keyring' setup),
    not just an env one. Best-effort; returns '' on any failure."""
    try:
        from m3_sdk import resolve_cdw_pg_dsn

        env = (resolve_cdw_pg_dsn("") or "").strip()
        if env:
            return env
    except Exception:
        pass
    try:
        from m3_core.context import M3Context

        secret = M3Context().get_secret("PG_URL")
        if secret:
            return secret.strip()
    except Exception:
        pass
    return ""


def _reject_same_as_warehouse(primary_url: str) -> None:
    """Raise if the PRIMARY DSN names the SAME database as the WAREHOUSE DSN.

    The warehouse is a shared fan-in mirror that many instances UPSERT into; the
    primary store is one instance's authoritative DB. If they were the SAME
    database, every peer's pg_sync would overwrite another peer's live primary —
    always a misconfiguration. Compared on the normalized (host, port, dbname)
    identity (see ``_dsn_identity``): same host but a DIFFERENT database (a
    single-node dev box running both) is legitimate and allowed; only the SAME
    database is rejected. The warehouse DSN is resolved from env AND the vault so
    the standard keyring-stored setup is covered, not just env.
    """
    cdw = _resolve_warehouse_dsn_for_guard()
    if not cdw:
        return
    primary_id = _dsn_identity(primary_url)
    cdw_id = _dsn_identity(cdw)
    if primary_id is not None and primary_id == cdw_id:
        raise RuntimeError(
            "The PostgreSQL PRIMARY-store DSN names the SAME database as the "
            "data-warehouse DSN (same host/port/dbname). The warehouse is a shared "
            "pg_sync fan-in mirror; the primary is this instance's authoritative "
            "store — they must be different databases (a different dbname on the "
            "same host is fine). Point M3_PRIMARY_PG_URL and M3_CDW_PG_URL at "
            "distinct databases."
        )


# ── PostgreSQL SQL dialect (co-located with the backend it belongs to) ───────
# Lives HERE, not in dialect.py (DESIGN_PHILOSOPHIES §2: one file per backend).
@dataclass(frozen=True)
class PostgresDialect(Dialect):
    """PostgreSQL SQL surface (one-schema chatlog, %s binds)."""

    backend: BackendName = "postgres"
    param_style: ParamStyle = "format"

    def insert_or_ignore(self) -> str:
        # postgres has no verb form — the arbiter goes in a trailing clause.
        return "INSERT INTO"

    def on_conflict_ignore(
        self, *, conflict_target: str = "", index_predicate: str = ""
    ) -> str:
        if index_predicate and not conflict_target:
            raise ValueError(
                "index_predicate requires a conflict_target (partial-index arbiter)"
            )
        tgt = f" {conflict_target}" if conflict_target else ""
        pred = f" WHERE {index_predicate}" if index_predicate else ""
        return f"ON CONFLICT{tgt}{pred} DO NOTHING"

    def now(self) -> str:
        return "NOW()"

    def now_minus_days(self, days_placeholder: str) -> str:
        # %s binds an int number of days; multiply a 1-day interval.
        return f"NOW() - ({days_placeholder} * INTERVAL '1 day')"

    def now_minus_minutes(self, minutes_placeholder: str) -> str:
        # %s binds an int number of minutes; multiply a 1-minute interval.
        return f"NOW() - ({minutes_placeholder} * INTERVAL '1 minute')"

    def empty_json_default(self) -> "str | None":
        return "{}"  # metadata_json is JSONB; '' is rejected, '{}' is the empty obj

    def returning_id_clause(self) -> str:
        return " RETURNING id"  # no last_insert_rowid on PG; RETURNING is the way

    def last_insert_id(self, cursor: object) -> object:
        return cursor.fetchone()[0]  # type: ignore[attr-defined]

    def _json_extract_text_expr(self, column: str, json_path: str) -> str:
        return f"{column} ->> '{json_path}'"

    def _json_extract_int_expr(self, column: str, json_path: str) -> str:
        return f"({column} ->> '{json_path}')::int"

    def _temporal_open_clause_expr(self, column: str, op: str, p: str) -> str:
        return f"({column} IS NULL OR {column} {op} {p})"

    def coalesce_open_timestamp(self, column: str, fill_placeholder: str) -> str:
        return f"COALESCE({column}, {fill_placeholder})"

    def _table_exists_query(self, table: str) -> "tuple[str, tuple]":
        # to_regclass returns NULL for a non-existent relation; the WHERE keeps the
        # result shape (0 or 1 rows) identical to the sqlite probe.
        return ("SELECT 1 WHERE to_regclass(%s) IS NOT NULL", (table,))

    def _columns_of_query(self, table: str) -> "tuple[str, tuple]":
        sql = (
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position"
        )
        return (sql, (table,))


# The one shared frozen singleton for PostgreSQL.
POSTGRES = PostgresDialect()


@register_backend("postgres", dialect=POSTGRES)
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
                # Apply any PG-native incremental migrations (pg_NNN_*.up.sql,
                # version > the v39 baseline) — the PG analogue of SQLite's
                # migrate_memory runner. Additive schema changes past v39 land here
                # so a PG deployment doesn't drift behind the SQLite one. Each file
                # commits in its own transaction inside the runner.
                try:
                    from migrate_pg import run_pending_pg_migrations

                    applied = run_pending_pg_migrations(conn)
                    if applied:
                        import logging

                        logging.getLogger("memory.backends.postgres").info(
                            "Applied PG incremental migrations: %s", applied
                        )
                except ImportError:
                    # bin/ not importable in this context — baseline still applied.
                    pass
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
    def connection(self) -> Iterator[Any]:
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

    @contextmanager
    def open_readonly(self, db_path: str) -> Iterator[Any]:
        """Read-only-intent connection. On PG there is ONE pooled store, so the
        ``db_path`` argument is meaningless and IGNORED (it names a SQLite file);
        this yields a normal pooled connection. Callers pass db_path for the
        SQLite case; here it's accepted-and-ignored so the call site stays
        backend-blind. Reads only — no writes are performed by the caller."""
        del db_path  # a SQLite file path; not applicable to the pooled PG store
        with self.connection() as conn:
            yield conn

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
        table: str = "memory_items",
    ) -> "list[KeywordHit]":
        """tsvector keyword search — the Postgres analogue of SQLite FTS5+bm25.

        Compiles the query to a tsquery (same sanitization pipeline as the FTS5
        path), matches the generated ``search_vector`` column with ``@@``, and
        ranks with ``ts_rank``. ts_rank is higher-is-better, but the seam
        contract is LOWER-is-better (bm25 convention), so the score is NEGATED —
        callers sort ascending identically on both backends. Empty compile -> [].

        ``tenancy_sql`` must already be in this backend's ``%s`` placeholder style.
        ``table`` is the items table to search — ``memory_items`` for core (the
        default) or ``chat_log_items`` for the chatlog store (both carry the
        generated ``search_vector`` column). It is a trusted internal identifier
        (from ``chatlog_table()``), never end-user input; validated as a bare
        identifier for defense-in-depth since it is interpolated, not bound.
        """
        if not table.isidentifier():
            raise ValueError(f"table must be a bare identifier: {table!r}")
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
            FROM {table} mi
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
        """Vector search, dispatched by capability to the best available path.

        The result SHAPE is identical regardless of arm (base.py invariant). Today
        only the add-on-free baseline exists; the ``if caps.has("pgvector")`` fork
        is the declared SEAM POINT so a future pgvector/HNSW ANN arm is a NEW ARM
        in this file, not a signature change across the seam (§1: CPU-only floor
        never regresses; pgvector is a Phase-4 opt-in behind the probe).
        """
        if self._detect_vector_accelerator(conn):
            # Placeholder for the pgvector ANN arm (Phase-4 opt-in). It MUST return
            # the same list[VectorHit] shape as the baseline. Until implemented,
            # fall through to the always-correct baseline (no behavior change).
            # Probes the CALLER'S conn, never a fresh pooled connection.
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
        """The extension-free arm: BYTEA embeddings + Rust cosine (no pgvector).

        Identical to the SQLite baseline except for placeholder style and that
        BYTEA comes back as memoryview/bytes (normalized in the shared scorer).
        Works on vanilla, locked-down federal Postgres — the CPU-only floor the
        conformance test asserts.
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
                if self._detect_vector_accelerator(conn):
                    vector_accel = "pgvector"
        except Exception:
            vector_accel = "none"
        self._caps = Capabilities(
            backend="postgres",
            keyword="tsvector",
            vector_accelerator=vector_accel,  # type: ignore[arg-type]
        )
        return self._caps

    @staticmethod
    def _detect_vector_accelerator(conn: object) -> bool:
        """True iff pgvector is installed, probed on the GIVEN connection (never
        opens a new pooled connection — so ``vector_search``, which already holds
        ``conn``, doesn't borrow a second one and risk starving a small pool).
        Never raises: any failure means the always-correct BYTEA+Rust floor.
        """
        try:
            cur = conn.cursor()  # type: ignore[attr-defined]
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            return cur.fetchone() is not None
        except Exception:
            return False
