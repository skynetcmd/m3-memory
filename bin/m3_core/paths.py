import argparse
import contextvars
import logging
import os
import sqlite3
import sys
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger("M3_SDK")

# ── Active-database ContextVar ────────────────────────────────────────────────
# Consulted by callers that want "whatever DB the surrounding request/CLI
# specified, else the default". The MCP tool dispatcher sets this before each
# tool call; CLI scripts set it once at startup.
_active_db: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "m3_active_db", default=None
)


def resolve_venv_python() -> str:
    """Returns the path to the project venv Python executable, cross-platform."""
    # __file__ is bin/m3_core/paths.py; three dirnames reach the repo root, the
    # same base the original bin/m3_sdk.py computed with two dirnames.
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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

    # __file__ is bin/m3_core/paths.py; three dirnames reach the repo root, the
    # same base the original bin/m3_sdk.py computed with two dirnames.
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
