import argparse
import asyncio
import atexit
import contextvars
import logging
import os
import queue
import random
import sqlite3
import sys
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs): pass

logger = logging.getLogger("M3_SDK")

# Single source of truth for the local LLM base URL + read timeout. Bridges
# imported this from here in bench-wip; main had been redefining it in each
# bridge. Still overridable via env so dev machines with LM Studio on a
# different port (or a remote Ollama) work without code edits.
LM_STUDIO_BASE = os.environ.get("LM_STUDIO_BASE", "http://localhost:1234/v1")
LM_READ_TIMEOUT = float(os.environ.get("LM_READ_TIMEOUT", "4800.0"))

# ── Per-path context registry ─────────────────────────────────────────────────
# Previously a module-global _SQLITE_POOL was used, with singleton M3Context
# silently reusing the first-initialized pool. Multi-DB support requires a
# pool per resolved DB path, so each M3Context instance owns its own pool and
# instances are cached per absolute path in _CONTEXTS.
#
# LRU-bounded to prevent unbounded growth on long-running MCP servers that see
# many distinct per-call `database` values. Hot paths (default DB, any DB the
# process accesses repeatedly) get refreshed to most-recently-used on every
# lookup, so the cap only ever evicts cold paths. Override via M3_CONTEXT_CACHE_SIZE.
_CONTEXT_CACHE_SIZE = max(2, int(os.environ.get("M3_CONTEXT_CACHE_SIZE", "16")))
_CONTEXTS: "OrderedDict[str, M3Context]" = OrderedDict()
_CONTEXTS_LOCK = threading.Lock()


def _close_context_pool(ctx: "M3Context") -> None:
    """Close every connection in ctx's pool. Safe to call once; idempotent."""
    pool = ctx._pool
    if pool is None:
        return
    ctx._pool = None
    while not pool.empty():
        try:
            conn = pool.get_nowait()
            conn.close()
        except queue.Empty:
            break
        except Exception as e:
            logger.error(f"Error closing SQLite connection: {e}")

_CIRCUITS = {}
_CB_THRESHOLD = 3
_CB_COOLDOWN = 60
_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_HTTP_CLIENT_LOOP_ID: Optional[int] = None
_HTTP_CLIENT_LOCK = threading.Lock()

# ── Active-database ContextVar ────────────────────────────────────────────────
# Consulted by callers that want "whatever DB the surrounding request/CLI
# specified, else the default". The MCP tool dispatcher sets this before each
# tool call; CLI scripts set it once at startup.
_active_db: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "m3_active_db", default=None
)


def resolve_venv_python() -> str:
    """Returns the path to the project venv Python executable, cross-platform."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.platform == "win32":
        return os.path.join(base, ".venv", "Scripts", "python.exe")
    return os.path.join(base, ".venv", "bin", "python")


def _default_db_path() -> str:
    base = os.getenv("M3_MEMORY_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "memory", "agent_memory.db")


def resolve_db_path(explicit: Optional[str] = None) -> str:
    """Resolve an absolute SQLite DB path.

    Order: explicit arg > M3_DATABASE env > active_database ContextVar > default
    (memory/agent_memory.db). Returns an absolute path so pool-cache keys are
    consistent regardless of caller CWD.
    """
    candidate = explicit or os.environ.get("M3_DATABASE") or _active_db.get() or _default_db_path()
    return os.path.abspath(candidate)


@contextmanager
def active_database(path: Optional[str]):
    """Set the active DB path for the duration of a block (ContextVar-scoped).

    Propagates across ``await`` within the same task but does not leak across
    threads — each executor thread gets its own copy unless the caller sets it
    explicitly. Pass ``None`` or "" to defer to env/default resolution.
    """
    resolved = resolve_db_path(path) if path else None
    token = _active_db.set(resolved)
    try:
        yield resolved
    finally:
        _active_db.reset(token)


def add_database_arg(parser: argparse.ArgumentParser) -> None:
    """Attach a standard --database flag to a CLI argparse parser.

    Precedence honored by resolve_db_path(): --database > M3_DATABASE env >
    default (memory/agent_memory.db). Scripts should activate the returned
    path via active_database() or by writing to os.environ['M3_DATABASE']
    before any DB-touching code runs.
    """
    parser.add_argument(
        "--database",
        default=None,
        metavar="PATH",
        help=(
            "SQLite database path. "
            "Env: M3_DATABASE. Default: memory/agent_memory.db."
        ),
    )


class M3Context:
    def __init__(self, db_path: Optional[str] = None):
        self.m3_memory_root = os.getenv("M3_MEMORY_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dotenv_path = os.path.join(self.m3_memory_root, ".env")
        if os.path.exists(dotenv_path):
            load_dotenv(dotenv_path)

        # Preserve constructor contract: no-arg M3Context() resolves against
        # env + default. Callers passing an explicit path bypass the resolver.
        self.db_path = os.path.abspath(db_path or resolve_db_path(None))
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._pool: Optional["queue.Queue[sqlite3.Connection]"] = None
        self._pool_lock = threading.Lock()
        self._init_sqlite_pool()

    @classmethod
    def for_db(cls, db_path: Optional[str] = None) -> "M3Context":
        """Return a cached M3Context for the given path (or default).

        Callers should prefer this over M3Context() so pool reuse works across
        invocations that target the same DB. Constructor remains public for
        legacy callers.

        The cache is LRU-bounded. When full, the least-recently-used context's
        pool is closed before the new one is inserted — in-flight connections
        checked out of that pool stay usable (they were captured by the caller
        via ``with get_sqlite_conn()``), but put-back will raise since the
        pool is torn down. Callers that hold conns across context-cache
        pressure should not; the whole design is request-scoped.
        """
        resolved = resolve_db_path(db_path)
        with _CONTEXTS_LOCK:
            ctx = _CONTEXTS.get(resolved)
            if ctx is not None:
                _CONTEXTS.move_to_end(resolved)
                return ctx
            # Miss — build and insert. Evict LRU if full.
            ctx = cls(resolved)
            _CONTEXTS[resolved] = ctx
            while len(_CONTEXTS) > _CONTEXT_CACHE_SIZE:
                evicted_key, evicted_ctx = _CONTEXTS.popitem(last=False)
                logger.debug(
                    f"M3Context cache evicting {evicted_key} "
                    f"(cache size={len(_CONTEXTS) + 1}, cap={_CONTEXT_CACHE_SIZE})"
                )
                _close_context_pool(evicted_ctx)
            return ctx

    def get_path(self, relative_path: str) -> str:
        return os.path.join(self.m3_memory_root, relative_path)

    def get_setting(self, key: str, default: Any = None) -> Any:
        return os.environ.get(key, default)

    def _init_sqlite_pool(self):
        with self._pool_lock:
            if self._pool is not None:
                return
            pool_size = int(os.environ.get("DB_POOL_SIZE", "5"))
            pool_timeout = int(os.environ.get("DB_POOL_TIMEOUT", "10"))
            pool: "queue.Queue[sqlite3.Connection]" = queue.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                try:
                    conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=pool_timeout)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA journal_mode = WAL")
                    conn.execute("PRAGMA synchronous = NORMAL")
                    conn.execute("PRAGMA foreign_keys = ON")
                    conn.execute("PRAGMA busy_timeout = 10000")
                    conn.execute("PRAGMA cache_size = -64000")   # 64 MB
                    conn.execute("PRAGMA mmap_size = 536870912") # 512 MB memory-mapped I/O
                    conn.execute("PRAGMA temp_store = MEMORY")   # temp tables in RAM
                    pool.put(conn)
                except sqlite3.Error as e:
                    logger.error(f"Failed to create SQLite connection for {self.db_path}: {e}")
                    raise
            self._pool = pool
            # One-time sanity log per context.
            _probe = pool.queue[0]
            _jm = _probe.execute("PRAGMA journal_mode").fetchone()[0]
            _sy = _probe.execute("PRAGMA synchronous").fetchone()[0]
            logger.info(f"SQLite pool ready: db={self.db_path} journal_mode={_jm} synchronous={_sy} pool_size={pool_size}")

    def _check_circuit(self, service: str) -> bool:
        """Checks if the circuit for a specific service is open."""
        state = _CIRCUITS.get(service)
        if not state:
            return True
        if state["open_until"] > time.time():
            logger.error(f"Circuit for {service} is OPEN. Failing fast.")
            return False
        return True

    def _record_success(self, service: str):
        if service in _CIRCUITS:
            del _CIRCUITS[service]

    def _record_failure(self, service: str):
        state = _CIRCUITS.get(service, {"failures": 0, "open_until": 0})
        state["failures"] += 1
        if state["failures"] >= _CB_THRESHOLD:
            state["open_until"] = time.time() + _CB_COOLDOWN
            logger.warning(f"Circuit for {service} OPENED for {_CB_COOLDOWN}s.")
        _CIRCUITS[service] = state

    def get_async_client(self) -> httpx.AsyncClient:
        """Returns a shared httpx.AsyncClient, recreating if the event loop has changed."""
        global _HTTP_CLIENT, _HTTP_CLIENT_LOOP_ID
        try:
            loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            loop_id = None
        if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed or loop_id != _HTTP_CLIENT_LOOP_ID:
            with _HTTP_CLIENT_LOCK:
                # Double-check inside lock to prevent redundant recreation
                if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed or loop_id != _HTTP_CLIENT_LOOP_ID:
                    timeout = httpx.Timeout(connect=5.0, read=4800.0, write=10.0, pool=5.0)
                    try:
                        _HTTP_CLIENT = httpx.AsyncClient(timeout=timeout, http2=True)
                        logger.debug("Initialized shared httpx.AsyncClient with HTTP/2.")
                    except ImportError:
                        _HTTP_CLIENT = httpx.AsyncClient(timeout=timeout, http2=False)
                        logger.info("HTTP/2 support not found (h2 package missing). Falling back to HTTP/1.1.")
                    _HTTP_CLIENT_LOOP_ID = loop_id
        return _HTTP_CLIENT

    async def aclose(self):
        """Closes the shared async client if it exists (H4)."""
        global _HTTP_CLIENT
        if _HTTP_CLIENT and not _HTTP_CLIENT.is_closed:
            await _HTTP_CLIENT.aclose()
            logger.debug("Closed shared httpx.AsyncClient.")

    async def request_with_retry(self, method: str, url: str, retries: int = 3, **kwargs):
        """Resilient HTTP requests with exponential backoff and Circuit Breaker."""
        service = url.split("//")[-1].split("/")[0]

        if not self._check_circuit(service):
            raise httpx.HTTPStatusError(f"Circuit open for {service}", request=None, response=None) # type: ignore

        client = self.get_async_client()
        for attempt in range(retries):
            try:
                resp = await client.request(method, url, **kwargs)
                resp.raise_for_status()
                self._record_success(service)
                return resp
            except (httpx.HTTPStatusError, httpx.NetworkError, httpx.TimeoutException) as exc:
                if attempt == retries - 1:
                    self._record_failure(service)
                    logger.error(f"HTTP Request failed after {retries} attempts: {exc}")
                    raise
                wait = (2 ** attempt) + random.uniform(0, 1)  # nosec B311 - backoff jitter, not cryptographic
                logger.warning(f"Request to {url} failed ({exc}). Retrying in {wait:.1f}s...")
                await asyncio.sleep(wait)

    @contextmanager
    def get_sqlite_conn(self) -> sqlite3.Connection:
        # Capture self._pool inside the with so put-back lands in the correct
        # pool even if another thread somehow swaps the attribute. (It won't
        # today, but cheap insurance for a multi-pool world.)
        if self._pool is None:
            self._init_sqlite_pool()
        pool = self._pool
        conn = pool.get(timeout=10)
        try:
            yield conn
        finally:
            pool.put(conn)

    @contextmanager
    def get_chatlog_conn(self) -> sqlite3.Connection:
        """Yield a SQLite connection for chat log writes/reads.

        Resolution: the chatlog DB path comes from chatlog_config (which now
        honors CHATLOG_DB_PATH > active M3_DATABASE > default agent_chatlog.db).
        If the resolved chatlog path equals this context's main path, we reuse
        the main pool. Otherwise a dedicated chatlog-tuned pool is used.
        """
        try:
            import chatlog_config
        except ImportError:
            with self.get_sqlite_conn() as conn:
                yield conn
            return

        target = chatlog_config.chatlog_db_path()
        if os.path.abspath(target) == os.path.abspath(self.db_path):
            with self.get_sqlite_conn() as conn:
                yield conn
            return

        with chatlog_config.chatlog_sqlite_conn() as conn:
            yield conn

    def get_secret(self, service: str) -> Optional[str]:
        # Lazy import: auth_utils may route through M3Context for vault reads,
        # creating a cycle if imported at module top.
        from auth_utils import get_api_key
        return get_api_key(service)

    def get_logger(self, name: str = "m3") -> "StructuredLogger":
        """Return a StructuredLogger for grep-friendly key=value output.

        Thin convenience accessor; main's StructuredLogger is stateless so
        the returned instance is shareable across calls. The ``name``
        parameter is reserved for a future namespacing pass — currently
        ignored, kept in the signature to match bench-wip callers.
        """
        return StructuredLogger()

    def query_memory(self, sql: str, params: tuple = ()) -> list:
        """Read-only ad-hoc SQL against the active pool.

        Convenience wrapper for bridges that want to run a quick SELECT
        without managing their own context manager. Callers must NOT pass
        mutating SQL here — the wrapper doesn't commit and the connection
        returns to the pool mid-transaction, which silently loses the
        write on the next borrow. Use ``get_sqlite_conn()`` for writes.
        """
        with self.get_sqlite_conn() as conn:
            return conn.execute(sql, params).fetchall()

    def log_event(self, category: str, detail_a: str,
                  detail_b: str = "", detail_c: str = "None") -> None:
        """Route a structured event to the correct legacy table.

        Used by bridges that predate the unified memory_items model.
        Categories: 'thought'/'activity' → activity_logs; 'decision' → project_decisions.
        Unknown categories fall through to activity_logs for safety.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.get_sqlite_conn() as conn:
            cur = conn.cursor()
            if category == "decision":
                cur.execute(
                    "INSERT INTO project_decisions (project, decision, rationale, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    (detail_c or "default", detail_a, detail_b, now),
                )
            else:
                cur.execute(
                    "INSERT INTO activity_logs (timestamp, query, response, model_used) "
                    "VALUES (?, ?, ?, ?)",
                    (now, detail_a, detail_b, detail_c or category),
                )
            conn.commit()

    @contextmanager
    def pg_connection(self):
        """Returns a psycopg2 connection to the PostgreSQL data warehouse with circuit breaker."""
        import psycopg2
        if not self._check_circuit("postgresql"):
            raise RuntimeError("PostgreSQL circuit breaker is open. Failing fast.")
        url = os.getenv("PG_URL") or self.get_secret("PG_URL")
        if not url:
            raise RuntimeError("PG_URL not found in environment or keychain.")
        last_exc = None
        for attempt in range(2):
            try:
                conn = psycopg2.connect(url, connect_timeout=10)
                self._record_success("postgresql")
                try:
                    yield conn
                finally:
                    conn.close()
                return
            except psycopg2.OperationalError as e:
                last_exc = e
                self._record_failure("postgresql")
                if attempt < 1:
                    logger.warning(f"PostgreSQL connect attempt {attempt + 1} failed: {e}. Retrying in 3s...")
                    time.sleep(3)
        raise RuntimeError(f"PostgreSQL connection failed after 2 attempts: {last_exc}")


class StructuredLogger:
    """Renders structured log lines as `event | k=v | k=v` for grep-friendly output."""

    def format(self, event: str, *args, **kwargs) -> str:
        parts = [event]
        for a in args:
            if a is None or a == "":
                continue
            parts.append(str(a))
        for k, v in kwargs.items():
            if v is None:
                continue
            parts.append(f"{k}={v}")
        return " | ".join(parts)


def _cleanup():
    with _CONTEXTS_LOCK:
        contexts = list(_CONTEXTS.values())
        _CONTEXTS.clear()
    for ctx in contexts:
        _close_context_pool(ctx)

atexit.register(_cleanup)
