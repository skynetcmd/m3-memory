"""Atomic work-stealing: exactly one sister wins a given row.

A bare TYPE queue is a FAN-OUT read, so every sister session sees the same
rows. Correct for a broadcast, wrong for work: two sisters both pick up the
same item and do it twice. `read_at` records THAT a row was consumed, not by
WHICH instance, and `received_at` is transport receipt -- neither arbitrates.

The concurrency tests below use PROCESSES, not threads. Threads share one
interpreter and can pass while the real deployment (N agent sessions, N OS
processes, one database file) still races.
"""
from __future__ import annotations

import collections
import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from multiprocessing import Pool

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory.backends.sqlite_backend import SqliteDialect  # noqa: E402

_NOW = "2026-09-14T00:00:00Z"


def _make_db(path: str, rows: int = 200) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE notifications (id INTEGER PRIMARY KEY, agent_id TEXT, "
        "claimed_by TEXT, claimed_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO notifications (id, agent_id, claimed_by, claimed_at) "
        "VALUES (?, 'worker', NULL, NULL)",
        [(i,) for i in range(1, rows + 1)],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def db_path():
    path = os.path.join(tempfile.mkdtemp(), "claim.db")
    _make_db(path)
    yield path


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _claim(conn, claimant: str):
    return SqliteDialect().claim_message(
        conn, table="notifications", where_sql="agent_id = ?",
        where_params=("worker",), claimant=claimant, now=_NOW,
    )


def test_claim_returns_a_row_and_stamps_the_claimant(db_path):
    conn = _open(db_path)
    got = _claim(conn, "claude-code@s1")
    assert got is not None

    row = conn.execute(
        "SELECT claimed_by, claimed_at FROM notifications WHERE id = ?", (got,)
    ).fetchone()
    assert row[0] == "claude-code@s1", (
        f"the caller's real id must replace the internal token, got {row[0]!r}"
    )
    assert row[1] == _NOW
    conn.close()


def test_no_token_survives_the_claim(db_path):
    """The token is an implementation detail and must never be left in the DB
    for a later reader to mistake for an agent id."""
    conn = _open(db_path)
    _claim(conn, "claude-code@s1")
    leaked = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE claimed_by LIKE '%#%'"
    ).fetchone()[0]
    assert leaked == 0, "an internal claim token was left in claimed_by"
    conn.close()


def test_a_claimed_row_is_not_reclaimed(db_path):
    conn = _open(db_path)
    first = _claim(conn, "claude-code@s1")
    second = _claim(conn, "claude-code@s2")
    assert first != second, "the same row was handed to two claimants"
    conn.close()


def test_empty_queue_returns_none_rather_than_raising(db_path):
    """Exhaustion is a normal outcome, not an error."""
    conn = _open(db_path)
    for _ in range(200):
        assert _claim(conn, "claude-code@s1") is not None
    assert _claim(conn, "claude-code@s1") is None
    conn.close()


def test_the_addressing_predicate_is_honoured(db_path):
    """A claim must not reach across to another addressee's mail."""
    conn = _open(db_path)
    conn.execute(
        "INSERT INTO notifications (id, agent_id, claimed_by) "
        "VALUES (9999, 'someone-else', NULL)"
    )
    got = SqliteDialect().claim_message(
        conn, table="notifications", where_sql="agent_id = ?",
        where_params=("someone-else",), claimant="claude-code@s1", now=_NOW,
    )
    assert got == 9999
    # And the reverse: claiming 'worker' never returns the other row.
    for _ in range(200):
        assert _claim(conn, "claude-code@s1") != 9999
    conn.close()


def _worker(args):
    path, name, n = args
    conn = _open(path)
    got, errs = [], []
    for _ in range(n):
        try:
            row = SqliteDialect().claim_message(
                conn, table="notifications", where_sql="agent_id = ?",
                where_params=("worker",), claimant=name, now=_NOW,
            )
            if row is not None:
                got.append(row)
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))
    conn.close()
    return got, errs


def test_eight_processes_never_double_claim():
    """THE guarantee. Without it two sisters do the same work twice."""
    path = os.path.join(tempfile.mkdtemp(), "claim_mp.db")
    _make_db(path, rows=200)

    with Pool(8) as pool:
        results = pool.map(_worker, [(path, f"agent@p{i}", 100) for i in range(8)])

    seen = collections.Counter()
    errors = []
    for got, errs in results:
        seen.update(got)
        errors.extend(errs)

    doubled = {k: v for k, v in seen.items() if v > 1}
    assert not errors, f"claims raised: {errors[:3]}"
    assert not doubled, f"rows claimed more than once: {list(doubled)[:5]}"
    assert len(seen) == 200, f"claimed {len(seen)} of 200 rows"


def test_every_row_ends_up_owned_by_exactly_one_agent():
    """The DB's own view must agree with what the claimers were told.

    Deliberately does NOT assert that all six processes won something. That
    was the first version of this test and it failed (got 2 of 6): a fast
    starter can drain the queue before its siblings open their connection.
    Distinct-owner count measures the OS scheduler, not the claim primitive,
    and a test that fails on a quiet machine is a false alarm
    (DESIGN_PHILOSOPHIES 3). What must hold is that every row is owned, by a
    real agent id, exactly once.
    """
    path = os.path.join(tempfile.mkdtemp(), "claim_own.db")
    _make_db(path, rows=120)

    with Pool(6) as pool:
        results = pool.map(_worker, [(path, f"agent@p{i}", 60) for i in range(6)])

    handed_out = [row for got, _ in results for row in got]
    assert len(handed_out) == len(set(handed_out)), "a row was handed to two agents"

    conn = _open(path)
    unclaimed = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE claimed_by IS NULL"
    ).fetchone()[0]
    owners = [r[0] for r in conn.execute(
        "SELECT DISTINCT claimed_by FROM notifications"
    ).fetchall()]
    conn.close()

    assert unclaimed == 0, f"{unclaimed} rows left unclaimed"
    assert len(handed_out) == 120, f"claimers were told about {len(handed_out)} rows"
    assert all(o and o.startswith("agent@p") for o in owners), (
        f"a row is owned by something that is not an agent id: {owners}"
    )


# ── The lock-acquisition guard ───────────────────────────────────────────────


def _upgrade_worker(args):
    """Claim with a READ first, so a deferred txn must UPGRADE to a writer.

    This is the shape the plain claim path does NOT have, and it is the only
    one that can distinguish BEGIN IMMEDIATE from a deferred BEGIN. Without it
    a counter-test that weakens the guard still passes -- verified: it did.
    """
    path, name, mode, n = args
    conn = _open(path)
    errs = []
    for _ in range(n):
        token = f"{name}#{uuid.uuid4()}"
        try:
            conn.execute(f"BEGIN {mode}".strip())
            conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE claimed_by IS NULL"
            ).fetchone()
            conn.execute(
                "UPDATE notifications SET claimed_by = ? WHERE id = ("
                "SELECT id FROM notifications WHERE claimed_by IS NULL "
                "ORDER BY id LIMIT 1)", (token,),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            errs.append(type(exc).__name__)
    conn.close()
    return errs


def test_begin_immediate_survives_the_upgrade_shape():
    """IMMEDIATE must not raise when the txn starts as a reader.

    A deferred BEGIN starts as a READ transaction and must UPGRADE to a writer
    on the UPDATE; a collision there is SQLITE_BUSY, and busy_timeout does NOT
    retry an upgrade deadlock. Measured standalone, 8 processes x 100 claims,
    3 runs: IMMEDIATE took 600 rows with 0 errors, a deferred BEGIN took 461
    and raised 1899 "database is locked".

    This asserts only the IMMEDIATE side. Asserting that a deferred BEGIN DOES
    fail was the first version and it was FLAKY -- with 200 rows and 8 workers
    the queue drains before real contention builds, so the errors that appear
    reliably under sustained load did not appear here at all. A test that needs
    a busy machine to pass is a false alarm waiting to happen
    (DESIGN_PHILOSOPHIES 3), so the deferred measurement lives in this
    docstring and in scratchpad/deferred.py, and the test pins the property we
    actually depend on: the claim path does not raise under the shape that
    breaks a deferred txn.
    """
    path = os.path.join(tempfile.mkdtemp(), "upgrade.db")
    _make_db(path, rows=200)
    with Pool(8) as pool:
        immediate = pool.map(
            _upgrade_worker, [(path, f"a{i}", "IMMEDIATE", 100) for i in range(8)]
        )
    errors = [e for errs in immediate for e in errs]
    assert not errors, (
        f"BEGIN IMMEDIATE raised under contention: {collections.Counter(errors)}"
    )

    conn = _open(path)
    unclaimed = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE claimed_by IS NULL"
    ).fetchone()[0]
    conn.close()
    assert unclaimed == 0, f"{unclaimed} rows never claimed despite 800 attempts"


def test_a_killed_claimer_leaves_no_ghost():
    """AGY's ghost-claim hypothesis, tested rather than argued.

    The claim is that a crash between the UPDATE and the SELECT-back strands
    the row in a claimed state forever. It cannot: both statements are inside
    one uncommitted transaction, so the kill rolls it back. Verified with
    os._exit(9) -- no commit, no atexit, no cleanup -- at exactly that line:
    5 kills, 0 orphans, 10 of 10 rows still claimable.

    The real crash window is AFTER the commit (agent dies mid-work), which is
    a liveness problem about a dead agent, not a correctness problem here.
    """
    path = os.path.join(tempfile.mkdtemp(), "ghost.db")
    _make_db(path, rows=10)

    child = (
        "import sqlite3,os,uuid,sys;"
        f"c=sqlite3.connect(r'{path}',timeout=30,isolation_level=None);"
        "c.execute('PRAGMA journal_mode=WAL');"
        "c.execute('BEGIN IMMEDIATE');"
        "c.execute(\"UPDATE notifications SET claimed_by=? WHERE id=("
        "SELECT id FROM notifications WHERE claimed_by IS NULL ORDER BY id "
        "LIMIT 1)\",(str(uuid.uuid4()),));"
        "os._exit(9)"
    )
    for _ in range(5):
        proc = subprocess.run([sys.executable, "-c", child], capture_output=True)
        assert proc.returncode == 9, f"child exited {proc.returncode}, not the kill"

    conn = _open(path)
    orphaned = conn.execute(
        "SELECT COUNT(*) FROM notifications WHERE claimed_by IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    assert orphaned == 0, (
        f"{orphaned} rows stranded by a killed claimer -- the claim is no "
        f"longer atomic and a reclaim path IS required"
    )
