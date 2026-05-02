"""Unit tests for bin/enrichment_state.py against migration 028.

Covers the state machine in isolation: enrollment idempotency,
content-hash supersede, claim + heartbeat recovery, dead-letter
promotion (deterministic + max-attempts paths), exponential backoff,
status counts, and end-of-run aggregation.

Tests open a fresh sqlite file per case, apply migration 028 directly
via executescript (the schema_versions table is not required for these
unit tests — we test the state module's behavior, not the runner).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
MIGRATIONS = [
    REPO_ROOT / "memory" / "migrations" / "028_enrichment_groups.up.sql",
    REPO_ROOT / "memory" / "migrations" / "029_enrichment_content_size.up.sql",
    REPO_ROOT / "memory" / "migrations" / "030_enrichment_partial_failure.up.sql",
]

if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import enrichment_state as estate


@pytest.fixture
def db_conn(tmp_path):
    """Sqlite connection with the minimal schema needed for the state-machine
    tables (migrations 028 + 029 applied directly)."""
    db_path = tmp_path / "state_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY, type TEXT, content TEXT,
            metadata_json TEXT, variant TEXT, is_deleted INTEGER DEFAULT 0
        );
    """)
    for m in MIGRATIONS:
        conn.executescript(m.read_text(encoding="utf-8"))
    yield conn
    conn.close()


# ── Hashing ─────────────────────────────────────────────────────────────


def test_content_hash_stable_across_reorder():
    turns_a = [
        ("t1", "hello", "user", 0, "2026-01-01", None),
        ("t2", "world", "assistant", 1, "2026-01-01", None),
    ]
    turns_b = list(reversed(turns_a))
    assert estate.compute_source_content_hash(turns_a) == estate.compute_source_content_hash(turns_b)


def test_content_hash_changes_when_content_changes():
    turns_a = [("t1", "hello", "user", 0, "2026-01-01", None)]
    turns_b = [("t1", "HELLO", "user", 0, "2026-01-01", None)]
    assert estate.compute_source_content_hash(turns_a) != estate.compute_source_content_hash(turns_b)


# ── Enrollment ──────────────────────────────────────────────────────────


def test_enroll_group_idempotent(db_conn):
    args = dict(
        source_variant="v1", target_variant="v1-out",
        group_key="conv-A", user_id="u1", db_path="/tmp/x",
        turn_count=5, source_content_hash="h1",
    )
    gid1, action1 = estate.enroll_group(db_conn, **args)
    db_conn.commit()
    assert action1 == "inserted"
    gid2, action2 = estate.enroll_group(db_conn, **args)
    assert action2 == "unchanged"
    assert gid1 == gid2


def test_enroll_does_not_wipe_status_on_backfill_placeholder_hash(db_conn):
    """Regression: backfill rows store a placeholder hash like
    'backfill::<variant>::<key>'. The first real run computing the actual
    SHA-256 must NOT supersede those rows (which would reset
    status='success' to 'pending' and lose all backfilled progress).
    """
    base = dict(
        source_variant="v1", target_variant="v1-out",
        group_key="conv-A", user_id="u1", db_path="/tmp/x",
        turn_count=0,
    )
    # Backfill seeds a placeholder hash + status='success'
    placeholder_hash = "backfill::v1::conv-A"
    gid, _ = estate.enroll_group(db_conn, source_content_hash=placeholder_hash, **base)
    db_conn.commit()
    db_conn.execute(
        "UPDATE enrichment_groups SET status='success', obs_emitted=7 WHERE id=?",
        (gid,),
    )
    db_conn.commit()
    # Real run computes a real hash.
    real_hash = "a" * 64
    gid2, action = estate.enroll_group(
        db_conn, source_content_hash=real_hash,
        **{**base, "turn_count": 12, "user_id": "u1"},
    )
    db_conn.commit()
    assert gid2 == gid
    # 'unchanged' because we updated the hash in-place without resetting status.
    assert action == "unchanged"
    row = db_conn.execute(
        "SELECT status, source_content_hash, obs_emitted, turn_count "
        "FROM enrichment_groups WHERE id=?",
        (gid,),
    ).fetchone()
    assert row[0] == "success"          # status preserved
    assert row[1] == real_hash          # hash upgraded
    assert row[2] == 7                  # obs_emitted preserved
    assert row[3] == 12                 # turn_count refreshed


def test_enroll_group_supersedes_on_content_drift(db_conn):
    base = dict(
        source_variant="v1", target_variant="v1-out",
        group_key="conv-A", user_id="u1", db_path="/tmp/x",
        turn_count=5,
    )
    gid_old, _ = estate.enroll_group(db_conn, source_content_hash="h1", **base)
    db_conn.commit()
    estate.mark_success(db_conn, gid_old, obs_emitted=3)
    gid_new, action = estate.enroll_group(db_conn, source_content_hash="h2", **base)
    db_conn.commit()
    assert action == "superseded"
    # In-place reset preserves the row id (avoids FK churn).
    assert gid_new == gid_old
    row = db_conn.execute(
        "SELECT status, source_content_hash, obs_emitted, attempts "
        "FROM enrichment_groups WHERE id=?",
        (gid_old,),
    ).fetchone()
    assert row == ("pending", "h2", 0, 0)


# ── Claim + recovery ────────────────────────────────────────────────────


def test_claim_group_atomic(db_conn):
    gid, _ = estate.enroll_group(
        db_conn, source_variant="v1", target_variant="v1-out",
        group_key="c", user_id="u", db_path="/", turn_count=1,
        source_content_hash="h",
    )
    db_conn.commit()
    token = estate.claim_group(db_conn, gid, enrich_run_id="run-1")
    assert token is not None
    # Second claim of same row must fail (status no longer pending).
    token2 = estate.claim_group(db_conn, gid, enrich_run_id="run-2")
    assert token2 is None


def test_claim_increments_attempts(db_conn):
    gid, _ = estate.enroll_group(
        db_conn, source_variant="v", target_variant="t",
        group_key="c", user_id="u", db_path="/", turn_count=1,
        source_content_hash="h",
    )
    db_conn.commit()
    estate.claim_group(db_conn, gid, enrich_run_id="r")
    estate.mark_failed(db_conn, gid, error_class="http_timeout", last_error="boom")
    estate.claim_group(db_conn, gid, enrich_run_id="r")
    row = db_conn.execute("SELECT attempts FROM enrichment_groups WHERE id=?", (gid,)).fetchone()
    assert row[0] == 2


def test_recover_stale_claims(db_conn):
    gid, _ = estate.enroll_group(
        db_conn, source_variant="v", target_variant="t",
        group_key="c", user_id="u", db_path="/", turn_count=1,
        source_content_hash="h",
    )
    db_conn.commit()
    estate.claim_group(db_conn, gid, enrich_run_id="r")
    # Backdate claimed_at to 2 hours ago.
    db_conn.execute(
        "UPDATE enrichment_groups SET claimed_at='2020-01-01T00:00:00Z' WHERE id=?",
        (gid,),
    )
    db_conn.commit()
    n = estate.recover_stale_claims(db_conn, timeout_sec=60)
    assert n == 1
    status = db_conn.execute("SELECT status, claim_token FROM enrichment_groups WHERE id=?", (gid,)).fetchone()
    assert status[0] == "pending"
    assert status[1] is None


# ── Terminal state transitions ──────────────────────────────────────────


def test_mark_success_clears_claim(db_conn):
    gid, _ = estate.enroll_group(
        db_conn, source_variant="v", target_variant="t",
        group_key="c", user_id="u", db_path="/", turn_count=1,
        source_content_hash="h",
    )
    db_conn.commit()
    estate.claim_group(db_conn, gid, enrich_run_id="r")
    estate.mark_success(db_conn, gid, obs_emitted=5, enrichment_ms=123,
                        tokens_in=100, tokens_out=50, cost_usd=0.0012)
    row = db_conn.execute(
        "SELECT status, obs_emitted, enrichment_ms, tokens_in, tokens_out, "
        "cost_usd, claim_token FROM enrichment_groups WHERE id=?",
        (gid,),
    ).fetchone()
    assert row[0] == "success"
    assert row[1] == 5
    assert row[2] == 123
    assert row[3] == 100
    assert row[5] == pytest.approx(0.0012)
    assert row[6] is None


def test_mark_failed_promotes_to_dead_letter_after_max_attempts(db_conn):
    gid, _ = estate.enroll_group(
        db_conn, source_variant="v", target_variant="t",
        group_key="c", user_id="u", db_path="/", turn_count=1,
        source_content_hash="h",
    )
    db_conn.commit()
    # 3 attempts → 3 transient failures → dead_letter on the 3rd
    estate.claim_group(db_conn, gid, enrich_run_id="r")
    s1 = estate.mark_failed(db_conn, gid, error_class="http_timeout", last_error="t1", max_attempts=3)
    assert s1 == "failed"
    estate.claim_group(db_conn, gid, enrich_run_id="r")
    s2 = estate.mark_failed(db_conn, gid, error_class="http_timeout", last_error="t2", max_attempts=3)
    assert s2 == "failed"
    estate.claim_group(db_conn, gid, enrich_run_id="r")
    s3 = estate.mark_failed(db_conn, gid, error_class="http_timeout", last_error="t3", max_attempts=3)
    assert s3 == "dead_letter"


def test_mark_failed_deterministic_error_immediately_dead_letters(db_conn):
    gid, _ = estate.enroll_group(
        db_conn, source_variant="v", target_variant="t",
        group_key="c", user_id="u", db_path="/", turn_count=1,
        source_content_hash="h",
    )
    db_conn.commit()
    estate.claim_group(db_conn, gid, enrich_run_id="r")
    s = estate.mark_failed(db_conn, gid, error_class="json_decode", last_error="bad json", max_attempts=3)
    assert s == "dead_letter"


def test_mark_failed_rate_limit_does_not_consume_attempts_budget(db_conn):
    """A daily-quota wall is upstream policy, not a per-group bug. 429-shaped
    failures must keep the group at status='failed' (retryable) even after
    attempts >= max_attempts, so a quota recovery picks them back up without
    needing --include-dead-letter."""
    gid, _ = estate.enroll_group(
        db_conn, source_variant="v", target_variant="t",
        group_key="c", user_id="u", db_path="/", turn_count=1,
        source_content_hash="h",
    )
    db_conn.commit()
    quota_msg = (
        "RuntimeError: observer http 429: You exceeded your current quota, "
        "please check your plan and billing details."
    )
    for _ in range(5):  # well past max_attempts=3
        estate.claim_group(db_conn, gid, enrich_run_id="r")
        s = estate.mark_failed(
            db_conn, gid,
            error_class="http_status", last_error=quota_msg, max_attempts=3,
        )
        assert s == "failed", f"rate-limit must not promote to dead_letter (got {s})"


def test_mark_failed_non_rate_limit_http_status_still_dead_letters(db_conn):
    """Non-429 http_status (e.g. 500 Internal Server Error) is a real
    per-group failure and must still consume the attempts budget."""
    gid, _ = estate.enroll_group(
        db_conn, source_variant="v", target_variant="t",
        group_key="c", user_id="u", db_path="/", turn_count=1,
        source_content_hash="h",
    )
    db_conn.commit()
    err = "HTTPStatusError: 500 Internal Server Error"
    estate.claim_group(db_conn, gid, enrich_run_id="r")
    s1 = estate.mark_failed(db_conn, gid, error_class="http_status", last_error=err, max_attempts=3)
    assert s1 == "failed"
    estate.claim_group(db_conn, gid, enrich_run_id="r")
    s2 = estate.mark_failed(db_conn, gid, error_class="http_status", last_error=err, max_attempts=3)
    assert s2 == "failed"
    estate.claim_group(db_conn, gid, enrich_run_id="r")
    s3 = estate.mark_failed(db_conn, gid, error_class="http_status", last_error=err, max_attempts=3)
    assert s3 == "dead_letter"


def test_mark_failed_sets_backoff(db_conn):
    gid, _ = estate.enroll_group(
        db_conn, source_variant="v", target_variant="t",
        group_key="c", user_id="u", db_path="/", turn_count=1,
        source_content_hash="h",
    )
    db_conn.commit()
    estate.claim_group(db_conn, gid, enrich_run_id="r")
    estate.mark_failed(db_conn, gid, error_class="http_timeout", last_error="x")
    next_at = db_conn.execute(
        "SELECT next_eligible_at FROM enrichment_groups WHERE id=?", (gid,)
    ).fetchone()[0]
    assert next_at is not None
    # In the future
    assert next_at > estate._utcnow_iso()


# ── Resume eligibility ──────────────────────────────────────────────────


def test_eligible_for_resume_excludes_success_and_dead_letter(db_conn):
    base = dict(source_variant="v", target_variant="t",
                user_id="u", db_path="/", turn_count=1,
                source_content_hash="h")
    g_pending, _ = estate.enroll_group(db_conn, group_key="c-pending", **base)
    g_success, _ = estate.enroll_group(db_conn, group_key="c-success", **base)
    g_dead, _ = estate.enroll_group(db_conn, group_key="c-dead", **base)
    g_failed, _ = estate.enroll_group(db_conn, group_key="c-failed", **base)
    db_conn.commit()
    estate.claim_group(db_conn, g_success, enrich_run_id="r")
    estate.mark_success(db_conn, g_success, obs_emitted=1)
    estate.claim_group(db_conn, g_dead, enrich_run_id="r")
    estate.mark_failed(db_conn, g_dead, error_class="json_decode", last_error="x")
    estate.claim_group(db_conn, g_failed, enrich_run_id="r")
    estate.mark_failed(db_conn, g_failed, error_class="http_timeout", last_error="y")
    # Backdate next_eligible_at so failed row IS eligible (default test runs fast)
    db_conn.execute(
        "UPDATE enrichment_groups SET next_eligible_at=NULL WHERE id=?",
        (g_failed,),
    )
    db_conn.commit()
    eligible = estate.eligible_for_resume(
        db_conn, source_variant="v", target_variant="t",
    )
    keys = {gkey for _gid, gkey, _uid in eligible}
    assert "c-pending" in keys
    assert "c-failed" in keys
    assert "c-success" not in keys
    assert "c-dead" not in keys

    # With include_dead_letter
    eligible2 = estate.eligible_for_resume(
        db_conn, source_variant="v", target_variant="t",
        include_dead_letter=True,
    )
    keys2 = {gkey for _gid, gkey, _uid in eligible2}
    assert "c-dead" in keys2


def test_eligible_for_resume_respects_backoff(db_conn):
    gid, _ = estate.enroll_group(
        db_conn, source_variant="v", target_variant="t",
        group_key="c", user_id="u", db_path="/", turn_count=1,
        source_content_hash="h",
    )
    db_conn.commit()
    estate.claim_group(db_conn, gid, enrich_run_id="r")
    estate.mark_failed(db_conn, gid, error_class="http_timeout", last_error="x")
    # Default backoff = 30s in the future → shouldn't be eligible yet.
    eligible = estate.eligible_for_resume(
        db_conn, source_variant="v", target_variant="t",
    )
    assert eligible == []
    # Backdate next_eligible_at to past.
    db_conn.execute(
        "UPDATE enrichment_groups SET next_eligible_at='2020-01-01T00:00:00Z' WHERE id=?",
        (gid,),
    )
    db_conn.commit()
    eligible = estate.eligible_for_resume(
        db_conn, source_variant="v", target_variant="t",
    )
    assert len(eligible) == 1


# ── Run record ──────────────────────────────────────────────────────────


def test_start_run_then_end_run_aggregates(db_conn):
    run_id = estate.start_run(
        db_conn,
        profile="enrich_local_qwen", model="qwen3-8b",
        source_variant="v", target_variant="t",
        db_path="/tmp/x", concurrency=4,
        launch_argv=["m3_enrich.py", "--track-state"],
    )
    # 3 success, 1 empty, 1 failed
    base = dict(source_variant="v", target_variant="t",
                user_id="u", db_path="/", turn_count=1,
                source_content_hash="h", enrich_run_id=run_id)
    for i in range(3):
        gid, _ = estate.enroll_group(db_conn, group_key=f"s{i}", **base)
        db_conn.commit()
        estate.claim_group(db_conn, gid, enrich_run_id=run_id)
        estate.mark_success(db_conn, gid, obs_emitted=2, cost_usd=0.001)
    gid, _ = estate.enroll_group(db_conn, group_key="e", **base)
    db_conn.commit()
    estate.claim_group(db_conn, gid, enrich_run_id=run_id)
    estate.mark_empty(db_conn, gid)
    gid, _ = estate.enroll_group(db_conn, group_key="f", **base)
    db_conn.commit()
    estate.claim_group(db_conn, gid, enrich_run_id=run_id)
    estate.mark_failed(db_conn, gid, error_class="http_timeout", last_error="x")

    estate.end_run(db_conn, run_id, status="completed")
    row = db_conn.execute(
        "SELECT n_success, n_empty, n_failed, n_dead_letter, "
        "total_cost_usd, status FROM enrichment_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    assert row[0] == 3       # success
    assert row[1] == 1       # empty
    assert row[2] == 1       # failed
    assert row[3] == 0       # dead_letter
    assert row[4] == pytest.approx(0.003)
    assert row[5] == "completed"


def test_run_total_cost_usd(db_conn):
    run_id = estate.start_run(
        db_conn, profile="p", model="m",
        source_variant="v", target_variant="t",
        db_path="/", concurrency=1,
    )
    base = dict(source_variant="v", target_variant="t",
                user_id="u", db_path="/", turn_count=1,
                source_content_hash="h", enrich_run_id=run_id)
    gid, _ = estate.enroll_group(db_conn, group_key="a", **base)
    db_conn.commit()
    estate.claim_group(db_conn, gid, enrich_run_id=run_id)
    estate.mark_success(db_conn, gid, obs_emitted=1, cost_usd=0.5)
    gid, _ = estate.enroll_group(db_conn, group_key="b", **base)
    db_conn.commit()
    estate.claim_group(db_conn, gid, enrich_run_id=run_id)
    estate.mark_success(db_conn, gid, obs_emitted=1, cost_usd=0.25)
    assert estate.run_total_cost_usd(db_conn, run_id) == pytest.approx(0.75)


# ── Bulk enrollment ─────────────────────────────────────────────────────


def test_enroll_groups_bulk_counts(db_conn):
    # Pre-enroll one to test the unchanged path.
    estate.enroll_group(
        db_conn, source_variant="v", target_variant="t",
        group_key="x", user_id="u", db_path="/", turn_count=1,
        source_content_hash="h-x",
    )
    db_conn.commit()
    inputs = [
        {"group_key": "x", "user_id": "u", "turn_count": 1, "source_content_hash": "h-x"},  # unchanged
        {"group_key": "y", "user_id": "u", "turn_count": 1, "source_content_hash": "h-y"},  # inserted
        {"group_key": "z", "user_id": "u", "turn_count": 1, "source_content_hash": "h-z"},  # inserted
    ]
    counts = estate.enroll_groups_bulk(
        db_conn, inputs,
        source_variant="v", target_variant="t", db_path="/",
    )
    assert counts == {"inserted": 2, "unchanged": 1, "superseded": 0}


# ── Status counts ───────────────────────────────────────────────────────


def test_compute_content_size_k():
    # Empty
    assert estate.compute_content_size_k([]) == 1
    # Single small turn
    turns = [("t1", "x" * 100, "user", 0, "now", None)]
    assert estate.compute_content_size_k(turns) == 1
    # Multiple turns under 1 KB each, total > 1 KB
    turns = [("t" + str(i), "y" * 600, "user", i, "now", None) for i in range(3)]
    # 3 * 600 = 1800 bytes -> 2 KB rounded up
    assert estate.compute_content_size_k(turns) == 2
    # Big single turn
    turns = [("t1", "z" * 5000, "user", 0, "now", None)]
    # 5000 / 1024 = 4.88 -> 5 KB
    assert estate.compute_content_size_k(turns) == 5


def test_enroll_records_content_size_k(db_conn):
    gid, _ = estate.enroll_group(
        db_conn, source_variant="v", target_variant="t",
        group_key="g", user_id="u", db_path="/", turn_count=3,
        source_content_hash="h", content_size_k=7,
    )
    db_conn.commit()
    row = db_conn.execute(
        "SELECT content_size_k FROM enrichment_groups WHERE id=?", (gid,)
    ).fetchone()
    assert row[0] == 7


def test_eligible_for_resume_size_filters(db_conn):
    base = dict(source_variant="v", target_variant="t",
                user_id="u", db_path="/", turn_count=1,
                source_content_hash="h")
    estate.enroll_group(db_conn, group_key="small", content_size_k=1, **base)
    estate.enroll_group(db_conn, group_key="mid",   content_size_k=4, **base)
    estate.enroll_group(db_conn, group_key="big",   content_size_k=10, **base)
    estate.enroll_group(db_conn, group_key="huge",  content_size_k=50, **base)
    estate.enroll_group(db_conn, group_key="legacy", content_size_k=None, **{**base, "source_content_hash": "h2"})
    db_conn.commit()

    # No bounds → everything eligible (5 rows including legacy)
    e = estate.eligible_for_resume(db_conn, source_variant="v", target_variant="t")
    assert {gkey for _gid, gkey, _uid in e} == {"small", "mid", "big", "huge", "legacy"}

    # max=4 → keeps small + mid; excludes legacy (NULL)
    e = estate.eligible_for_resume(
        db_conn, source_variant="v", target_variant="t", max_size_k=4,
    )
    assert {gkey for _gid, gkey, _uid in e} == {"small", "mid"}

    # min=10 → big + huge
    e = estate.eligible_for_resume(
        db_conn, source_variant="v", target_variant="t", min_size_k=10,
    )
    assert {gkey for _gid, gkey, _uid in e} == {"big", "huge"}

    # min=4, max=10 → mid + big
    e = estate.eligible_for_resume(
        db_conn, source_variant="v", target_variant="t",
        min_size_k=4, max_size_k=10,
    )
    assert {gkey for _gid, gkey, _uid in e} == {"mid", "big"}


def test_status_counts(db_conn):
    base = dict(source_variant="v", target_variant="t",
                user_id="u", db_path="/", turn_count=1,
                source_content_hash="h")
    for k in ("a", "b", "c"):
        gid, _ = estate.enroll_group(db_conn, group_key=k, **base)
        db_conn.commit()
        if k == "b":
            estate.claim_group(db_conn, gid, enrich_run_id="r")
            estate.mark_success(db_conn, gid, obs_emitted=1)
    counts = estate.status_counts(db_conn, source_variant="v", target_variant="t")
    assert counts.get("pending", 0) == 2
    assert counts.get("success", 0) == 1
