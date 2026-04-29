"""Durable per-group enrichment state for m3_enrich.

Backs migration 028's enrichment_groups + enrichment_runs tables. Pure helper
module — m3_enrich.py imports from here; no reverse dependency. Designed to
be unit-testable in isolation against a fresh sqlite file.

State machine:
    pending ──claim──▶ in_progress ──┬── success (obs_emitted > 0)
                                      ├── empty   (extractor OK, 0 obs)
                                      ├── failed  (transient — eligible for retry)
                                      └── dead_letter (deterministic OR attempts>=N)

    stale claim (claimed_at older than CLAIM_TIMEOUT_SEC) ──▶ pending  (auto on resume)
    source_content_hash changed                              ──▶ superseded (old row)

All callers should hold a per-DB sqlite3.Connection in WAL mode. The module
neither opens nor closes connections — that's the caller's responsibility,
matching the rest of bin/.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence


# ── Tunables ───────────────────────────────────────────────────────────────
DEFAULT_MAX_ATTEMPTS = 3
CLAIM_TIMEOUT_SEC = 3600          # in_progress → pending if claimed_at older
BACKOFF_BASE_SEC = 30             # exp backoff: 30, 60, 120, ...

# Errors that won't change on retry — straight to dead_letter on first failure.
DETERMINISTIC_ERROR_CLASSES = frozenset({
    "json_decode",
    "tokenizer_error",
    "content_too_large",
    "schema_violation",
})


# ── Time helpers ───────────────────────────────────────────────────────────
def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_plus(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ── Hashing ────────────────────────────────────────────────────────────────
def compute_source_content_hash(turns: Sequence[tuple]) -> str:
    """SHA-256 over the concatenated turn IDs + content.

    Detects when a group's source content has changed since its last
    enrichment so we can supersede the stale row.

    `turns` is the same shape m3_enrich._query_eligible_groups produces:
        (id, content, role, turn_index, created_at, metadata_json)
    Stable across reorderings: we sort by turn_index then id.
    """
    h = hashlib.sha256()
    for turn in sorted(turns, key=lambda t: (t[3] if t[3] is not None else 0, t[0])):
        h.update(str(turn[0]).encode("utf-8"))
        h.update(b"\x00")
        h.update((turn[1] or "").encode("utf-8"))
        h.update(b"\x01")
    return h.hexdigest()


# ── Schema verification ────────────────────────────────────────────────────
def has_state_tables(conn: sqlite3.Connection) -> bool:
    """True iff migration 028 has been applied to this DB."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('enrichment_groups','enrichment_runs')"
    ).fetchall()
    return len(rows) == 2


# ── enrichment_runs ────────────────────────────────────────────────────────
def _git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def start_run(
    conn: sqlite3.Connection,
    *,
    profile: str,
    model: str,
    source_variant: Optional[str],
    target_variant: str,
    db_path: str,
    concurrency: int,
    launch_argv: Optional[list[str]] = None,
) -> str:
    """Insert a new enrichment_runs row at status='running'. Returns run_id."""
    run_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO enrichment_runs (
            id, started_at, profile, model, source_variant, target_variant,
            db_path, concurrency, launch_argv, host, git_sha, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
        """,
        (
            run_id, _utcnow_iso(), profile, model, source_variant, target_variant,
            db_path, concurrency,
            json.dumps(launch_argv if launch_argv is not None else sys.argv),
            socket.gethostname(),
            _git_sha(),
        ),
    )
    conn.commit()
    return run_id


def end_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    abort_reason: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    """Update enrichment_runs row with final counts + status."""
    counts = conn.execute(
        """
        SELECT status, COUNT(*), COALESCE(SUM(cost_usd), 0)
        FROM enrichment_groups
        WHERE enrich_run_id = ?
        GROUP BY status
        """,
        (run_id,),
    ).fetchall()
    n = {"pending": 0, "success": 0, "failed": 0, "empty": 0, "dead_letter": 0}
    total_cost = 0.0
    for st, cnt, cost in counts:
        if st in n:
            n[st] = cnt
        total_cost += cost or 0.0
    conn.execute(
        """
        UPDATE enrichment_runs
        SET finished_at = ?, status = ?,
            n_pending = ?, n_success = ?, n_failed = ?,
            n_empty = ?, n_dead_letter = ?,
            total_cost_usd = ?, abort_reason = ?, notes = ?
        WHERE id = ?
        """,
        (
            _utcnow_iso(), status,
            n["pending"], n["success"], n["failed"], n["empty"], n["dead_letter"],
            total_cost, abort_reason, notes, run_id,
        ),
    )
    conn.commit()


def run_total_cost_usd(conn: sqlite3.Connection, run_id: str) -> float:
    """Sum cost_usd across all groups linked to a run. Used for budget checks."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM enrichment_groups "
        "WHERE enrich_run_id = ?",
        (run_id,),
    ).fetchone()
    return float(row[0] or 0.0)


# ── enrichment_groups: enrollment ──────────────────────────────────────────
def enroll_group(
    conn: sqlite3.Connection,
    *,
    source_variant: str,
    target_variant: str,
    group_key: str,
    user_id: str,
    db_path: str,
    turn_count: int,
    source_content_hash: str,
    profile: Optional[str] = None,
    model: Optional[str] = None,
    enrich_run_id: Optional[str] = None,
) -> tuple[int, str]:
    """Idempotent enroll. Returns (group_id, action) where action is one of
    'inserted' (new row) | 'unchanged' (existing match) | 'superseded' (old
    row content changed; new pending row inserted).

    Caller is responsible for `commit()` — we keep this fast for batch enroll.
    """
    cur = conn.execute(
        """
        SELECT id, source_content_hash, status
        FROM enrichment_groups
        WHERE source_variant = ? AND target_variant = ? AND group_key = ?
        """,
        (source_variant, target_variant, group_key),
    )
    existing = cur.fetchone()
    if existing:
        old_id, old_hash, old_status = existing
        if old_hash == source_content_hash:
            return (old_id, "unchanged")
        # Backfill rows store a placeholder hash ("backfill::" or
        # "backfill-pending::") because the script doesn't have access to
        # the full source content. The first real run that touches such a
        # row should UPDATE the hash without resetting status — otherwise
        # we wipe legitimate prior progress (success/empty/dead_letter).
        is_placeholder = (old_hash or "").startswith("backfill::") or \
                         (old_hash or "").startswith("backfill-pending::")
        if is_placeholder:
            conn.execute(
                """
                UPDATE enrichment_groups
                SET source_content_hash=?, turn_count=?,
                    profile=COALESCE(?, profile),
                    model=COALESCE(?, model),
                    enrich_run_id=COALESCE(?, enrich_run_id)
                WHERE id = ?
                """,
                (source_content_hash, turn_count, profile, model, enrich_run_id, old_id),
            )
            return (old_id, "unchanged")
        # Real hash mismatch = source content actually drifted. Reset the
        # row to pending with the new hash. Updating in-place keeps the
        # UNIQUE constraint happy and preserves the row id for FK refs
        # (memory_items.source_group_id). Old observations from the prior
        # hash remain in memory_items but become orphaned from run-level
        # state — Reflector or a separate cleanup pass owns that gardening.
        conn.execute(
            """
            UPDATE enrichment_groups
            SET status='pending', source_content_hash=?, turn_count=?,
                obs_emitted=0, attempts=0, last_error=NULL, error_class=NULL,
                enrichment_ms=NULL, tokens_in=NULL, tokens_out=NULL, cost_usd=NULL,
                claim_token=NULL, claimed_at=NULL, next_eligible_at=NULL,
                first_attempt_at=NULL, last_attempt_at=NULL,
                profile=?, model=?, enrich_run_id=?
            WHERE id = ?
            """,
            (source_content_hash, turn_count, profile, model, enrich_run_id, old_id),
        )
        return (old_id, "superseded")
    cur = conn.execute(
        """
        INSERT INTO enrichment_groups (
            source_variant, target_variant, group_key, user_id, db_path,
            turn_count, source_content_hash, profile, model, enrich_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_variant, target_variant, group_key, user_id, db_path,
            turn_count, source_content_hash, profile, model, enrich_run_id,
        ),
    )
    return (cur.lastrowid, "inserted")


def enroll_groups_bulk(
    conn: sqlite3.Connection,
    groups: Iterable[dict],
    *,
    source_variant: str,
    target_variant: str,
    db_path: str,
    profile: Optional[str] = None,
    model: Optional[str] = None,
    enrich_run_id: Optional[str] = None,
) -> dict[str, int]:
    """Batch wrapper. Each group dict needs: group_key, user_id, turn_count,
    source_content_hash. Returns {'inserted', 'unchanged', 'superseded'} counts.
    Single transaction for performance on 200K+ groups."""
    counts = {"inserted": 0, "unchanged": 0, "superseded": 0}
    for g in groups:
        _, action = enroll_group(
            conn,
            source_variant=source_variant,
            target_variant=target_variant,
            group_key=g["group_key"],
            user_id=g["user_id"],
            db_path=db_path,
            turn_count=g["turn_count"],
            source_content_hash=g["source_content_hash"],
            profile=profile,
            model=model,
            enrich_run_id=enrich_run_id,
        )
        counts[action] += 1
    conn.commit()
    return counts


# ── enrichment_groups: claim + recovery ────────────────────────────────────
def recover_stale_claims(
    conn: sqlite3.Connection,
    *,
    timeout_sec: int = CLAIM_TIMEOUT_SEC,
) -> int:
    """Reset in_progress rows that haven't been touched in `timeout_sec` back
    to pending. Run at every resume to self-heal from crashed workers.
    Returns count of recovered rows."""
    cutoff = _iso_plus(-timeout_sec)
    cur = conn.execute(
        """
        UPDATE enrichment_groups
        SET status='pending', claim_token=NULL, claimed_at=NULL
        WHERE status='in_progress' AND (claimed_at IS NULL OR claimed_at < ?)
        """,
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


def claim_group(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    enrich_run_id: str,
) -> Optional[str]:
    """Atomically claim a group eligible for processing. Returns claim_token
    on success, None if the row is no longer claimable (terminal state or
    raced).

    Eligible source states: 'pending' (never tried) or 'failed' (transient
    failure with retries left). Terminal states ('success'/'empty'/'dead_letter'/
    'superseded') return None — the resume picker is responsible for not
    handing those off.
    """
    token = str(uuid.uuid4())
    now = _utcnow_iso()
    cur = conn.execute(
        """
        UPDATE enrichment_groups
        SET status='in_progress', claim_token=?, claimed_at=?,
            attempts = attempts + 1,
            first_attempt_at = COALESCE(first_attempt_at, ?),
            last_attempt_at = ?,
            enrich_run_id = ?
        WHERE id = ? AND status IN ('pending','failed')
        """,
        (token, now, now, now, enrich_run_id, group_id),
    )
    conn.commit()
    return token if cur.rowcount == 1 else None


# ── enrichment_groups: terminal updates ────────────────────────────────────
def mark_success(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    obs_emitted: int,
    enrichment_ms: Optional[int] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    cost_usd: Optional[float] = None,
) -> None:
    conn.execute(
        """
        UPDATE enrichment_groups
        SET status='success', obs_emitted=?, enrichment_ms=?,
            tokens_in=?, tokens_out=?, cost_usd=?,
            claim_token=NULL, claimed_at=NULL, last_error=NULL, error_class=NULL
        WHERE id = ?
        """,
        (obs_emitted, enrichment_ms, tokens_in, tokens_out, cost_usd, group_id),
    )
    conn.commit()


def mark_empty(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    enrichment_ms: Optional[int] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    cost_usd: Optional[float] = None,
) -> None:
    conn.execute(
        """
        UPDATE enrichment_groups
        SET status='empty', obs_emitted=0, enrichment_ms=?,
            tokens_in=?, tokens_out=?, cost_usd=?,
            claim_token=NULL, claimed_at=NULL, last_error=NULL, error_class=NULL
        WHERE id = ?
        """,
        (enrichment_ms, tokens_in, tokens_out, cost_usd, group_id),
    )
    conn.commit()


def mark_failed(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    error_class: str,
    last_error: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    enrichment_ms: Optional[int] = None,
) -> str:
    """Record a failure. Promotes to dead_letter if (a) the error class is
    deterministic or (b) attempts have hit max_attempts. Otherwise leaves
    the row at status='failed' with next_eligible_at set per exponential
    backoff. Returns the resulting status."""
    row = conn.execute(
        "SELECT attempts FROM enrichment_groups WHERE id = ?",
        (group_id,),
    ).fetchone()
    attempts = (row[0] if row else 0) or 0
    truncated = (last_error or "")[:1000]
    if error_class in DETERMINISTIC_ERROR_CLASSES or attempts >= max_attempts:
        new_status = "dead_letter"
        next_eligible_at: Optional[str] = None
    else:
        new_status = "failed"
        # Exponential backoff: 30s, 60s, 120s ...
        backoff_sec = BACKOFF_BASE_SEC * (2 ** max(0, attempts - 1))
        next_eligible_at = _iso_plus(backoff_sec)
    conn.execute(
        """
        UPDATE enrichment_groups
        SET status=?, error_class=?, last_error=?,
            next_eligible_at=?, enrichment_ms=?,
            claim_token=NULL, claimed_at=NULL
        WHERE id = ?
        """,
        (new_status, error_class, truncated, next_eligible_at, enrichment_ms, group_id),
    )
    conn.commit()
    return new_status


# ── enrichment_groups: query for resume ────────────────────────────────────
def eligible_for_resume(
    conn: sqlite3.Connection,
    *,
    source_variant: str,
    target_variant: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    include_dead_letter: bool = False,
    limit: Optional[int] = None,
) -> list[tuple[int, str, str]]:
    """Return [(id, group_key, user_id)] for groups that resume should pick up.

    Picks pending OR failed-with-retries-left (and optionally dead_letter).
    Honors next_eligible_at backoff. Caller filters further as needed.
    """
    statuses = ["pending", "failed"]
    if include_dead_letter:
        statuses.append("dead_letter")
    placeholders = ",".join("?" * len(statuses))
    sql = f"""
        SELECT id, group_key, user_id
        FROM enrichment_groups
        WHERE source_variant = ? AND target_variant = ?
          AND status IN ({placeholders})
          AND attempts < ?
          AND (next_eligible_at IS NULL OR next_eligible_at <= ?)
        ORDER BY attempts ASC, turn_count DESC
    """
    params: list = [source_variant, target_variant, *statuses, max_attempts, _utcnow_iso()]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def status_counts(
    conn: sqlite3.Connection,
    *,
    source_variant: Optional[str] = None,
    target_variant: Optional[str] = None,
) -> dict[str, int]:
    """Per-status row counts. Useful for status-report CLI."""
    where = "1=1"
    params: list = []
    if source_variant is not None:
        where += " AND source_variant = ?"
        params.append(source_variant)
    if target_variant is not None:
        where += " AND target_variant = ?"
        params.append(target_variant)
    rows = conn.execute(
        f"SELECT status, COUNT(*) FROM enrichment_groups WHERE {where} GROUP BY status",
        params,
    ).fetchall()
    return {r[0]: r[1] for r in rows}
