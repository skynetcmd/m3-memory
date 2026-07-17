"""SQLite primitives, schema lifecycle, history, and gate cache.

Phase 2.B of the memory_core modularization. Holds:
  - Connection helpers (_db, _conn) that route through the active
    M3Context's connection pool
  - Lazy schema-init (_lazy_init, _ensure_sync_tables, _backfill_change_agent)
    with per-DB-path tracking via _initialized_dbs
  - History audit trail (_record_history, memory_history_impl)
  - Gate cache (_gate_active, _gate_count_query, _GATE_CACHE, _GATE_CACHE_TTL)
    used by the auto-activation gates in memory_core
  - Access-stamp batcher (_access_stamp_flusher, _enqueue_access_stamps,
    _access_pending, _access_lock, _access_flusher_task)

Mutable module-level state (`_initialized_dbs`, `_GATE_CACHE`,
`_access_pending`, `_access_flusher_task`) is externally observable
through the memory_core re-export shim. Callers must NOT rebind these
names from outside; only mutate the existing container objects.

Subtle dependency notes:
  - `_db()` resolves the active context via `M3Context.for_db(resolve_db_path(None))`
    rather than calling memory_core's `_current_ctx` (avoids the circular
    import that would otherwise loop db.py back through memory_core).
  - `_backfill_change_agent` uses `infer_change_agent` from `m3_sdk` directly,
    again to avoid the circular.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from embedding_utils import infer_change_agent as _infer_change_agent_util
from m3_sdk import M3Context, migration_lock, resolve_db_path

from . import config

logger = logging.getLogger("memory.db")


# ──────────────────────────────────────────────────────────────────────────────
# Connection-pool state (per-thread)
# ──────────────────────────────────────────────────────────────────────────────
_local = threading.local()
_init_lock = threading.RLock()
_initialized = False  # legacy flag — once true, stays true
# Per-DB-path tracking. Externally imported (per the migration audit) — DO NOT
# rebind this set; only mutate it in place.
_initialized_dbs: set[str] = set()


# ──────────────────────────────────────────────────────────────────────────────
# Gate cache: ~5 min memoized COUNT(*) results for auto-activation gates
# ──────────────────────────────────────────────────────────────────────────────
# Externally imported. DO NOT rebind; only mutate.
_GATE_CACHE: dict[str, tuple[bool, float]] = {}
_GATE_CACHE_TTL = 300  # seconds; counts can change as drains run

_OBS_COUNT_QUERY = "SELECT COUNT(*) FROM memory_items WHERE type='observation' AND COALESCE(is_deleted,0)=0"
_ENTITY_COUNT_QUERY = "SELECT COUNT(*) FROM entities"


# ──────────────────────────────────────────────────────────────────────────────
# Access-stamp batcher: coalesce last_accessed_at UPDATEs into bulk writes
# ──────────────────────────────────────────────────────────────────────────────
_ACCESS_FLUSH_INTERVAL = 0.25  # seconds
_access_pending: set[str] = set()
_access_flusher_task: "asyncio.Task | None" = None
_access_lock = asyncio.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# Connection helpers
# ──────────────────────────────────────────────────────────────────────────────
def _current_ctx_local() -> M3Context:
    """Return the M3Context for the currently active DB path.

    Resolved here instead of imported from memory_core to avoid a circular
    import. Honors active_database() ContextVar > M3_DATABASE env > default.
    """
    return M3Context.for_db(resolve_db_path(None))


@contextmanager
def _db():
    """Open a read/write connection from the ACTIVE backend.

    On SQLite (the default) this opens a pooled ``sqlite3.Connection`` from the
    active context's pool, triggering ``_lazy_init`` on first touch per DB path,
    committing on clean exit and rolling back on exception — byte-identical to
    the pre-backend behavior.

    On PostgreSQL it delegates to ``active_backend().connection()``, whose
    context manager already applies the SAME discipline (commit on clean exit,
    rollback on exception, return to pool). We therefore just yield through its
    already-wrapped connection rather than re-implementing commit/rollback here.

    Routing lives in this single canonical helper so every module that did
    ``from .db import _db`` becomes backend-aware for free; the previous per-file
    wrapper (memory.write._db) collapses into this. Tests that monkeypatch a
    module-local ``_db`` still win — they replace the imported name, not this
    definition — and all such tests run on the SQLite default, where this path is
    unchanged.
    """
    # Backend routing. SQLite is the legacy special case (the M3Context pool path
    # below); EVERY OTHER backend routes through the seam's connection(). Keyed on
    # `!= "sqlite"`, not `== "postgres"`, so a future SQL backend (e.g. MariaDB)
    # goes through the seam too rather than silently falling into the SQLite path
    # and touching the wrong store. The PROBE is guarded so a missing/half-
    # initialized backend layer falls through to the historical SQLite path; but
    # once we route to the seam, the connection and the caller's work run OUTSIDE
    # the guard — a real backend error must propagate, never be swallowed into a
    # silent fall-through to the wrong store.
    _use_seam = False
    _backend = None
    try:
        from memory.backends import active_backend as _ab

        _backend = _ab()
        _use_seam = _backend.name != "sqlite"
    except Exception:
        if os.environ.get("M3_DEBUG"):
            import traceback

            traceback.print_exc()
        _use_seam = False

    if _use_seam:
        # The backend's connection() applies its own commit/rollback discipline
        # (see StorageBackend.connection); we only yield through it. Errors propagate.
        with _backend.connection() as conn:
            yield conn
        return

    active_ctx = _current_ctx_local()
    if os.environ.get("M3_DEBUG"):
        print(f"DEBUG DB PATH: {active_ctx.db_path}")
    _lazy_init(active_ctx.db_path)
    with active_ctx.get_sqlite_conn() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


@contextmanager
def _conn():
    """Legacy alias for _db context manager (C7)."""
    with _db() as db:
        yield db


# ──────────────────────────────────────────────────────────────────────────────
# Schema lifecycle
# ──────────────────────────────────────────────────────────────────────────────
def _ensure_sync_tables(db_path: str | None = None) -> None:
    """Run pending migrations against the active DB.

    Fast path: if the schema is already at the latest version on disk
    (compared against the migration files in memory/migrations/ or
    memory/chatlog_migrations/), skip the subprocess entirely.

    Test escape hatch: when M3_SKIP_MIGRATIONS=1 is set, return without
    doing anything. Test fixtures that pre-create the full post-v031
    schema (via tests/conftest.py's `create_full_main_schema`) set this
    so the migration subprocess doesn't re-run on an already-current DB.
    """
    if os.environ.get("M3_SKIP_MIGRATIONS", "").lower() in ("1", "true", "yes"):
        return
    try:
        migration_script = os.path.join(config.BASE_DIR, "bin", "migrate_memory.py")

        # Detect chatlog context via schema fingerprint.
        active = db_path or resolve_db_path(None)
        target_flag: list[str] = []
        target_kind = "main"
        try:
            sys.path.insert(0, os.path.join(config.BASE_DIR, "bin"))
            from migrate_memory import _classify_db
            if _classify_db(active) == "chatlog":
                target_flag = ["--target", "chatlog"]
                target_kind = "chatlog"
        except Exception:
            pass

        # Fast path: compare DB's applied version vs. the highest .up.sql file
        # number for the resolved target. If equal, no migrations to apply.
        try:
            mig_dir = os.path.join(
                config.BASE_DIR, "memory",
                "chatlog_migrations" if target_kind == "chatlog" else "migrations",
            )
            file_versions = []
            pattern = re.compile(r"^(\d+)_.*\.up\.sql$")
            for fn in os.listdir(mig_dir):
                m = pattern.match(fn)
                if m:
                    file_versions.append(int(m.group(1)))
            latest_on_disk = max(file_versions) if file_versions else -1

            db_latest = -1
            conn = sqlite3.connect(f"file:{active}?mode=ro", uri=True, timeout=2.0)
            try:
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('schema_versions','schema_migrations')"
                ).fetchall()
                tables = {r[0] for r in cur}
                if "schema_versions" in tables:
                    row = conn.execute(
                        "SELECT MAX(CAST(version AS INTEGER)) FROM schema_versions "
                        "WHERE typeof(CAST(version AS INTEGER))='integer' "
                        "  AND CAST(version AS INTEGER) > 0"
                    ).fetchone()
                    db_latest = int(row[0]) if row and row[0] is not None else -1
                elif "schema_migrations" in tables:
                    row = conn.execute(
                        "SELECT MAX(CAST(version AS INTEGER)) FROM schema_migrations "
                        "WHERE typeof(CAST(version AS INTEGER))='integer' "
                        "  AND CAST(version AS INTEGER) > 0"
                    ).fetchone()
                    db_latest = int(row[0]) if row and row[0] is not None else -1
            finally:
                conn.close()

            if latest_on_disk >= 0 and db_latest >= latest_on_disk:
                return
        except Exception:
            pass

        env = os.environ.copy()
        if target_flag:
            env["M3_DATABASE"] = active
        try:
            from _task_runtime import no_window_kwargs
            _nw = no_window_kwargs()
        except Exception:
            _nw = {}
        with migration_lock():
            subprocess.run(
                [sys.executable, migration_script, "up", "--yes", *target_flag],
                check=True,
                timeout=300,
                stdin=subprocess.DEVNULL,
                env=env,
                **_nw,
            )
    except Exception as e:
        logger.exception(f"_ensure_sync_tables failed: {e}")


def _backfill_change_agent() -> None:
    try:
        with _db() as db:
            rows = db.execute(
                "SELECT id, agent_id, model_id FROM memory_items WHERE change_agent IS NULL"
            ).fetchall()
            for row in rows:
                agent = _infer_change_agent_util(
                    row["agent_id"] or "", row["model_id"] or "", default="legacy"
                )
                db.execute("UPDATE memory_items SET change_agent = ? WHERE id = ?", (agent, row["id"]))
    except Exception as e:
        logger.warning(f"Backfill failed: {e}")


def ensure_pinned_column(conn) -> None:
    """Idempotently add memory_items.pinned INTEGER DEFAULT 0. Best-effort:
    a DB that predates this (or already has it) is a no-op. Mirrors the
    bin/enrich/prep.py::_ensure_migration_025 runtime-DDL fallback — no
    migration file is required (the migrations dir chain is not fully
    in-tree; pre-v2026.7.1.0 DBs are no longer supported).

    Pinned memories (pinned=1) are exempt from decay, expiry, and
    retention purges — see bin/memory_maintenance.py.
    """
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_items)")}
        if "pinned" not in cols:
            conn.execute("ALTER TABLE memory_items ADD COLUMN pinned INTEGER DEFAULT 0")
            conn.commit()
    except Exception:  # noqa: BLE001 — never break the caller
        pass


def _lazy_init(db_path: str | None = None) -> None:
    """Run one-time schema + backfill per DB path. Per-DB to support multi-DB."""
    global _initialized
    key = db_path or resolve_db_path(None)
    with _init_lock:
        if key in _initialized_dbs:
            return
        _initialized_dbs.add(key)
        _initialized = True  # legacy flag — once true, stays true
        try:
            _ensure_sync_tables(key)
            _backfill_change_agent()
            try:
                conn = sqlite3.connect(str(key), timeout=10.0)
                try:
                    ensure_pinned_column(conn)
                finally:
                    conn.close()
            except Exception:
                # Best-effort — never let the pinned-column bootstrap break
                # DB init. ensure_pinned_column() also self-guards, but the
                # connect() itself could fail on an exotic path.
                pass
        except Exception:
            # Do not trap init in a permanently-failed state — let next caller retry.
            _initialized_dbs.discard(key)
            raise


# ──────────────────────────────────────────────────────────────────────────────
# History audit trail
# ──────────────────────────────────────────────────────────────────────────────
def _record_history(
    memory_id: str,
    event: str,
    prev_value: str | None = None,
    new_value: str | None = None,
    field: str = "content",
    actor_id: str = "",
    db: Any = None,
) -> None:
    """Records a change event in the memory_history audit trail.

    Pass ``db`` when the caller already holds an open connection (e.g. inside a
    ``with _db() as db:`` block). Opening a second pool connection while the
    outer one has an uncommitted writer causes SQLite WAL writer contention,
    which burns the full ``busy_timeout`` per call.
    """
    from memory.backends import active_backend as _active_backend

    _d = _active_backend().dialect()
    row = (str(uuid.uuid4()), memory_id, event, prev_value, new_value, field, actor_id)
    sql = (
        "INSERT INTO memory_history "
        "(id, memory_id, event, prev_value, new_value, field, actor_id) "
        f"VALUES ({_d.placeholder(7)})"
    )
    try:
        if db is not None:
            db.execute(sql, row)
        else:
            with _db() as inner:
                inner.execute(sql, row)
    except Exception as e:
        # NOTE: on PostgreSQL a failed statement aborts the WHOLE transaction, so
        # a swallowed error here on the caller's shared `db` would silently roll
        # back the caller's writes too. Dialecting the SQL above prevents the
        # syntax error that used to trigger exactly that (superseded rows silently
        # not persisting). Keep the catch for genuinely-tolerable failures (e.g. a
        # pre-migration DB with no memory_history table).
        logger.debug(f"History recording failed: {e}")


def memory_history_impl(memory_id: str, limit: int = 20) -> str:
    """Returns the change history for a memory item."""
    from memory.backends import active_backend as _active_backend

    _p = _active_backend().dialect().param()
    with _db() as db:
        rows = db.execute(
            "SELECT event, field, prev_value, new_value, actor_id, created_at "
            f"FROM memory_history WHERE memory_id = {_p} ORDER BY created_at DESC LIMIT {_p}",
            (memory_id, limit),
        ).fetchall()
    if not rows:
        return f"No history found for {memory_id}"
    lines = [f"History for {memory_id} ({len(rows)} events):"]
    for r in rows:
        prev = (r["prev_value"] or "")[:80]
        new = (r["new_value"] or "")[:80]
        lines.append(
            f"  [{r['created_at']}] {r['event']} ({r['field']}) by {r['actor_id'] or 'unknown'}: "
            f"{prev!r} -> {new!r}"
        )
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Gate cache
# ──────────────────────────────────────────────────────────────────────────────
def _gate_count_query(query: str) -> int:
    """Run a COUNT(*) query against the active SQLite DB. Returns 0 on error."""
    try:
        with _db() as db:
            row = db.execute(query).fetchone()
            if row is None:
                return 0
            return int(row[0] if not hasattr(row, "keys") else list(row)[0])
    except Exception:
        return 0


def _gate_active(env_var: str, count_query: str, threshold: int = 1) -> bool:
    """True if env var is explicitly on, or auto-activated by data presence.

    Cached per (env_var, count_query) for ~5 min; the cache is invalidated by
    process restart or natural TTL expiry. Single-process; no thread lock — a
    stampede on first miss would just run COUNT(*) twice, harmless.

    The count-query function is resolved at call time so tests that
    monkeypatch `memory_core._gate_count_query` (legacy pattern) take
    effect. Production reads the module-local `_gate_count_query`.
    """
    if os.environ.get(env_var, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if os.environ.get("M3_DISABLE_AUTO_ACTIVATION", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    cache_key = f"{env_var}::{count_query}"
    cached = _GATE_CACHE.get(cache_key)
    now = time.monotonic()
    # Resolve TTL via memory_core for legacy tests that monkeypatch it
    # (test_phase_l_auto_activation::test_cache_expires_after_ttl).
    _ttl = _GATE_CACHE_TTL
    _count_fn = _gate_count_query
    try:
        import memory_core as _mc  # type: ignore
        _ttl = getattr(_mc, "_GATE_CACHE_TTL", _ttl)
        _count_fn = getattr(_mc, "_gate_count_query", _count_fn)
    except ImportError:
        pass
    if cached is not None and (now - cached[1]) < _ttl:
        return cached[0]
    count = _count_fn(count_query)
    active = count >= threshold
    _GATE_CACHE[cache_key] = (active, now)
    return active


# ──────────────────────────────────────────────────────────────────────────────
# Access-stamp batcher
# ──────────────────────────────────────────────────────────────────────────────
async def _access_stamp_flusher() -> None:
    """Drains _access_pending into a single batched UPDATE on a fixed cadence.

    Lives for the lifetime of the running event loop. Per-loop singleton —
    created lazily by ``_enqueue_access_stamps``. Catches its own errors so a
    transient DB lock can't kill the long-lived task.
    """
    while True:
        try:
            await asyncio.sleep(_ACCESS_FLUSH_INTERVAL)
            async with _access_lock:
                if not _access_pending:
                    continue
                batch = list(_access_pending)
                _access_pending.clear()
            now_iso = datetime.now(timezone.utc).isoformat()
            try:
                with _db() as db:
                    placeholders = ",".join("?" * len(batch))
                    db.execute(
                        f"UPDATE memory_items "
                        f"SET last_accessed_at = ?, access_count = access_count + 1 "
                        f"WHERE id IN ({placeholders})",
                        (now_iso, *batch),
                    )
            except Exception as e:  # noqa: BLE001
                logger.debug(f"access-stamp flush failed (batch={len(batch)}): {e}")
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001 — keep the task alive
            logger.debug(f"access-stamp flusher recoverable error: {e}")


def _enqueue_access_stamps(ids) -> None:
    """Buffer hit-ids for a fire-and-forget UPDATE. Idempotent / dedup'd."""
    global _access_flusher_task
    if not ids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop -> skip; sync callers don't need this
    _access_pending.update(i for i in ids if i)
    if _access_flusher_task is None or _access_flusher_task.done():
        _access_flusher_task = loop.create_task(_access_stamp_flusher())
