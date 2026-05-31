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
from typing import Any, Optional

import httpx
from sqlite_pragmas import apply_pragmas, profile_for_db

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs): pass

M3_CORE_RS_DISABLE = os.environ.get("M3_CORE_RS_DISABLE", "0") == "1"

try:
    if M3_CORE_RS_DISABLE:
        raise ImportError
    from m3_core_rs import format_log
except ImportError:
    def format_log(event: str, *args, **kwargs) -> str:
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

logger = logging.getLogger("M3_SDK")

import hashlib

_LAST_USER_INTERACTION = 0.0

# User-selectable configurations
INITIAL_LIMIT = min(99, max(10, int(os.environ.get("M3_GOVERNOR_INITIAL_THRESHOLD", "85"))))
LIMIT_THRESHOLD = min(100, max(20, int(os.environ.get("M3_GOVERNOR_LIMIT_THRESHOLD", "95"))))

# Enforce sanity constraint: initial < limit
if INITIAL_LIMIT >= LIMIT_THRESHOLD and LIMIT_THRESHOLD != 100:
    INITIAL_LIMIT = LIMIT_THRESHOLD - 5

def register_user_interaction():
    global _LAST_USER_INTERACTION
    _LAST_USER_INTERACTION = time.time()

def get_governor_pacing(telemetry: dict) -> dict:
    """Return pacing delay configurations for background and interactive pipelines."""
    load = max(telemetry.get("cpu_total", 0.0), telemetry.get("ram_total", 0.0), telemetry.get("gpu_total", 0.0))
    elapsed = time.time() - _LAST_USER_INTERACTION

    # 1. Critical Mode (Overall load >= LIMIT_THRESHOLD)
    if LIMIT_THRESHOLD != 100 and load >= LIMIT_THRESHOLD:
        return {"background": "HALTED", "interactive_delay": 30.0} # 30s-60s delay

    # 2. Throttled Mode (Overall load >= INITIAL_LIMIT but < LIMIT_THRESHOLD)
    if load >= INITIAL_LIMIT:
        return {"background": "THROTTLED", "background_delay": 10.0, "interactive_delay": 0.0} # 5s-10s delay

    # 3. Normal Mode
    if elapsed < 30.0:
        return {"background": "HALTED", "interactive_delay": 0.0}
    elif elapsed < 60.0:
        return {"background": "TAPERED", "background_delay": 5.0, "interactive_delay": 0.0}
    return {"background": "CONTINUOUS", "background_delay": 0.1, "interactive_delay": 0.0}

async def pre_execute_interactive_check():
    register_user_interaction()

    ctx = M3Context.for_db()
    telemetry = ctx.get_system_telemetry()
    pacing = get_governor_pacing(telemetry)

    delay = pacing.get("interactive_delay", 0.0)
    if delay > 0.0:
        logger.warning(
            f"Host load critical. Throttling interactive task by {delay}s "
            "to prevent system freeze."
        )
        await asyncio.sleep(delay)

@contextmanager
def migration_lock():
    """Acquires an exclusive atomic file lock for safe startup migrations.

    If the lock is held by another process, it block-waits (with a timeout of 120s)
    until the lock is released.
    """
    lock_path = os.path.join(get_m3_config_root(), ".migration.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)

    fd = None
    start_time = time.time()
    acquired = False

    while time.time() - start_time < 120.0:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            acquired = True
            break
        except FileExistsError:
            time.sleep(0.5)

    if not acquired:
        raise RuntimeError(
            f"Could not acquire migration lock at {lock_path} within 120 seconds. "
            "Another migration process may be hung. If you are sure no other process is migrating, "
            "delete the lock file manually."
        )

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
            try:
                os.unlink(lock_path)
            except Exception:
                pass




def ensure_utf8() -> None:
    """Guarantee the current process runs in Python UTF-8 mode.

    On Windows both stdio AND open() default to the legacy cp1252 code page, so
    any non-cp1252 character (em-dashes, arrows, box-drawing, emoji) crashes with
    UnicodeEncodeError on print or UnicodeDecodeError on a no-encoding open().
    True UTF-8 mode (PEP 540) fixes both, but the interpreter reads it only at
    startup — so we set PYTHONUTF8 and re-exec once with -X utf8.

    Shared canonical implementation: called from every m3 entry process that
    isn't guaranteed to inherit UTF-8 mode — the m3 CLI (m3_memory.cli) and the
    standalone MCP→OpenAI proxy (bin/mcp_proxy.py, the OpenClaw path, launched
    directly as `python bin/mcp_proxy.py` so it never flows through the CLI).

    Safety: no-op if already in UTF-8 mode; an env sentinel bounds the re-exec to
    exactly once so it cannot loop; sys.orig_argv reconstructs the launch
    faithfully (so -m / file-path forms survive).

    KNOWN LIMITATION: inline `python -c "<code>"` launches can mangle on re-exec
    because the OS re-quotes the program string; not a supported m3 entry path.
    Set PYTHONUTF8=1 in the env to bypass (then this short-circuits).
    """
    if sys.flags.utf8_mode:
        return
    if os.environ.get("_M3_UTF8_REEXEC") == "1":
        return
    os.environ["PYTHONUTF8"] = "1"
    os.environ["_M3_UTF8_REEXEC"] = "1"
    orig = list(getattr(sys, "orig_argv", [sys.executable, *sys.argv])) or [
        sys.executable, *sys.argv]
    try:
        os.execv(sys.executable, [orig[0], "-X", "utf8", *orig[1:]])
    except OSError:
        # Re-exec failed (exotic launcher / permissions). Caller's stdio
        # reconfigure (if any) still handles the common print path.
        pass


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


def get_m3_root() -> str:
    """Returns the M3 root directory for user state (config, backups, etc.).
    Honors M3_MEMORY_ROOT env var, defaults to ~/.m3-memory.
    """
    root = os.getenv("M3_MEMORY_ROOT")
    if root:
        return os.path.abspath(os.path.expanduser(root))
    return os.path.join(os.path.expanduser("~"), ".m3-memory")


def get_m3_config_root() -> str:
    """Returns the M3 configuration directory.
    Precedence: M3_CONFIG_ROOT > M3_MEMORY_ROOT/config > ~/.m3/config
    """
    root = os.getenv("M3_CONFIG_ROOT")
    if root:
        return os.path.abspath(os.path.expanduser(root))
    m3_mem_root = os.getenv("M3_MEMORY_ROOT")
    if m3_mem_root:
        return os.path.join(os.path.abspath(os.path.expanduser(m3_mem_root)), "config")
    return os.path.join(os.path.expanduser("~"), ".m3", "config")


def get_m3_engine_root() -> str:
    """Returns the M3 database engine directory.
    Precedence: M3_ENGINE_ROOT > M3_MEMORY_ROOT/engine > ~/.m3/engine
    """
    root = os.getenv("M3_ENGINE_ROOT")
    if root:
        return os.path.abspath(os.path.expanduser(root))
    m3_mem_root = os.getenv("M3_MEMORY_ROOT")
    if m3_mem_root:
        return os.path.join(os.path.abspath(os.path.expanduser(m3_mem_root)), "engine")
    return os.path.join(os.path.expanduser("~"), ".m3", "engine")


def _db_is_populated(path: str) -> bool:
    """True iff `path` is a SQLite file that actually carries the memory schema.

    A bare-existence check is not enough: a connection attempt against a not-yet-
    migrated engine root auto-creates a 0-table `agent_memory.db` stub, and that
    stub would otherwise shadow a populated legacy DB (the M3_MEMORY_ROOT drift —
    a fresh engine/ stub silently winning over memory/agent_memory.db with the
    real data). Returns False for a missing file, an empty stub, or any open/read
    error (treat unreadable as "not usable" so the caller keeps searching).
    """
    if not os.path.exists(path):
        return False
    try:
        conn = sqlite3.connect(path, timeout=2)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_items' LIMIT 1"
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — unreadable/locked DB is not a usable default
        return False


def _default_db_path() -> str:
    # Precedence: explicit M3_ENGINE_ROOT (honored as-is) > a *populated* derived
    # engine root > a populated ~/.m3/engine default > populated sibling memory/
    # (dev clone) > the derived engine path as a last resort (fresh install).
    #
    # The key fix over the naive "any env var set -> engine path" rule: when only
    # M3_MEMORY_ROOT is set, the engine path is DERIVED, not chosen. If that
    # derived DB is missing or an empty stub, we must not let it shadow a
    # populated legacy memory/ DB — see _db_is_populated.
    if os.getenv("M3_ENGINE_ROOT"):
        # Explicit engine root is a deliberate operator choice; honor it verbatim
        # even if empty (a fresh deployment legitimately starts empty here).
        return os.path.join(get_m3_engine_root(), "agent_memory.db")

    engine_db = os.path.join(get_m3_engine_root(), "agent_memory.db")
    if os.getenv("M3_MEMORY_ROOT"):
        if _db_is_populated(engine_db):
            return engine_db
        # Derived engine DB is missing/empty. Prefer a populated legacy memory/
        # DB under the same root before falling back to the empty engine path.
        legacy_under_root = os.path.join(
            os.path.abspath(os.path.expanduser(os.getenv("M3_MEMORY_ROOT"))),
            "memory", "agent_memory.db",
        )
        if _db_is_populated(legacy_under_root):
            logger.warning(
                "M3_MEMORY_ROOT engine DB (%s) is missing or unmigrated; using the "
                "populated legacy store at %s. Run bin/homecoming.py to migrate, or "
                "set M3_ENGINE_ROOT explicitly to silence this.",
                engine_db, legacy_under_root,
            )
            return legacy_under_root
        return engine_db

    # No env override: prefer a populated ~/.m3/engine default, else a populated
    # sibling memory/ (developer clone), else the engine default for a fresh start.
    m3_engine_default = os.path.join(os.path.expanduser("~"), ".m3", "engine", "agent_memory.db")
    if _db_is_populated(m3_engine_default):
        return m3_engine_default

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    legacy_path = os.path.join(base, "memory", "agent_memory.db")
    if _db_is_populated(legacy_path):
        return legacy_path

    return os.path.join(get_m3_engine_root(), "agent_memory.db")


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
            pool_size = int(os.environ.get("DB_POOL_SIZE", "5"))
            pool_timeout = int(os.environ.get("DB_POOL_TIMEOUT", "30"))
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

        # GPU Total Usage (Mock/fallback)
        gpu_total = 0.0

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

    def _record_failure(self, service: str, custom_cooldown: Optional[float] = None):
        state = _CIRCUITS.get(service, {"failures": 0, "open_until": 0})
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
        return format_log(event, *args, **kwargs)

    def log(self, event: str, *args, **kwargs) -> None:
        """Helper to format and print a structured log line to stderr."""
        print(self.format(event, *args, **kwargs), file=sys.stderr)


def _cleanup():
    with _CONTEXTS_LOCK:
        contexts = list(_CONTEXTS.values())
        _CONTEXTS.clear()
    for ctx in contexts:
        _close_context_pool(ctx)

atexit.register(_cleanup)
