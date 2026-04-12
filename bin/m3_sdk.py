import os
import sys
import sqlite3
import logging
import httpx
import asyncio
import atexit
import queue
import random
import threading
import json
import time
import platform
from datetime import datetime, timezone
from typing import Optional, Any
from contextlib import contextmanager
from auth_utils import get_api_key

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs): pass

logger = logging.getLogger("M3_SDK")

_SQLITE_POOL: Optional[queue.Queue] = None
_POOL_LOCK = threading.Lock()
_CIRCUITS = {}
_CB_THRESHOLD = 3
_CB_COOLDOWN = 60
_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_HTTP_CLIENT_LOOP_ID: Optional[int] = None
_HTTP_CLIENT_LOCK = threading.Lock()

def resolve_venv_python() -> str:
    """Returns the path to the project venv Python executable, cross-platform."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if sys.platform == "win32":
        return os.path.join(base, ".venv", "Scripts", "python.exe")
    return os.path.join(base, ".venv", "bin", "python")

class M3Context:
    def __init__(self, db_path: Optional[str] = None):
        self.m3_memory_root = os.getenv("M3_MEMORY_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dotenv_path = os.path.join(self.m3_memory_root, ".env")
        if os.path.exists(dotenv_path):
            load_dotenv(dotenv_path)

        self.db_path = db_path or self.get_path("memory/agent_memory.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_sqlite_pool()

    def get_path(self, relative_path: str) -> str:
        return os.path.join(self.m3_memory_root, relative_path)

    def get_setting(self, key: str, default: Any = None) -> Any:
        return os.environ.get(key, default)

    def _init_sqlite_pool(self):
        global _SQLITE_POOL
        with _POOL_LOCK:
            if _SQLITE_POOL is None:
                pool_size = int(os.environ.get("DB_POOL_SIZE", "5"))
                pool_timeout = int(os.environ.get("DB_POOL_TIMEOUT", "10"))
                _SQLITE_POOL = queue.Queue(maxsize=pool_size)
                for _ in range(pool_size):
                    try:
                        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=pool_timeout)
                        conn.row_factory = sqlite3.Row
                        conn.execute("PRAGMA journal_mode = WAL")
                        conn.execute("PRAGMA synchronous = NORMAL")
                        conn.execute("PRAGMA foreign_keys = ON")
                        conn.execute("PRAGMA busy_timeout = 10000")
                        _SQLITE_POOL.put(conn)
                    except sqlite3.Error as e:
                        logger.error(f"Failed to create SQLite connection: {e}")
                        raise
                # One-time sanity log — confirm WAL + synchronous settings took effect.
                _probe = _SQLITE_POOL.queue[0]
                _jm = _probe.execute("PRAGMA journal_mode").fetchone()[0]
                _sy = _probe.execute("PRAGMA synchronous").fetchone()[0]
                logger.info(f"SQLite pool ready: journal_mode={_jm} synchronous={_sy} pool_size={pool_size}")
                logger.debug(f"Initialized SQLite connection pool (size={pool_size}, timeout={pool_timeout}s).")

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
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"Request to {url} failed ({exc}). Retrying in {wait:.1f}s...")
                await asyncio.sleep(wait)

    @contextmanager
    def get_sqlite_conn(self) -> sqlite3.Connection:
        global _SQLITE_POOL
        if _SQLITE_POOL is None:
            self._init_sqlite_pool()
        
        conn = _SQLITE_POOL.get(timeout=10)
        try:
            yield conn
        finally:
            _SQLITE_POOL.put(conn)
    
    def get_secret(self, service: str) -> Optional[str]:
        return get_api_key(service)

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

def _cleanup():
    global _SQLITE_POOL
    if _SQLITE_POOL:
        while not _SQLITE_POOL.empty():
            try:
                conn = _SQLITE_POOL.get_nowait()
                conn.close()
            except queue.Empty:
                break
            except Exception as e:
                logger.error(f"Error closing SQLite connection during cleanup: {e}")

atexit.register(_cleanup)
