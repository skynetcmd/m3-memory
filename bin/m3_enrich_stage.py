#!/usr/bin/env python3
"""Shared queue-plumbing primitives for multi-stage enrichment.

Phase E5 forward-compat module. Both `bin/run_observer.py` and
`bin/run_reflector.py` currently implement the same pop/ack/fail
pattern against their own queue tables (observation_queue,
reflector_queue). Migration 026 added a `stage` column to both so
they can be unified later without another schema change.

When a future stage (entity_consolidator, timeline_validator, ...)
arrives, it should:

  1. Pick a stage name (lowercase, snake_case).
  2. Use `pop_batch(...)` to claim work, filtered by `stage`.
  3. Call `ack(...)` on success or `fail(...)` on exception.

The existing observer/reflector drainers keep their inline SQL for
now — they predate this module and rewriting them adds no value
until we actually have a third stage. When stage 3 lands and we
collapse the queues into one (Option B in memory 9f5033b8), the
inline blocks fold into this module's helpers in one diff.

Public API:
    pop_batch(table, stage, limit, db) -> list[QueueRow]
    ack(table, queue_id, db) -> None
    fail(table, queue_id, error_message, db) -> None
    queue_depth(table, stage, db) -> int

`db` is an open sqlite3.Connection; callers manage their own
connection lifecycle (matches how memory_core._db() is used today).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

# Stage name registry. Adding a new stage = add a constant here so
# greps find every call site.
STAGE_OBSERVER = "observer"
STAGE_REFLECTOR = "reflector"

KNOWN_STAGES: frozenset[str] = frozenset({
    STAGE_OBSERVER,
    STAGE_REFLECTOR,
})

# Queue tables. After Option B these collapse to a single
# `enrichment_queue` table; until then this module accepts both.
QUEUE_TABLES: frozenset[str] = frozenset({
    "observation_queue",
    "reflector_queue",
})

# Hard cap on attempts before a row is considered poisoned. Mirrors
# the inline `attempts < 5` filter in the existing drainers.
MAX_ATTEMPTS: int = 5


@dataclass(frozen=True)
class QueueRow:
    queue_id: int
    conversation_id: str
    user_id: str
    attempts: int
    stage: str


def _validate_table(table: str) -> None:
    if table not in QUEUE_TABLES:
        raise ValueError(
            f"unknown queue table {table!r}; expected one of {sorted(QUEUE_TABLES)}"
        )


def pop_batch(
    table: str,
    stage: str,
    limit: int,
    db: sqlite3.Connection,
) -> list[QueueRow]:
    """Claim up to `limit` rows from `table` for the given `stage`.

    Ordered by (attempts ASC, enqueued_at ASC) so retries of failed
    rows are interleaved with fresh work. This is *not* a locking
    pop — concurrent drainers will race. SQLite's per-process write
    lock plus the row-level DELETE-on-ack means the worst case is
    duplicate work, not data loss. Add SELECT...FOR UPDATE semantics
    if/when we move to a multi-process drainer.

    Migration 026 added the `stage` column with a backfilled default,
    so this is safe to call against pre-026 schemas after migrate-up.
    """
    _validate_table(table)
    rows = db.execute(
        f"""
        SELECT id, conversation_id, COALESCE(user_id, ''), attempts, stage
        FROM {table}
        WHERE attempts < ? AND stage = ?
        ORDER BY attempts ASC, enqueued_at ASC
        LIMIT ?
        """,
        (MAX_ATTEMPTS, stage, limit),
    ).fetchall()
    return [
        QueueRow(
            queue_id=r[0],
            conversation_id=r[1],
            user_id=r[2],
            attempts=r[3],
            stage=r[4],
        )
        for r in rows
    ]


def ack(table: str, queue_id: int, db: sqlite3.Connection) -> None:
    """Mark a row complete by deleting it.

    No tombstone is kept; the downstream type='observation' /
    supersedes-edge rows in memory_items are the durable record.
    """
    _validate_table(table)
    db.execute(f"DELETE FROM {table} WHERE id = ?", (queue_id,))
    db.commit()


def fail(
    table: str,
    queue_id: int,
    error_message: str,
    db: sqlite3.Connection,
) -> None:
    """Increment attempts + record last_error. Row stays in queue
    until attempts hits MAX_ATTEMPTS, after which pop_batch skips it."""
    _validate_table(table)
    db.execute(
        f"""
        UPDATE {table}
        SET attempts = attempts + 1,
            last_error = ?,
            last_attempt_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
        WHERE id = ?
        """,
        (str(error_message)[:500], queue_id),
    )
    db.commit()


def queue_depth(
    table: str,
    stage: str,
    db: sqlite3.Connection,
) -> int:
    """Count rows still eligible for draining (attempts under cap)."""
    _validate_table(table)
    row = db.execute(
        f"SELECT COUNT(*) FROM {table} WHERE attempts < ? AND stage = ?",
        (MAX_ATTEMPTS, stage),
    ).fetchone()
    return int(row[0]) if row else 0


def known_stages() -> Iterable[str]:
    return tuple(sorted(KNOWN_STAGES))
