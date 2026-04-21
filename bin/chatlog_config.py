"""
chatlog_config.py — configuration resolver for the chat log subsystem.

The three-mode (integrated/separate/hybrid) system has been collapsed: there
is now only a single chatlog DB path. If it happens to equal the main memory
DB path, chat log rows live in the main store (equivalent to the old
"integrated" behavior). Otherwise they live in a dedicated file (equivalent
to "separate"), and promote operations ATTACH the main DB and copy rows
across (what used to be called "hybrid" is just copy=True, which is the
default).

Resolution order for the chatlog DB path:
    1. CHATLOG_DB_PATH env var (explicit chatlog override, highest priority)
    2. M3_DATABASE env var (unified main DB — chatlog shares it)
    3. .chatlog_config.json db_path field
    4. Default: memory/agent_chatlog.db (separate file; historical default)

Consumers:
    bin/chatlog_core.py       - write queue, search, promote, cost report
    bin/chatlog_status.py     - observability summary
    bin/chatlog_init.py       - interactive setup
    bin/chatlog_ingest.py     - stdin → bulk write
    bin/migrate_memory.py     - multi-target migration runner
    bin/m3_sdk.py             - get_chatlog_conn()

Zero dependency on memory_core, memory_bridge, or mcp_tool_catalog. Safe to import
from any module in bin/ without creating cycles.

Deprecated env var:
    CHATLOG_MODE — ignored with a warning. The former "integrated" behavior
    is now achieved by setting CHATLOG_DB_PATH equal to the main DB (or by
    leaving both unset when a shared M3_DATABASE covers everything).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

logger = logging.getLogger("chatlog_config")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "memory", ".chatlog_config.json")
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "memory", "agent_chatlog.db")
MAIN_DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")
STATE_FILE = os.path.join(BASE_DIR, "memory", ".chatlog_state.json")
SPILL_DIR = os.path.join(BASE_DIR, "memory", "chatlog_spill")
INGEST_CURSOR = os.path.join(BASE_DIR, "memory", ".chatlog_ingest_cursor.json")
CHATLOG_MIGRATIONS_DIR = os.path.join(BASE_DIR, "memory", "chatlog_migrations")

VALID_HOST_AGENTS: frozenset[str] = frozenset(("claude-code", "gemini-cli", "opencode", "aider"))
VALID_PROVIDERS: frozenset[str] = frozenset((
    "anthropic", "google", "openai", "local", "xai",
    "deepseek", "mistral", "meta", "other",
))


# ── Dataclasses ───────────────────────────────────────────────────────────────
@dataclass
class HookSpec:
    enabled: bool = False
    hook_path: str = ""
    last_seen: str = ""
    # claude-code only: if True, also capture on every assistant turn (Stop hook)
    # in addition to PreCompact. Default off — PreCompact alone is enough for
    # most users and avoids a Python spawn per turn. Cursor dedupes either way.
    stop_hook: bool = False


@dataclass
class EmbedSweeperSpec:
    batch_size: int = 256
    interval_min: int = 30


@dataclass
class RedactionSpec:
    enabled: bool = False  # OFF by default — local-first, opt-in
    patterns: list[str] = field(default_factory=lambda: [
        "api_keys", "bearer_tokens", "jwt", "aws_keys", "github_tokens",
    ])
    custom_regex: list[str] = field(default_factory=list)
    redact_pii: bool = False
    store_original_hash: bool = True


@dataclass
class CostTrackingSpec:
    enabled: bool = True  # ON by default — zero user-visible cost


@dataclass
class ChatlogConfig:
    db_path: str = DEFAULT_DB_PATH
    host_agents: dict[str, HookSpec] = field(default_factory=lambda: {
        "claude-code": HookSpec(),
        "gemini-cli":  HookSpec(),
        "opencode":    HookSpec(),
        "aider":       HookSpec(),
    })
    embed_default: bool = False
    embed_sweeper: EmbedSweeperSpec = field(default_factory=EmbedSweeperSpec)
    queue_flush_rows: int = 200
    queue_flush_ms: int = 1500
    queue_max_depth: int = 20_000
    backpressure: Literal["spill_to_disk", "block", "drop"] = "spill_to_disk"
    cost_tracking: CostTrackingSpec = field(default_factory=CostTrackingSpec)
    redaction: RedactionSpec = field(default_factory=RedactionSpec)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ── Cache ─────────────────────────────────────────────────────────────────────
_CACHE: Optional[ChatlogConfig] = None
_CACHE_LOCK = threading.Lock()
_MODE_WARN_EMITTED = False


def invalidate_cache() -> None:
    """Drop the cached config so the next resolve_config() re-reads env + file."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


# ── File I/O ──────────────────────────────────────────────────────────────────
def _load_file() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                logger.warning("Chatlog config at %s is not an object; ignoring.", CONFIG_PATH)
                return {}
            return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read %s: %s", CONFIG_PATH, e)
        return {}


def save_config(cfg: ChatlogConfig) -> None:
    """Persist cfg to CONFIG_PATH as pretty JSON (atomic rename)."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg.to_dict(), f, indent=2)
    os.replace(tmp, CONFIG_PATH)
    invalidate_cache()


# ── Resolver ──────────────────────────────────────────────────────────────────
def _path_from_env() -> Optional[str]:
    v = os.environ.get("CHATLOG_DB_PATH")
    if v is None:
        return None
    v = v.strip()
    return v or None


def _main_path_from_env() -> Optional[str]:
    """Respect M3_DATABASE: if the user unified on a single DB, chatlog shares it
    unless CHATLOG_DB_PATH explicitly says otherwise."""
    v = os.environ.get("M3_DATABASE")
    if v is None:
        return None
    v = v.strip()
    return v or None


def _warn_if_mode_env_set() -> None:
    """CHATLOG_MODE has been removed in the unified model. Warn once per process
    so stale shell configs surface visibly without breaking the caller."""
    global _MODE_WARN_EMITTED
    if _MODE_WARN_EMITTED:
        return
    if "CHATLOG_MODE" in os.environ:
        logger.warning(
            "CHATLOG_MODE is deprecated and ignored. The chatlog DB path is now "
            "resolved from CHATLOG_DB_PATH > M3_DATABASE > .chatlog_config.json > "
            "default. Set CHATLOG_DB_PATH equal to your main DB to restore the old "
            "'integrated' behavior."
        )
    _MODE_WARN_EMITTED = True


def _build_from_dict(d: dict) -> ChatlogConfig:
    """Construct a ChatlogConfig from a possibly-partial dict. Unknown keys
    (including the legacy `mode` field) are ignored."""
    base = ChatlogConfig()
    if not d:
        return base

    base.db_path = d.get("db_path", base.db_path) or base.db_path

    hooks_in = d.get("host_agents") or {}
    for host in list(base.host_agents.keys()):
        spec_in = hooks_in.get(host) or {}
        if isinstance(spec_in, dict):
            base.host_agents[host] = HookSpec(
                enabled=bool(spec_in.get("enabled", False)),
                hook_path=str(spec_in.get("hook_path", "") or ""),
                last_seen=str(spec_in.get("last_seen", "") or ""),
                stop_hook=bool(spec_in.get("stop_hook", False)),
            )

    base.embed_default = bool(d.get("embed_default", base.embed_default))

    sw = d.get("embed_sweeper") or {}
    if isinstance(sw, dict):
        base.embed_sweeper = EmbedSweeperSpec(
            batch_size=int(sw.get("batch_size", base.embed_sweeper.batch_size)),
            interval_min=int(sw.get("interval_min", base.embed_sweeper.interval_min)),
        )

    base.queue_flush_rows = int(d.get("queue_flush_rows", base.queue_flush_rows))
    base.queue_flush_ms   = int(d.get("queue_flush_ms", base.queue_flush_ms))
    base.queue_max_depth  = int(d.get("queue_max_depth", base.queue_max_depth))

    bp = d.get("backpressure", base.backpressure)
    if bp in ("spill_to_disk", "block", "drop"):
        base.backpressure = bp  # type: ignore[assignment]

    ct = d.get("cost_tracking") or {}
    if isinstance(ct, dict):
        base.cost_tracking = CostTrackingSpec(enabled=bool(ct.get("enabled", True)))

    rd = d.get("redaction") or {}
    if isinstance(rd, dict):
        base.redaction = RedactionSpec(
            enabled=bool(rd.get("enabled", False)),
            patterns=list(rd.get("patterns", base.redaction.patterns)),
            custom_regex=list(rd.get("custom_regex", [])),
            redact_pii=bool(rd.get("redact_pii", False)),
            store_original_hash=bool(rd.get("store_original_hash", True)),
        )

    return base


def resolve_config() -> ChatlogConfig:
    """Return the active config. Cached; call invalidate_cache() after edits."""
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is not None:
            return _CACHE

        _warn_if_mode_env_set()

        file_data = _load_file()
        cfg = _build_from_dict(file_data)

        env_path = _path_from_env()
        if env_path is not None:
            cfg.db_path = env_path
        else:
            # No explicit chatlog override — honor the unified M3_DATABASE so
            # a caller that set one env var gets a single-DB experience.
            main_env = _main_path_from_env()
            if main_env is not None:
                cfg.db_path = main_env

        _CACHE = cfg
        return cfg


# ── Convenience accessors ─────────────────────────────────────────────────────
def chatlog_db_path() -> str:
    """Effective DB path for chat log writes."""
    return resolve_config().db_path


# ── Connection pool ───────────────────────────────────────────────────────────
_POOL: Optional["queue.Queue[sqlite3.Connection]"] = None
_POOL_LOCK = threading.Lock()
_POOL_DB_PATH: Optional[str] = None  # path the pool was opened against; if it changes, rebuild


# Chatlog-tuned pragmas. Larger mmap and cache than main DB — chat logs are
# bigger and more append-heavy. WAL + synchronous=NORMAL is the zero-latency
# combo; wal_autocheckpoint bounds WAL growth.
_CHATLOG_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 30000",
    "PRAGMA wal_autocheckpoint = 2000",
    "PRAGMA journal_size_limit = 67108864",   # 64 MiB
    "PRAGMA temp_store = MEMORY",
    "PRAGMA mmap_size = 1073741824",          # 1 GiB
    "PRAGMA cache_size = -131072",            # 128 MiB
)


def _build_pool(db_path: str) -> "queue.Queue[sqlite3.Connection]":
    pool_size = int(os.environ.get("CHATLOG_DB_POOL_SIZE", "4"))
    pool_timeout = int(os.environ.get("CHATLOG_DB_POOL_TIMEOUT", "10"))
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    q: "queue.Queue[sqlite3.Connection]" = queue.Queue(maxsize=pool_size)
    for _ in range(pool_size):
        conn = sqlite3.connect(db_path, check_same_thread=False, timeout=pool_timeout)
        conn.row_factory = sqlite3.Row
        for pragma in _CHATLOG_PRAGMAS:
            conn.execute(pragma)
        q.put(conn)
    return q


def _ensure_pool() -> "queue.Queue[sqlite3.Connection]":
    global _POOL, _POOL_DB_PATH
    target = chatlog_db_path()
    with _POOL_LOCK:
        if _POOL is None or _POOL_DB_PATH != target:
            # Drop existing pool (connections will be GC'd when refs vanish)
            _POOL = _build_pool(target)
            _POOL_DB_PATH = target
        return _POOL


@contextmanager
def chatlog_sqlite_conn():
    """Context-managed connection from the chatlog pool.

    When chatlog_db_path() happens to equal the main DB, the pool here is
    still chatlog-tuned (larger mmap/cache, 30s busy_timeout). Callers that
    want the main-DB pool should route through ``M3Context.get_chatlog_conn``
    which detects the path-equality case and reuses the main pool instead.
    """
    pool = _ensure_pool()
    timeout = int(os.environ.get("CHATLOG_DB_POOL_TIMEOUT", "10"))
    conn = pool.get(timeout=timeout)
    try:
        yield conn
    finally:
        pool.put(conn)


# ── CLI self-test ─────────────────────────────────────────────────────────────
def _selftest() -> None:
    # Env > file > default
    os.environ.pop("CHATLOG_DB_PATH", None)
    os.environ.pop("M3_DATABASE", None)
    invalidate_cache()
    c = resolve_config()
    assert c.db_path == DEFAULT_DB_PATH

    os.environ["M3_DATABASE"] = MAIN_DB_PATH
    invalidate_cache()
    assert chatlog_db_path() == MAIN_DB_PATH
    os.environ.pop("M3_DATABASE")

    import tempfile
    tmp_db = os.path.join(tempfile.gettempdir(), "alt_chatlog.db")
    os.environ["CHATLOG_DB_PATH"] = tmp_db
    invalidate_cache()
    assert resolve_config().db_path == tmp_db
    os.environ.pop("CHATLOG_DB_PATH")
    invalidate_cache()

    print("chatlog_config.py self-tests passed")


if __name__ == "__main__":
    _selftest()
