"""Ack must not complete work another agent holds a lease on.

`complete_message` fences on `lease_token` precisely so a worker whose lease
lapsed cannot report someone else's attempt as finished. Both ack paths wrote
`read_at` with no fence and no `claimed_by` check, so they reached the same
column through a door that never looked at the lease.

Measured before this fix: claim a message as worker-1, call
`notifications_ack_all`, and the row comes back `read_at` SET with `claimed_by`
still worker-1 -- then worker-1's own `complete_message`, holding its valid
token, returns False. It silently lost work it was actively doing.

The clock is the second half. `complete_message` stamps the DATABASE clock;
both acks stamped the caller's, so one column carried two formats
('...847559+00:00' vs '...Z') written by two machines. pg_sync spans two hosts,
so once anything compares `read_at` that is the N-clock bug already rejected
for `claimed_at` and `last_seen`.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

from conftest import dispatch_conn_for_tests, dispatch_table_for_tests

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory.backends import dialect  # noqa: E402
from memory.orchestration import (  # noqa: E402
    agent_register_impl,
    notifications_ack_all_impl,
    notifications_ack_impl,
    notify_impl,
)

# The store notify_impl writes to -- resolved by production's own
# resolver so these tests cannot drift from the real routing.
_T = dispatch_table_for_tests()


@pytest.fixture
def agent():
    a = f"pytest-ackfence-{uuid.uuid4().hex[:12]}"
    agent_register_impl(a, "test")
    return a


def _claim(agent_id: str, claimant: str = "worker-1"):
    D = dialect()
    with dispatch_conn_for_tests() as db:
        p = D.param()
        return D.claim_message(
            db, table=dispatch_table_for_tests(), where_sql=f"agent_id = {p}",
            where_params=(agent_id,), claimant=claimant, lease_ttl=300,
        )


def _row(row_id):
    with dispatch_conn_for_tests() as db:
        p = dialect().param()
        return db.execute(
            f"SELECT read_at, claimed_by FROM {_T} WHERE id = {p}",
            (row_id,),
        ).fetchone()


def test_ack_all_does_not_steal_a_claimed_message(agent):
    """THE work-stealing defect, via the bulk path."""
    notify_impl(agent, "ping", {"work": 1})
    got = _claim(agent)
    assert got is not None
    row_id, token = got

    notifications_ack_all_impl(agent)

    assert _row(row_id)["read_at"] is None, (
        "ack_all completed a message another worker holds a live lease on"
    )

    with dispatch_conn_for_tests() as db:
        still_mine = dialect().complete_message(
            db, table=dispatch_table_for_tests(), row_id=row_id, lease_token=token,
        )
    assert still_mine, (
        "the leaseholder could no longer complete its own work -- ack stole it"
    )


def test_ack_by_id_does_not_steal_a_claimed_message(agent):
    """Same defect, single-row path. Both doors or neither."""
    notify_impl(agent, "ping", {"work": 1})
    got = _claim(agent)
    assert got is not None
    row_id, token = got

    out = notifications_ack_impl(row_id)

    assert _row(row_id)["read_at"] is None, (
        f"ack by id completed a claimed message: {out!r}"
    )
    with dispatch_conn_for_tests() as db:
        assert dialect().complete_message(
            db, table=dispatch_table_for_tests(), row_id=row_id, lease_token=token,
        ), "the leaseholder lost its work to a by-id ack"


def test_ack_still_works_on_an_unclaimed_message(agent):
    """The polarity guard. A fence that refused everything would pass the two
    tests above while breaking every real ack."""
    notify_impl(agent, "ping", {"work": 1})
    notifications_ack_all_impl(agent)

    with dispatch_conn_for_tests() as db:
        p = dialect().param()
        row = db.execute(
            f"SELECT read_at FROM {_T} WHERE agent_id = {p}", (agent,)
        ).fetchone()
    assert row["read_at"] is not None, (
        "an UNCLAIMED message could no longer be acked -- the queue is now stuck"
    )


def test_ack_by_id_still_works_on_an_unclaimed_message(agent):
    notify_impl(agent, "ping", {"work": 1})
    with dispatch_conn_for_tests() as db:
        p = dialect().param()
        rid = db.execute(
            f"SELECT id FROM {_T} WHERE agent_id = {p}", (agent,)
        ).fetchone()["id"]

    out = notifications_ack_impl(rid)
    assert "Error" not in out, out
    assert _row(rid)["read_at"] is not None


def test_read_at_is_written_by_the_database_clock(agent):
    """One column, one clock, one format.

    A Python isoformat() writes microseconds and a +00:00 offset; the DB clock
    writes '...Z'. complete_message already used the DB clock, so before this
    fix `read_at` carried BOTH spellings depending on which path closed the row.
    """
    notify_impl(agent, "ping", {"work": 1})
    notifications_ack_all_impl(agent)

    with dispatch_conn_for_tests() as db:
        p = dialect().param()
        stamped = db.execute(
            f"SELECT read_at FROM {_T} WHERE agent_id = {p}", (agent,)
        ).fetchone()["read_at"]

    assert stamped.endswith("Z"), (
        f"read_at {stamped!r} is not in the DB's format -- a Python "
        f"isoformat() leaks a +00:00 offset"
    )
    assert "." not in stamped, (
        f"read_at {stamped!r} carries microseconds, which the DB clock does "
        f"not emit -- this was written by the caller"
    )


def test_both_ack_paths_agree_on_the_clock(agent):
    """Two writers to one column must not disagree, which is how the split
    arose in the first place."""
    notify_impl(agent, "a", {})
    notify_impl(agent, "b", {})
    with dispatch_conn_for_tests() as db:
        p = dialect().param()
        ids = [r["id"] for r in db.execute(
            f"SELECT id FROM {_T} WHERE agent_id = {p} ORDER BY id",
            (agent,)).fetchall()]

    notifications_ack_impl(ids[0])
    notifications_ack_all_impl(agent)

    a, b = _row(ids[0])["read_at"], _row(ids[1])["read_at"]
    assert a.endswith("Z") and b.endswith("Z"), (
        f"the two ack paths wrote different formats: {a!r} vs {b!r}"
    )


def test_a_dead_lettered_message_is_still_ackable(agent):
    """The deliberate asymmetry between poll and ack, pinned so it survives a
    well-meaning "unify these two predicates" refactor.

    `notifications_poll` HIDES dead-lettered rows -- a poison message is not
    work to hand out. Ack must still CLEAR them, or a row that failed
    `max_attempts` times sits unread in the inbox forever with no way to remove
    it. That is why ack's predicate is NOT `message_state_sql("PENDING")`:
    PENDING also requires `failed_at IS NULL`.
    """
    notify_impl(agent, "poison", {"work": 1})
    got = _claim(agent)
    assert got is not None
    row_id = got[0]

    D = dialect()
    with dispatch_conn_for_tests() as db:
        p_ = D.param()
        # Exactly what sweep_expired_leases does at max_attempts.
        db.execute(
            f"UPDATE {_T} SET failed_at = {D.now()}, claimed_by = NULL, "
            f"lease_token = NULL, claim_expires_at = NULL WHERE id = {p_}",
            (row_id,),
        )

    out = notifications_ack_all_impl(agent)
    assert _row(row_id)["read_at"] is not None, (
        f"a dead-lettered message could not be acked ({out!r}) -- it is hidden "
        f"from poll AND unclearable, so it is stuck in the inbox permanently"
    )


def test_ack_all_reports_what_it_left_behind(agent):
    """§3: "Acked 0" while rows sit claimed reads as an empty queue, which is
    the one state it is not."""
    notify_impl(agent, "ping", {"work": 1})
    _claim(agent)

    out = notifications_ack_all_impl(agent)
    assert "claimed" in out, (
        f"ack_all silently left claimed work behind: {out!r}"
    )
