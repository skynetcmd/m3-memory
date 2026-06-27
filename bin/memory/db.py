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
  - Write-queue daemon (WriteQueueDaemon, _enqueue_write, _get_write_daemon,
    _write_daemons) that coalesces concurrent single-row writes into batched
    single-transaction commits to reduce SQLite write-lock contention

Mutable module-level state (`_initialized_dbs`, `_GATE_CACHE`,
`_access_pending`, `_access_flusher_task`, `_write_daemons`) is externally observable
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
    """Open a SQLite connection from the active context's pool.

    Triggers _lazy_init on first touch per DB path. Commits on clean exit,
    rolls back on exception.
    """
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
    row = (str(uuid.uuid4()), memory_id, event, prev_value, new_value, field, actor_id)
    sql = (
        "INSERT INTO memory_history "
        "(id, memory_id, event, prev_value, new_value, field, actor_id) "
        "VALUES (?,?,?,?,?,?,?)"
    )
    try:
        if db is not None:
            db.execute(sql, row)
        else:
            with _db() as inner:
                inner.execute(sql, row)
    except Exception as e:
        logger.debug(f"History recording failed: {e}")


def memory_history_impl(memory_id: str, limit: int = 20) -> str:
    """Returns the change history for a memory item."""
    with _db() as db:
        rows = db.execute(
            "SELECT event, field, prev_value, new_value, actor_id, created_at "
            "FROM memory_history WHERE memory_id = ? ORDER BY created_at DESC LIMIT ?",
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


# ──────────────────────────────────────────────────────────────────────────────
# Write-queue daemon: coalesce concurrent single-row writes into batched commits
# ──────────────────────────────────────────────────────────────────────────────
# SQLite serializes writers; under heavy concurrent ingest, independent
# `memory_write` calls each open their own transaction and contend for the
# write lock, producing `database is locked` stalls. The WriteQueueDaemon
# funnels all enqueued writes through ONE writer task that drains the queue on
# a short aggregation window and commits the whole batch in a single
# transaction. Each caller still awaits its own result via a per-item future,
# so the API contract (write happened, here's the outcome) is preserved.
#
# Design note vs. the access-stamp batcher above: that path is fire-and-forget
# (no return value, last-write-wins dedup). Writes are NOT — each carries a
# distinct row and a caller awaiting success/failure — so every item gets its
# own future and a per-item error is isolated to that future, never failing
# the rest of the batch.
#
# Per-loop singleton, keyed by db_path: each event loop + DB path gets one
# daemon, created lazily on first enqueue. M3_WRITE_QUEUE_DISABLE=1 forces the
# direct-write path (the daemon is opt-in via _enqueue_write callers).
_WRITE_QUEUE_FLUSH_INTERVAL = float(os.environ.get("M3_WRITE_QUEUE_INTERVAL", "0.1"))  # 100ms window
_WRITE_QUEUE_MAX_BATCH = int(os.environ.get("M3_WRITE_QUEUE_MAX_BATCH", "50"))  # rows per commit
# Per-(loop_id, db_path) daemon registry. DO NOT rebind; only mutate in place.
_write_daemons: "dict[tuple[int, str], WriteQueueDaemon]" = {}


class WriteQueueDaemon:
    """Single-writer commit queue for one (event loop, db_path) pair.

    Callers `await enqueue_write(sql, params)`; the daemon batches up to
    ``_WRITE_QUEUE_MAX_BATCH`` statements arriving within a
    ``_WRITE_QUEUE_FLUSH_INTERVAL`` window and commits them in ONE
    transaction, then resolves each caller's future with its rowcount (or
    sets its exception). A failure on one statement is isolated to that
    statement's future — the batch still commits the rest.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.queue: "asyncio.Queue[tuple[str, tuple, asyncio.Future] | None]" = asyncio.Queue()
        self._task: "asyncio.Task | None" = None
        self._closed = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._flush_loop())

    async def enqueue_write(self, sql: str, params: tuple = ()) -> Any:
        """Submit a write; resolves to the statement's rowcount on commit."""
        if self._closed:
            raise RuntimeError("WriteQueueDaemon is closed")
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        await self.queue.put((sql, params, future))
        self.start()
        return await future

    async def _flush_loop(self) -> None:
        """Drain the queue and commit in batches until cancelled."""
        while True:
            try:
                # Block for the first item, then aggregate within the window.
                first = await self.queue.get()
                if first is None:  # shutdown sentinel
                    return
                batch = [first]
                await asyncio.sleep(_WRITE_QUEUE_FLUSH_INTERVAL)
                while len(batch) < _WRITE_QUEUE_MAX_BATCH:
                    try:
                        nxt = self.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if nxt is None:  # sentinel mid-drain: flush what we have, then stop
                        self._commit_batch(batch)
                        return
                    batch.append(nxt)
                self._commit_batch(batch)
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001 — keep the daemon alive
                logger.debug(f"write-queue flush recoverable error: {e}")

    def _commit_batch(self, batch: "list[tuple[str, tuple, asyncio.Future]]") -> None:
        """Execute every statement in one transaction; resolve each future.

        Per-item errors are captured and attached to that item's future
        without aborting the batch. If the COMMIT itself fails, every
        not-yet-resolved future in the batch gets the commit exception.
        """
        results: list[tuple[asyncio.Future, Any, BaseException | None]] = []
        try:
            with _db() as db:  # routes through the active context's pool; commits on exit
                for sql, params, future in batch:
                    if future.done():  # caller cancelled/timed out — skip
                        continue
                    try:
                        cur = db.execute(sql, params)
                        results.append((future, cur.rowcount, None))
                    except Exception as e:  # noqa: BLE001 — isolate to this future
                        results.append((future, None, e))
        except Exception as commit_err:  # noqa: BLE001 — whole transaction failed
            for sql, params, future in batch:
                if not future.done():
                    future.set_exception(commit_err)
            return
        for future, value, err in results:
            if future.done():
                continue
            if err is not None:
                future.set_exception(err)
            else:
                future.set_result(value)


def _get_write_daemon(db_path: str | None = None) -> WriteQueueDaemon:
    """Return the per-(loop, db_path) WriteQueueDaemon, creating it lazily."""
    loop = asyncio.get_running_loop()
    path = db_path or _current_ctx_local().db_path
    key = (id(loop), str(path))
    daemon = _write_daemons.get(key)
    if daemon is None:
        daemon = WriteQueueDaemon(str(path))
        _write_daemons[key] = daemon
    daemon.start()
    return daemon


async def _enqueue_write(sql: str, params: tuple = (), db_path: str | None = None) -> Any:
    """Coalesced write entrypoint. Awaits the batched commit and returns rowcount.

    Set ``M3_WRITE_QUEUE_DISABLE=1`` to bypass the daemon and commit inline
    (useful for tests or when single-writer aggregation isn't wanted).
    """
    if os.environ.get("M3_WRITE_QUEUE_DISABLE", "").lower() in ("1", "true", "yes"):
        with _db() as db:
            return db.execute(sql, params).rowcount
    daemon = _get_write_daemon(db_path)
    return await daemon.enqueue_write(sql, params)
