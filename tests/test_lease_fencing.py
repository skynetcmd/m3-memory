"""Leases, fencing tokens, and the sweeper.

A claim without an expiry is a claim an agent can hold forever by dying. A
lease fixes that, but only if a worker whose lease lapsed can no longer act on
the row -- otherwise reclaim just means TWO agents believe they own it. The
fence (a per-attempt lease_token) is what makes expiry real, and it must gate
complete, fail AND renew. The renew case is the subtle one: without it, a
straggler renews its way back into ownership of a row someone else now holds.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory.backends.sqlite_backend import SqliteDialect  # noqa: E402

D = SqliteDialect()
T = "notifications"


@pytest.fixture
def conn():
    path = os.path.join(tempfile.mkdtemp(), "lease.db")
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "CREATE TABLE notifications (id INTEGER PRIMARY KEY, agent_id TEXT, "
        "claimed_by TEXT, claimed_at TEXT, read_at TEXT, failed_at TEXT, "
        "lease_token TEXT, claim_expires_at TEXT, "
        "attempt_count INTEGER NOT NULL DEFAULT 0)"
    )
    db.executemany(
        "INSERT INTO notifications (id, agent_id) VALUES (?, 'worker')",
        [(i,) for i in range(1, 11)],
    )
    yield db
    db.close()


def _claim(conn, who="agent@s1", ttl=300):
    return D.claim_message(
        conn, table=T, where_sql="agent_id = ?", where_params=("worker",),
        claimant=who, lease_ttl=ttl,
    )


def _expire(conn, rid):
    """Age a live claim's deadline into the past.

    Deliberately NOT done by claiming with a negative TTL: claim_message now
    refuses that outright, because a non-positive TTL produced a NULL deadline
    on SQLite -- an immortal claim no sweeper would ever reclaim. Backdating the
    column is what actually happens in production anyway: time passes.
    """
    conn.execute(
        "UPDATE notifications SET claim_expires_at = "
        "strftime('%Y-%m-%dT%H:%M:%SZ','now','-60 seconds') WHERE id = ?",
        (rid,),
    )


def _row(conn, rid):
    return conn.execute("SELECT * FROM notifications WHERE id = ?", (rid,)).fetchone()


# ── the lease itself ─────────────────────────────────────────────────────────


def test_claim_returns_both_the_id_and_the_fence(conn):
    got = _claim(conn)
    assert got is not None
    rid, token = got
    assert rid and token, "a fence the caller cannot hold is not a fence"
    assert _row(conn, rid)["lease_token"] == token


def test_claim_sets_an_expiry_in_the_future_and_counts_the_attempt(conn):
    rid, _ = _claim(conn, ttl=300)
    row = _row(conn, rid)
    now = conn.execute(f"SELECT {D.now()}").fetchone()[0]
    assert row["claim_expires_at"] > now, "lease must expire in the FUTURE"
    assert row["attempt_count"] == 1


def test_the_lease_deadline_comes_from_the_database_clock(conn):
    """Same reason claimed_at does: two hosts, one authoritative clock."""
    rid, _ = _claim(conn, ttl=300)
    expires = _row(conn, rid)["claim_expires_at"]
    expected = conn.execute(f"SELECT {D.now_plus_seconds('?')}", (300,)).fetchone()[0]
    assert expires[:16] == expected[:16], (
        f"lease deadline {expires!r} does not track the DB clock {expected!r}"
    )
    assert expires.endswith("Z") and "+" not in expires


# ── the fence ────────────────────────────────────────────────────────────────


def test_complete_requires_the_token(conn):
    rid, token = _claim(conn)
    assert D.complete_message(conn, table=T, row_id=rid, lease_token="wrong") is False
    assert _row(conn, rid)["read_at"] is None, "a bad fence still completed the row"
    assert D.complete_message(conn, table=T, row_id=rid, lease_token=token) is True
    assert _row(conn, rid)["read_at"] is not None


def test_fail_requires_the_token(conn):
    rid, token = _claim(conn)
    assert D.fail_message(conn, table=T, row_id=rid, lease_token="wrong") is False
    assert _row(conn, rid)["failed_at"] is None
    assert D.fail_message(conn, table=T, row_id=rid, lease_token=token) is True
    assert _row(conn, rid)["failed_at"] is not None


def test_renew_requires_the_token(conn):
    """THE easy-to-miss one. Fencing complete and fail but not renew lets a
    straggler extend a lease it no longer owns."""
    rid, token = _claim(conn)
    assert D.renew_lease(conn, table=T, row_id=rid, lease_token="wrong") is False
    assert D.renew_lease(conn, table=T, row_id=rid, lease_token=token) is True


def test_renew_pushes_the_deadline_out(conn):
    rid, token = _claim(conn, ttl=1)
    before = _row(conn, rid)["claim_expires_at"]
    assert D.renew_lease(conn, table=T, row_id=rid, lease_token=token,
                         lease_ttl=600) is True
    assert _row(conn, rid)["claim_expires_at"] > before


def test_a_completed_row_cannot_be_completed_twice(conn):
    rid, token = _claim(conn)
    assert D.complete_message(conn, table=T, row_id=rid, lease_token=token) is True
    assert D.complete_message(conn, table=T, row_id=rid, lease_token=token) is False


# ── the sweeper ──────────────────────────────────────────────────────────────


def test_sweeper_reclaims_an_expired_lease_and_leaves_a_live_one(conn):
    dead_id, _ = _claim(conn, who="agent@dead")
    _expire(conn, dead_id)
    live_id, _ = _claim(conn, who="agent@live", ttl=300)

    reclaimed, dead_lettered = D.sweep_expired_leases(conn, table=T)

    assert (reclaimed, dead_lettered) == (1, 0)
    assert _row(conn, dead_id)["claimed_by"] is None, "expired lease not reclaimed"
    assert _row(conn, dead_id)["lease_token"] is None
    assert _row(conn, live_id)["claimed_by"] == "agent@live", (
        "the sweeper stole a LIVE claim -- this is the false-alarm failure the "
        "whole lease design exists to avoid"
    )


def test_a_reclaimed_worker_cannot_complete_fail_or_renew(conn):
    """The fence and the sweeper together, which is the actual guarantee."""
    rid, stale_token = _claim(conn, who="agent@slow")
    _expire(conn, rid)
    D.sweep_expired_leases(conn, table=T)

    assert D.complete_message(conn, table=T, row_id=rid,
                              lease_token=stale_token) is False
    assert D.fail_message(conn, table=T, row_id=rid,
                          lease_token=stale_token) is False
    assert D.renew_lease(conn, table=T, row_id=rid,
                         lease_token=stale_token) is False
    assert _row(conn, rid)["read_at"] is None
    assert _row(conn, rid)["failed_at"] is None


def test_a_reclaimed_row_can_be_claimed_by_someone_else(conn):
    rid, _ = _claim(conn, who="agent@dead")
    _expire(conn, rid)
    D.sweep_expired_leases(conn, table=T)
    again = _claim(conn, who="agent@fresh")
    assert again is not None
    assert again[0] == rid, "the reclaimed row did not go back to the queue"
    assert _row(conn, rid)["attempt_count"] == 2, "attempts must accumulate"


def test_an_expired_lease_cannot_be_renewed(conn):
    """Once the deadline passes the row belongs to the sweeper. Letting a
    straggler renew would make expiry advisory."""
    rid, token = _claim(conn)
    _expire(conn, rid)
    assert D.renew_lease(conn, table=T, row_id=rid, lease_token=token) is False


def test_dead_letters_after_max_attempts(conn):
    """A poison message must stop cycling. Without the bound, an infinite retry
    loop looks exactly like healthy queue activity."""
    rid = None
    for _ in range(5):
        got = _claim(conn, who="agent@crash")
        rid = got[0]
        _expire(conn, rid)
        D.sweep_expired_leases(conn, table=T, max_attempts=5)

    row = _row(conn, rid)
    assert row["attempt_count"] >= 5
    assert row["failed_at"] is not None, "poison message was never dead-lettered"
    assert row["claimed_by"] is None


def test_sweeper_is_idempotent(conn):
    rid, _ = _claim(conn)
    _expire(conn, rid)
    first = D.sweep_expired_leases(conn, table=T)
    second = D.sweep_expired_leases(conn, table=T)
    assert first == (1, 0)
    assert second == (0, 0), "a second sweep re-reclaimed an already-free row"


def test_a_non_positive_ttl_is_refused_loudly(conn):
    """A zero or negative TTL used to yield a NULL deadline on SQLite -- an
    immortal claim the sweeper could never reclaim, with no error at all."""
    for bad in (0, -5):
        with pytest.raises(ValueError, match="lease_ttl must be positive"):
            _claim(conn, ttl=bad)


def test_a_live_lease_is_never_null(conn):
    """The shape of the silent failure above: if this is ever NULL again, the
    row is immortal and nothing else in this file would notice."""
    rid, _ = _claim(conn, ttl=300)
    assert _row(conn, rid)["claim_expires_at"] is not None, (
        "NULL lease deadline -- this row can never be reclaimed"
    )
