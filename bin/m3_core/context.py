import asyncio
import atexit
import hashlib
import os
import queue
import random
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Optional

import httpx
from sqlite_pragmas import apply_pragmas, profile_for_db

from m3_core.gpu import probe_gpu_util
from m3_core.paths import (
    get_m3_config_root,
    get_m3_engine_root,
    get_m3_root,
    getenv_compat,
    resolve_cdw_pg_dsn,
    resolve_db_path,
)
from m3_core.runtime import (
    M3_CORE_RS_DISABLE,
    StructuredLogger,
    load_dotenv,
    logger,
)

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

_CIRCUITS: dict[str, Any] = {}
_CB_THRESHOLD = 3
_CB_COOLDOWN = 60
_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_HTTP_CLIENT_LOOP_ID: Optional[int] = None
_HTTP_CLIENT_LOCK = threading.Lock()


class M3Context:
    def __init__(self, db_path: Optional[str] = None):
        self.m3_config_root = get_m3_config_root()
        self.m3_engine_root = get_m3_engine_root()
        self.m3_memory_root = get_m3_root()  # Keep for legacy compatibility

        # Load dotenv from config root first, fallback to memory root
        dotenv_path = os.path.join(self.m3_config_root, ".env")
        if not os.path.exists(dotenv_path):
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
            pool_size = int(getenv_compat("M3_DB_POOL_SIZE", "DB_POOL_SIZE", "5"))
            pool_timeout = int(getenv_compat("M3_DB_POOL_TIMEOUT", "DB_POOL_TIMEOUT", "30"))
            pool: "queue.Queue[sqlite3.Connection]" = queue.Queue(maxsize=pool_size)
            for _ in range(pool_size):
                try:
                    conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=pool_timeout)
                    conn.row_factory = sqlite3.Row
                    # Centralised pragma stack — profile selected by DB basename.
                    # Gains wal_autocheckpoint + journal_size_limit to bound WAL growth.
                    apply_pragmas(conn, profile_for_db(self.db_path))
                    pool.put(conn)
                except sqlite3.Error as e:
                    logger.error(f"Failed to create SQLite connection for {self.db_path}: {e}")
                    raise
            self._pool = pool
            try:
                self._verify_cohesion()
            except Exception as e:
                # If we fail the cohesion check, let's close the pool and raise
                self._pool = None
                while not pool.empty():
                    try:
                        pool.get_nowait().close()
                    except Exception:
                        pass
                logger.error(f"Cohesion validation failed: {e}")
                raise
            # One-time sanity log per context.
            _probe = pool.queue[0]
            _jm = _probe.execute("PRAGMA journal_mode").fetchone()[0]
            _sy = _probe.execute("PRAGMA synchronous").fetchone()[0]
            logger.info(f"SQLite pool ready: db={self.db_path} journal_mode={_jm} synchronous={_sy} pool_size={pool_size}")

    def get_system_telemetry(self) -> dict:
        """Unify system hardware metrics (CPU, RAM, GPU, Thermal status)."""
        # Try native Rust FFI fast path first
        if not M3_CORE_RS_DISABLE:
            try:
                import m3_core_rs
                if hasattr(m3_core_rs, "get_native_telemetry"):
                    telemetry = m3_core_rs.get_native_telemetry()
                    native_gpu = float(getattr(telemetry, "gpu_total", 0.0))
                    # If the native path reports no GPU (older wheel without GPU
                    # support), fall back to the nvidia-smi probe so the governor
                    # still sees a GPU-pinned local LLM / embed server.
                    gpu_total = native_gpu if native_gpu > 0.0 else probe_gpu_util()
                    return {
                        "cpu_total": float(getattr(telemetry, "cpu_total", 0.0)),
                        "ram_total": float(getattr(telemetry, "ram_total", 0.0)),
                        "gpu_total": gpu_total,
                        "thermal": str(getattr(telemetry, "thermal", "Nominal")),
                    }
            except Exception:
                pass

        try:
            import psutil
        except ImportError:
            return {
                "cpu_total": 0.0,
                "ram_total": 0.0,
                "gpu_total": 0.0,
                "thermal": "Nominal"
            }

        # CPU Total Usage
        try:
            cpu_total = psutil.cpu_percent(interval=None)
        except Exception:
            cpu_total = 0.0

        # RAM Total Usage
        try:
            ram = psutil.virtual_memory()
            ram_total = ram.percent
        except Exception:
            ram_total = 0.0

        # GPU Total Usage — real probe via nvidia-smi (was hardcoded 0.0, which
        # left the governor blind to a GPU-pinned local LLM / embed server).
        gpu_total = probe_gpu_util()

        # Thermal Load
        try:
            from thermal_utils import get_thermal_status
            thermal = get_thermal_status()
        except Exception:
            thermal = "Nominal"

        return {
            "cpu_total": cpu_total,
            "ram_total": ram_total,
            "gpu_total": gpu_total,
            "thermal": thermal
        }

    def _verify_cohesion(self):
        """Verifies the cohesion between the configuration salt and the database.

        Creates the `m3_system_cohesion` metadata table if it does not exist, and
        stores or re-verifies the SHA-256 hash of the active encryption salt.
        """
        try:
            from auth_utils import get_salt_path
        except ImportError:
            return

        salt_path = get_salt_path()
        if not salt_path or not os.path.exists(salt_path):
            return

        try:
            with open(salt_path, "rb") as f:
                salt_bytes = f.read()
            salt_hash = hashlib.sha256(salt_bytes).hexdigest()
        except Exception as e:
            logger.warning(f"Failed to read/hash salt for cohesion check: {e}")
            return

        with self.get_sqlite_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS m3_system_cohesion (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

            row = conn.execute("SELECT value FROM m3_system_cohesion WHERE key = 'salt_hash'").fetchone()
            if row:
                stored_hash = row[0]
                if stored_hash != salt_hash:
                    raise RuntimeError(
                        f"CRITICAL COHESION ERROR: Active configuration salt mismatch with stored database hash!\n"
                        f"Stored Hash: {stored_hash}\n"
                        f"Active Hash: {salt_hash}\n"
                        f"This database was previously encrypted with a different salt. Decoupled path mismatch detected.\n"
                        f"Please reconcile your config and engine folders or env overrides (M3_CONFIG_ROOT / M3_ENGINE_ROOT)."
                    )
            else:
                conn.execute(
                    "INSERT INTO m3_system_cohesion (key, value) VALUES ('salt_hash', ?)",
                    (salt_hash,)
                )
                conn.commit()


    def _check_circuit(self, service: str) -> bool:
        """Checks if the circuit for a specific service is open."""
        if not M3_CORE_RS_DISABLE:
            try:
                import m3_core_rs
                if hasattr(m3_core_rs, "NativeCircuitBreaker"):
                    state = _CIRCUITS.get(service)
                    if not state or not hasattr(state, "check"):
                        state = m3_core_rs.NativeCircuitBreaker(3, 60)
                        _CIRCUITS[service] = state
                    return state.check()
            except Exception:
                pass

        state = _CIRCUITS.get(service)
        if state is None or isinstance(state, dict):
            if not state:
                return True
            if state["open_until"] > time.time():
                logger.error(f"Circuit for {service} is OPEN. Failing fast.")
                return False
            return True
        return True

    def _record_success(self, service: str):
        if not M3_CORE_RS_DISABLE:
            try:
                import m3_core_rs
                if hasattr(m3_core_rs, "NativeCircuitBreaker"):
                    state = _CIRCUITS.get(service)
                    if state and hasattr(state, "record_success"):
                        state.record_success()
                        return
            except Exception:
                pass

        if service in _CIRCUITS:
            del _CIRCUITS[service]

    def _record_failure(self, service: str, custom_cooldown: Optional[float] = None):
        if not M3_CORE_RS_DISABLE:
            try:
                import m3_core_rs
                if hasattr(m3_core_rs, "NativeCircuitBreaker"):
                    state = _CIRCUITS.get(service)
                    if not state or not hasattr(state, "check"):
                        cooldown: float = int(custom_cooldown or _CB_COOLDOWN)
                        state = m3_core_rs.NativeCircuitBreaker(3, cooldown)
                        _CIRCUITS[service] = state
                    if hasattr(state, "record_failure"):
                        state.record_failure()
                        return
            except Exception:
                pass

        state = _CIRCUITS.get(service)
        if state is None or not isinstance(state, dict):
            state = {"failures": 0, "open_until": 0}
        state["failures"] += 1
        cooldown = custom_cooldown or _CB_COOLDOWN
        if state["failures"] >= _CB_THRESHOLD:
            state["open_until"] = time.time() + cooldown
            logger.warning(f"Circuit for {service} OPENED for {cooldown}s.")
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
                    from crypto_provider import provider as crypto
                    ssl_ctx = crypto.get_ssl_context()

                    try:
                        _HTTP_CLIENT = httpx.AsyncClient(timeout=timeout, http2=True, verify=ssl_ctx)
                        logger.debug("Initialized shared httpx.AsyncClient with HTTP/2 and hardened SSL.")
                    except ImportError:
                        _HTTP_CLIENT = httpx.AsyncClient(timeout=timeout, http2=False, verify=ssl_ctx)
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
    def get_sqlite_conn(self) -> Iterator[sqlite3.Connection]:
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
    def get_chatlog_conn(self) -> "Iterator[Any]":
        """Yield a connection for chat log writes/reads.

        Backend-neutral yield type (``Any`` — a duck-typed DB connection, matching
        StorageBackend.connection): a ``sqlite3.Connection`` on SQLite, a psycopg2
        connection on PostgreSQL. NOT annotated ``sqlite3.Connection`` — that
        would be a lie on PG; ``Any`` lets callers use the shared DB-API surface
        (execute/cursor/commit) without a false concrete type.

        On SQLite: the chatlog DB path comes from chatlog_config (CHATLOG_DB_PATH
        > active M3_DATABASE > default agent_chatlog.db). If the resolved chatlog
        path equals this context's main path, reuse the main pool; otherwise a
        dedicated chatlog-tuned pool.

        On PostgreSQL: there is ONE database — chatlog lives in chat_log_* tables
        in the same database/pool as core (one-schema/two-table). The SQLite
        path-equality "unified vs separate" test is meaningless (no file path), so
        we yield the ACTIVE backend connection (same as the core pool). Chatlog
        queries target chat_log_* tables via memory.backends.chatlog_table; the
        table name separates the stores, not the connection.
        """
        # PG: one DB, no file path — route to the active backend pool.
        try:
            from memory.backends import active_backend as _ab

            _backend = _ab()
        except Exception:
            _backend = None
        if _backend is not None and _backend.name != "sqlite":
            with _backend.connection() as conn:
                yield conn
            return

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
        ignored, kept in the signature to match existing callers.
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
                  detail_b: str = "", detail_c: Optional[str] = None) -> None:
        """Route a structured event to the correct legacy table.

        Used by bridges that predate the unified memory_items model.
        Categories: 'thought'/'activity' → activity_logs; 'decision' → project_decisions.
        Unknown categories fall through to activity_logs for safety.
        """
        from audit_trail import log_event
        log_event(self, category, detail_a, detail_b, detail_c)


    @contextmanager
    def pg_connection(self):
        """Returns a psycopg2 connection to the PostgreSQL data warehouse with circuit breaker."""
        import psycopg2
        if not self._check_circuit("postgresql"):
            raise RuntimeError("PostgreSQL circuit breaker is open. Failing fast.")
        # Warehouse role: M3_CDW_PG_URL > PG_URL(deprecated). The vault key stays
        # PG_URL for continuity. Does NOT read M3_PG_URL (that is the primary store).
        url = resolve_cdw_pg_dsn() or self.get_secret("PG_URL")
        if not url:
            raise RuntimeError(
                "Data-warehouse DSN not found. Set M3_CDW_PG_URL (or store PG_URL "
                "in the keychain). NOTE: M3_PG_URL is the PRIMARY-store var and is "
                "no longer read here."
            )
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


def _cleanup():
    with _CONTEXTS_LOCK:
        contexts = list(_CONTEXTS.values())
        _CONTEXTS.clear()
    for ctx in contexts:
        _close_context_pool(ctx)

atexit.register(_cleanup)
