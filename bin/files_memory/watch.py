"""Watch-mode daemon for files-memory — phase 4.

A long-running poller that periodically invokes `files_staleness_review`
and emits a notification when files change. Three operating modes:

  - `watch_loop(...)`   — blocking event loop (used by the CLI)
  - `watch_once(...)`   — single pass; suitable for cron / scheduled tasks
  - `WatchState`        — pure dataclass; no I/O beyond the staleness probe
                          and the notify hook (which is injectable)

The daemon does NOT do filesystem-event watching (no inotify/watchdog
dependency). Polling is the design per FILE_INGESTION_PLAN.md §11 phase
4: simple, cross-platform, zero deps. Real-time response can be added in
a later phase if anyone needs it.

Notifications:
  - Default channel: m3-memory `notifications` table via memory_core's
    `notify_impl` (lightweight inbox; agents poll). One notification per
    (file_node_uuid, event_kind) pair within the cooldown window — we
    don't spam the same change repeatedly.
  - The notify path is injectable via `notify_callable` so tests can
    capture without touching memory.db.

State persistence:
  - Last-notified timestamps live in a SQLite key/value table
    `watch_state` under files.db (lazily created). Survives daemon
    restarts.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import config
from .db import _db
from .staleness import files_staleness_review

logger = logging.getLogger("files_memory.watch")


# ──────────────────────────────────────────────────────────────────────────────
# Persistence: watch_state table
# ──────────────────────────────────────────────────────────────────────────────
def _ensure_watch_state_table(conn: sqlite3.Connection) -> None:
    """Lazy-create the watch-state key/value table.

    Held inside files.db (not memory.db) because the state is
    files-memory-scoped and naturally co-located with the file_nodes
    these notifications reference.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS watch_state ("
        "  key TEXT PRIMARY KEY, "
        "  value TEXT NOT NULL, "
        "  updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )


def _watch_state_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM watch_state WHERE key = ?", (key,),
    ).fetchone()
    return row["value"] if row else None


def _watch_state_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO watch_state(key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value),
    )


def _cooldown_key(file_node_uuid: str, event_kind: str) -> str:
    return f"notify:{event_kind}:{file_node_uuid}"


# ──────────────────────────────────────────────────────────────────────────────
# Notification hook
# ──────────────────────────────────────────────────────────────────────────────
def _default_notify(agent_id: str, kind: str, payload: dict) -> Optional[str]:
    """Default channel: memory_core.notify_impl (writes to memory.db).

    Returns the result string from notify_impl, or None when memory_core
    is unavailable (e.g. running against a standalone files.db without a
    paired memory.db).
    """
    try:
        from memory_core import notify_impl  # type: ignore
    except ImportError:
        logger.debug("memory_core unavailable; skipping notify for kind=%s", kind)
        return None
    try:
        return notify_impl(agent_id=agent_id, kind=kind, payload=payload)
    except Exception as e:
        logger.warning("notify_impl raised for kind=%s: %s", kind, e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Watch state (per-cycle telemetry)
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class CycleResult:
    """Counters surfaced after a single watch cycle."""
    started_at: float
    duration_ms: int = 0
    stale_count: int = 0
    new_count: int = 0
    missing_count: int = 0
    failed_extraction_count: int = 0
    rename_candidate_count: int = 0
    drifted_promotion_count: int = 0
    notifications_emitted: int = 0
    notifications_suppressed_by_cooldown: int = 0
    errors: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Core: single-pass watch cycle
# ──────────────────────────────────────────────────────────────────────────────
def watch_once(
    *,
    directory: Optional[str] = None,
    corpus_id: Optional[str] = None,
    agent_id: str = "files_memory.watch",
    cooldown_seconds: float = 3600.0,
    notify_callable: Optional[Callable[[str, str, dict], Optional[str]]] = None,
    db_path: Optional[str] = None,
    notify_kinds: Optional[set[str]] = None,
) -> CycleResult:
    """Run one staleness review + notify pass.

    Args:
        directory: if set, scope the staleness review to this path.
        corpus_id: scope filter for the review.
        agent_id: notification recipient (memory.db notifications.agent_id).
        cooldown_seconds: don't re-notify the same (file_node, kind) within
            this window. Default 1 hour.
        notify_callable: pluggable notification sink. None → memory_core
            notify_impl. Tests inject a recording function.
        db_path: target files.db.
        notify_kinds: which event kinds to emit. Default = all of
            {stale, new, missing, failed_extraction, drifted_promotion,
             rename_candidate}.

    Returns counts for the cycle.
    """
    if notify_kinds is None:
        notify_kinds = {
            "stale", "new", "missing", "failed_extraction",
            "drifted_promotion", "rename_candidate",
        }
    notify = notify_callable or _default_notify
    t0 = time.perf_counter()
    result = CycleResult(started_at=time.time())

    try:
        rpt = files_staleness_review(
            directory=directory,
            corpus_id=corpus_id,
            db_path=db_path,
        )
    except Exception as e:
        logger.exception("staleness review raised: %s", e)
        result.errors.append(f"staleness_review: {type(e).__name__}: {e}")
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result

    result.stale_count = len(rpt.stale)
    result.new_count = len(rpt.new)
    result.missing_count = len(rpt.missing)
    result.failed_extraction_count = len(rpt.failed_extraction)
    result.rename_candidate_count = len(rpt.rename_candidates)
    result.drifted_promotion_count = len(rpt.drifted_promotions)

    # Cooldown bookkeeping + emission. Each (file_node_uuid, kind) pair
    # gets at most one notification per cooldown_seconds.
    now = time.time()
    with _db(db_path) as conn:
        _ensure_watch_state_table(conn)

        def _maybe_notify(kind: str, file_node_uuid: Optional[str], payload: dict) -> None:
            if kind not in notify_kinds:
                return
            # New files / drifted promotions / rename candidates don't
            # always have a file_node uuid handy — gate by a payload-
            # derived key in those cases.
            cool_key = _cooldown_key(file_node_uuid or payload.get("_key", kind), kind)
            last = _watch_state_get(conn, cool_key)
            if last:
                try:
                    if (now - float(last)) < cooldown_seconds:
                        result.notifications_suppressed_by_cooldown += 1
                        return
                except (TypeError, ValueError):
                    pass
            ok = notify(agent_id, f"files_staleness.{kind}", payload)
            if ok is not None:
                result.notifications_emitted += 1
                _watch_state_set(conn, cool_key, str(now))
            else:
                # Notify failed (memory_core unavailable etc.) — don't
                # set the cooldown, so we'll retry next cycle.
                pass

        # Emit per-bucket.
        for s in rpt.stale:
            # Find the file_node uuid for cooldown. Staleness review
            # returns the missing_file_node_uuid only for rename
            # candidates; for `stale` we have to look it up by path.
            file_node = _file_node_for_path(conn, s.path)
            _maybe_notify("stale", file_node, {
                "path": s.path,
                "version": s.last_ingested_version,
                "last_ingest_date": s.last_ingest_date,
                "fact_count": s.fact_count,
                "promoted_count": s.promoted_count,
                "_key": s.path,  # fallback when file_node lookup fails
            })

        for n in rpt.new:
            _maybe_notify("new", None, {
                "path": n.path,
                "filetype": n.filetype,
                "size_bytes": n.size_bytes,
                "mtime": n.mtime,
                "_key": n.path,
            })

        for m in rpt.missing:
            _maybe_notify("missing", m.file_node_uuid, {
                "path": m.path,
                "fact_count": m.fact_count,
                "promoted_count": m.promoted_count,
                "last_ingest_date": m.last_ingest_date,
            })

        for f in rpt.failed_extraction:
            _maybe_notify("failed_extraction", f.file_node_uuid, {
                "path": f.path,
                "failed_leaf_count": f.failed_leaf_count,
                "total_leaf_count": f.total_leaf_count,
                "last_error": f.last_error,
            })

        for r in rpt.rename_candidates:
            _maybe_notify("rename_candidate", r.missing_file_node_uuid, {
                "missing_path": r.missing_path,
                "new_path": r.new_path,
                "confidence": r.confidence,
                "content_sha256": r.content_sha256,
            })

        for d in rpt.drifted_promotions:
            _maybe_notify("drifted_promotion", None, {
                "marker_uuid": d.marker_uuid,
                "promoted_to": d.promoted_to,
                "source_path": d.source_path,
                "source_superseded_at": d.source_superseded_at,
                "_key": d.marker_uuid,
            })

    result.duration_ms = int((time.perf_counter() - t0) * 1000)
    return result


def _file_node_for_path(conn: sqlite3.Connection, path: str) -> Optional[str]:
    """Look up the current file_node uuid for an absolute path. None if
    not found or the row is superseded."""
    row = conn.execute(
        "SELECT uuid FROM file_nodes "
        "WHERE path_absolute = ? AND superseded_by IS NULL "
        "LIMIT 1",
        (path,),
    ).fetchone()
    return row["uuid"] if row else None


# ──────────────────────────────────────────────────────────────────────────────
# Long-running loop
# ──────────────────────────────────────────────────────────────────────────────
def watch_loop(
    *,
    directory: Optional[str] = None,
    corpus_id: Optional[str] = None,
    interval_seconds: float = 300.0,
    agent_id: str = "files_memory.watch",
    cooldown_seconds: float = 3600.0,
    max_cycles: Optional[int] = None,
    db_path: Optional[str] = None,
    notify_callable: Optional[Callable[[str, str, dict], Optional[str]]] = None,
) -> int:
    """Blocking polling loop. SIGINT-friendly: KeyboardInterrupt breaks out.

    Args:
        interval_seconds: time between cycles. Default 5 min.
        max_cycles: stop after this many cycles. None = unlimited.

    Returns the number of cycles completed.
    """
    cycles_done = 0
    logger.info(
        "files_memory.watch starting: directory=%s corpus=%s interval=%.1fs "
        "cooldown=%.0fs agent=%s",
        directory, corpus_id, interval_seconds, cooldown_seconds, agent_id,
    )
    try:
        while max_cycles is None or cycles_done < max_cycles:
            result = watch_once(
                directory=directory,
                corpus_id=corpus_id,
                agent_id=agent_id,
                cooldown_seconds=cooldown_seconds,
                notify_callable=notify_callable,
                db_path=db_path,
            )
            cycles_done += 1
            logger.info(
                "cycle %d: %dms  stale=%d new=%d missing=%d failed=%d "
                "rename=%d drifted=%d  notified=%d suppressed=%d  errors=%d",
                cycles_done, result.duration_ms,
                result.stale_count, result.new_count, result.missing_count,
                result.failed_extraction_count, result.rename_candidate_count,
                result.drifted_promotion_count,
                result.notifications_emitted,
                result.notifications_suppressed_by_cooldown,
                len(result.errors),
            )
            if max_cycles is not None and cycles_done >= max_cycles:
                break
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        logger.info("files_memory.watch interrupted; completed %d cycles", cycles_done)
    return cycles_done
