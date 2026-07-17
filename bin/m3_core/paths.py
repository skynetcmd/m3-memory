import argparse
import contextvars
import logging
import os
import sqlite3
import sys
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger("M3_SDK")

# ── Deprecated env-var compatibility shim ─────────────────────────────────────
# m3-specific config vars are being namespaced under M3_*. Old un-namespaced
# names (PG_URL, tuning knobs, ...) are collision-prone with
# other tools. getenv_compat reads the new name, falls back to the old one with
# a ONE-TIME deprecation warning, and records the hit so `m3 doctor` can report
# which deprecated vars are still in use (fail loud + observable, §3).
#
# DEPRECATED_ENV_RENAMES is the STATIC, authoritative old_name -> new_name map
# for the M3_* namespacing migration. It is the source of truth for the on-disk
# env-migration helper (`m3 doctor` scans config files for these OLD names and
# `--fix` rewrites them), which cannot rely on _DEPRECATED_ENV_SEEN (that only
# records names READ this process — a var sitting unread in a settings.json env
# block would never appear). Every getenv_compat(new, old) call site MUST have a
# matching entry here; tests/test_env_rename_map_drift.py enforces that so the map
# can't silently fall out of sync with the call sites.
DEPRECATED_ENV_RENAMES: dict[str, str] = {
    "CHATLOG_DB_PATH":         "M3_CHATLOG_DB_PATH",
    "CHATLOG_DB_POOL_SIZE":    "M3_CHATLOG_DB_POOL_SIZE",
    "CHATLOG_DB_POOL_TIMEOUT": "M3_CHATLOG_DB_POOL_TIMEOUT",
    # CHROMA_BASE_URL is NOT here: the ChromaDB feature was retired, so there is
    # no live getenv_compat call site for it. It moved to RETIRED_ENV_RENAMES so
    # `m3 doctor` still cleans a stale CHROMA_BASE_URL out of on-disk config. See
    # below.
    "DB_BACKEND":              "M3_DB_BACKEND",
    "DB_POOL_SIZE":            "M3_DB_POOL_SIZE",
    "DB_POOL_TIMEOUT":         "M3_DB_POOL_TIMEOUT",
    "IMPORTANCE_WEIGHT":       "M3_IMPORTANCE_WEIGHT",
    "LLM_ENDPOINTS_CSV":       "M3_LLM_ENDPOINTS_CSV",
    "ORIGIN_DEVICE":           "M3_ORIGIN_DEVICE",
    # PG_URL is NOT here: it was split by role (primary vs warehouse), so it lives
    # in ROLE_SPLIT_ENV_RENAMES (-> M3_CDW_PG_URL) and is resolved by the
    # role-specific resolvers, not getenv_compat. See below.
    "POSTGRES_SERVER":         "M3_POSTGRES_SERVER",
    "SEARCH_ROW_CAP":          "M3_SEARCH_ROW_CAP",
    "SHORT_TURN_THRESHOLD":    "M3_SHORT_TURN_THRESHOLD",
    "SPEAKER_IN_TITLE":        "M3_SPEAKER_IN_TITLE",
    "SUPERSEDES_PENALTY":      "M3_SUPERSEDES_PENALTY",
    "SYNC_TARGET_IP":          "M3_SYNC_TARGET_IP",
    "TITLE_MATCH_BOOST":       "M3_TITLE_MATCH_BOOST",
}

# ROLE_SPLIT_ENV_RENAMES: deprecations that are NOT pure M3_ namespacing because
# the var was split by role (one old name -> a role-specific new name). Kept
# separate from DEPRECATED_ENV_RENAMES so the "new == M3_ + old" invariant on that
# map (test_env_rename_map_drift.test_rename_map_is_pure_namespacing) still holds.
# `m3 doctor` scans BOTH maps when reporting/rewriting on-disk config (via
# all_env_renames()); getenv_compat only consults the pure-namespacing map.
#
# PG_URL was overloaded (primary store vs data-warehouse). The doctor-fix target
# is the WAREHOUSE name, since that is what a legacy PG_URL almost always meant
# (pg_sync). An operator who actually wanted a PG PRIMARY store sets
# M3_PRIMARY_PG_URL by hand — doctor can't tell the two intents apart, so it
# rewrites to the common case and the deprecation message names both.
ROLE_SPLIT_ENV_RENAMES: dict[str, str] = {
    "PG_URL": "M3_CDW_PG_URL",
}

# RETIRED_ENV_RENAMES: vars for FEATURES THAT NO LONGER EXIST. There is no live
# getenv_compat call site (the reading code is gone), so these are deliberately
# NOT in DEPRECATED_ENV_RENAMES — the drift test asserts that map has a call site
# for every entry. But `m3 doctor` should still scrub a stale old name out of a
# user's on-disk config (a settings.json env block, a dotenv line), so they are
# reported/rewritten via all_env_renames(). The rename is cosmetic: nothing reads
# either name anymore. Kept separate from ROLE_SPLIT so the "new == M3_ + old"
# invariant on THAT map is not muddied.
#
#   * CHROMA_BASE_URL — ChromaDB (optional L3 sync backend) was retired. Nothing
#     reads it; doctor renames M3_-consistently so the old name stops lingering.
RETIRED_ENV_RENAMES: dict[str, str] = {
    "CHROMA_BASE_URL": "M3_CHROMA_BASE_URL",
}


def all_env_renames() -> dict[str, str]:
    """Union of the pure-namespacing, role-split, and retired deprecation maps.

    The single source of truth for `m3 doctor`'s on-disk config scan/rewrite:
    every old env-var KEY that should be reported and renamed, mapped to its new
    name. getenv_compat does NOT use this (it only does pure namespacing); the
    role-split names have dedicated resolvers (resolve_cdw_pg_dsn, etc.), and the
    retired names have no reader at all — doctor just scrubs stale config.
    """
    merged = dict(DEPRECATED_ENV_RENAMES)
    merged.update(ROLE_SPLIT_ENV_RENAMES)
    merged.update(RETIRED_ENV_RENAMES)
    return merged


# _DEPRECATED_ENV_SEEN maps old_name -> new_name for every deprecated var that
# was actually read from the environment this process. Doctor reads it via
# deprecated_env_in_use().
_DEPRECATED_ENV_SEEN: dict[str, str] = {}
_DEPRECATED_ENV_WARNED: set[str] = set()


def getenv_compat(new_name: str, old_name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve an env var during the M3_* namespacing migration.

    Precedence: new_name > old_name > default. If only the old (deprecated)
    name is set, warn once and record it in _DEPRECATED_ENV_SEEN so `m3 doctor`
    can surface it. The new name always wins, so users can migrate incrementally.
    """
    val = os.environ.get(new_name)
    if val is not None:
        return val
    old_val = os.environ.get(old_name)
    if old_val is not None:
        _DEPRECATED_ENV_SEEN[old_name] = new_name
        if old_name not in _DEPRECATED_ENV_WARNED:
            _DEPRECATED_ENV_WARNED.add(old_name)
            logger.warning(
                "Deprecated env var %s is set; use %s instead. The old name still "
                "works for now but will be removed. (`m3 doctor` lists all in use.)",
                old_name, new_name,
            )
        return old_val
    return default


def deprecated_env_in_use() -> dict[str, str]:
    """Return {old_name: new_name} for every deprecated env var read so far this
    process (via getenv_compat OR the role-specific PG resolvers). Used by
    `m3 doctor` to report migration TODOs. Only reflects names actually READ — a
    var set but never consulted won't show.
    """
    return dict(_DEPRECATED_ENV_SEEN)


# ── PostgreSQL DSN resolution: two ROLES, deliberately separated ──────────────
# m3 uses PostgreSQL in two unrelated roles that historically read the SAME env
# var (PG_URL / M3_PG_URL) — a footgun, because setting the warehouse DSN would
# silently arm the primary-store path (and vice-versa). They are now separated:
#
#   * PRIMARY store   (opt-in `M3_DB_BACKEND=postgres`): a single instance's
#     authoritative read/write DB. Resolved by `resolve_primary_pg_dsn`.
#       precedence: M3_PRIMARY_PG_URL > M3_PG_URL > vault. NEVER reads a CDW var.
#   * CDW / WAREHOUSE (pg_sync fan-in mirror, `m3_warehouse` namespace): a shared
#     aggregate many instances UPSERT into. Resolved by `resolve_cdw_pg_dsn`.
#       precedence: M3_CDW_PG_URL > PG_URL(deprecated, warns) > vault.
#
# `PG_URL` is DEPRECATED for the warehouse role: it still resolves as a last
# resort so live sync doesn't break mid-migration, but every read warns once and
# is recorded for `m3 doctor`, and install/upgrade HARD-FAILS if it is set (see
# assert_no_deprecated_pg_url_on_install). Rename it to M3_CDW_PG_URL.
#
# The primary role does NOT accept `PG_URL` at all — a warehouse DSN can never
# reach the primary store through env resolution.

# The canonical deprecation: old PG_URL -> the warehouse (CDW) namespace. This is
# NOT pure M3_ namespacing (PG_URL -> M3_CDW_PG_URL, not M3_PG_URL), so it is
# tracked here rather than in DEPRECATED_ENV_RENAMES (which is namespacing-only).
PG_URL_DEPRECATION: tuple[str, str] = ("PG_URL", "M3_CDW_PG_URL")


def _warn_pg_url_deprecated() -> None:
    """One-time warning + doctor-visible record that PG_URL is set (warehouse)."""
    old, new = PG_URL_DEPRECATION
    _DEPRECATED_ENV_SEEN[old] = new
    if old not in _DEPRECATED_ENV_WARNED:
        _DEPRECATED_ENV_WARNED.add(old)
        logger.warning(
            "Deprecated env var %s is set; use %s instead for the data-warehouse "
            "DSN. The old name still works for now but will be removed, and "
            "`m3 install`/`update` will refuse to run while it is set. "
            "(`m3 doctor --fix` renames it.)",
            old, new,
        )


def resolve_primary_pg_dsn(default: Optional[str] = None) -> Optional[str]:
    """DSN for the PRIMARY PostgreSQL store (M3_DB_BACKEND=postgres).

    Precedence: M3_PRIMARY_PG_URL > M3_PG_URL > default. Deliberately does NOT
    read PG_URL or any M3_CDW_* var — the warehouse DSN must never reach the
    primary store through env resolution. Callers add vault/forbidden-host checks.
    """
    val = os.environ.get("M3_PRIMARY_PG_URL")
    if val is not None:
        return val
    val = os.environ.get("M3_PG_URL")
    if val is not None:
        return val
    return default


def resolve_cdw_pg_dsn(default: Optional[str] = None) -> Optional[str]:
    """DSN for the CDW / data-warehouse PostgreSQL (pg_sync fan-in mirror).

    Precedence: M3_CDW_PG_URL > PG_URL(deprecated) > default. Reading the legacy
    PG_URL warns once and is recorded for `m3 doctor`. Does NOT read M3_PG_URL —
    that is the primary-store var; a warehouse consumer that wants the old
    behavior must set M3_CDW_PG_URL explicitly.
    """
    val = os.environ.get("M3_CDW_PG_URL")
    if val is not None:
        return val
    val = os.environ.get("PG_URL")
    if val is not None:
        _warn_pg_url_deprecated()
        return val
    return default


def _reset_pg_url_deprecation_state_for_tests() -> None:
    """Clear the one-time PG_URL deprecation latch + seen-record. Test-only.

    The 'warn once' behavior of ``resolve_cdw_pg_dsn`` is module-global; a test
    that asserts the warning fires must reset this first or it gets order-dependent
    flakiness. Exposed as a named helper so tests don't reach into private globals.
    """
    old, _ = PG_URL_DEPRECATION
    _DEPRECATED_ENV_WARNED.discard(old)
    _DEPRECATED_ENV_SEEN.pop(old, None)


def assert_no_deprecated_pg_url_on_install() -> None:
    """Hard-fail an install/upgrade if the deprecated PG_URL env var is set.

    Rationale: install/upgrade is the one moment the operator is present and can
    fix config, so we force the rename here rather than letting a stale PG_URL
    keep shadowing behavior forever. Runtime paths only warn (a hard-fail there
    would be an outage); install is the safe place to be strict. Raises
    RuntimeError with the exact remediation.
    """
    old, new = PG_URL_DEPRECATION
    if os.environ.get(old) is not None:
        raise RuntimeError(
            f"Deprecated env var {old} is set. It has been split by role and "
            f"renamed: use {new} for the data-warehouse (pg_sync) DSN, or "
            f"M3_PRIMARY_PG_URL for a PostgreSQL PRIMARY store. Unset {old} and "
            f"set the correct one, then re-run. (See CHANGELOG: 'PG_URL split by "
            f"role'.) Refusing to install/upgrade with an ambiguous {old} set."
        )


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


def resolve_engine_file(filename: str) -> str:
    """Resolve a path under the engine root, honoring the legacy fallback.

    Returns ``<engine_root>/<filename>`` unless a legacy copy exists at
    ``<memory_root>/memory/<filename>`` and the new path does not — in which
    case the legacy path is returned so pre-Homecoming installs keep working.

    Single source of truth: chatlog_config, memory.config and migrate_memory
    previously each carried a byte-identical copy of this helper. They now
    import it from here so the resolution rule lives in exactly one place.
    """
    new_path = os.path.join(get_m3_engine_root(), filename)
    legacy_path = os.path.join(get_m3_root(), "memory", filename)
    if os.path.exists(legacy_path) and not os.path.exists(new_path):
        return legacy_path
    return new_path


def resolve_config_file(filename: str) -> str:
    """Resolve a path under the config root, honoring the legacy fallback.

    Returns ``<config_root>/<filename>`` unless a legacy copy exists at
    ``<memory_root>/memory/<filename>`` and the new path does not. Companion to
    :func:`resolve_engine_file`; see that docstring for the de-duplication note.
    """
    new_path = os.path.join(get_m3_config_root(), filename)
    legacy_path = os.path.join(get_m3_root(), "memory", filename)
    if os.path.exists(legacy_path) and not os.path.exists(new_path):
        return legacy_path
    return new_path


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
