"""A CLAIMED message must not be handed to a second poller.

`claim_message` has been a seam primitive with no caller since 2026.9.14.2: it
writes `claimed_by` / `lease_token` / `claim_expires_at`, and
`notifications_poll` never looked at any of them. It filters on `read_at IS
NULL` alone, so an in-flight message stayed visible to every other reader.

Measured before the fix: notify -> poll (visible) -> claim_message succeeds ->
poll again as a different reader -> STILL VISIBLE. Latent only because nothing
claimed; the moment the dispatch daemon claims, two agents do the same work.
That is the whole point of a lease, so a lease nobody reads is not a lease.

`unread_only=False` is the AUDIT view and deliberately still shows claimed rows
— an operator asking "what is in this queue" must see work in flight, or a
stuck claim becomes invisible at exactly the moment someone is looking for it.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from conftest import dispatch_conn_for_tests, dispatch_table_for_tests

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory.backends import dialect  # noqa: E402
from memory.orchestration import (  # noqa: E402
    agent_register_impl,
    notifications_poll_impl,
    notify_impl,
)

# The store notify_impl writes to -- resolved by production's own
# resolver so these tests cannot drift from the real routing.
_T = dispatch_table_for_tests()


@pytest.fixture
def agent():
    a = f"pytest-lease-{uuid.uuid4().hex[:12]}"
    agent_register_impl(a, "test")
    return a


def _claim(agent_id: str, claimant: str = "worker-1", ttl: int = 300):
    D = dialect()
    with dispatch_conn_for_tests() as db:
        p = D.param()
        return D.claim_message(
            db, table=dispatch_table_for_tests(), where_sql=f"agent_id = {p}",
            where_params=(agent_id,), claimant=claimant, lease_ttl=ttl,
        )


def test_a_claimed_message_is_not_offered_to_a_second_poller(agent):
    """THE defect. Two agents must not both receive the same message."""
    notify_impl(agent, "ping", {"work": 1})
    assert "(empty)" not in notifications_poll_impl(agent, unread_only=True), (
        "precondition failed: the message was not visible before the claim"
    )

    assert _claim(agent) is not None, "claim_message did not claim the row"

    out = notifications_poll_impl(agent, unread_only=True)
    assert "(empty)" in out, (
        f"a CLAIMED message was handed to a second poller: {out!r} -- both "
        f"agents would now do the same work"
    )


def test_the_audit_view_still_shows_claimed_work(agent):
    """The other polarity. unread_only=False is how an operator inspects the
    queue; hiding in-flight work there would make a stuck claim invisible
    exactly when someone is hunting for it (§3)."""
    notify_impl(agent, "ping", {"work": 1})
    _claim(agent)

    out = notifications_poll_impl(agent, unread_only=False)
    assert "(empty)" not in out, (
        "the audit view hid claimed work -- a stuck lease is now invisible"
    )


def test_an_unclaimed_message_is_still_offered(agent):
    """A guard that hid everything would pass the first test trivially."""
    notify_impl(agent, "ping", {"work": 1})
    out = notifications_poll_impl(agent, unread_only=True)
    assert "(empty)" not in out, (
        f"an UNCLAIMED message was withheld -- the queue is now dead: {out!r}"
    )


def test_a_lapsed_claim_is_offered_again(agent):
    """A lease is a deadline, not a lock. If the holder dies, the work must
    become claimable again or one crashed worker strands the message forever.

    Uses the sweeper rather than a hand-edited row: reclaim is the sweeper's
    job, and asserting on its behaviour keeps ONE owner for the rule.
    """
    notify_impl(agent, "ping", {"work": 1})
    got = _claim(agent)
    assert got is not None
    row_id = got[0]

    # Backdate the deadline rather than claiming with a non-positive TTL:
    # claim_message refuses that outright (a NULL deadline on SQLite is an
    # immortal claim no sweeper reclaims). Time passing is what happens in
    # production anyway. The value is computed in Python and bound as a
    # parameter, so this needs no backend-specific date arithmetic.
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with dispatch_conn_for_tests() as db:
        p_ = dialect().param()
        db.execute(
            f"UPDATE {_T} SET claim_expires_at = {p_} "
            f"WHERE id = {p_}",
            (past, row_id),
        )

    D = dialect()
    with dispatch_conn_for_tests() as db:
        reclaimed, dead = D.sweep_expired_leases(db, table=dispatch_table_for_tests())
    assert reclaimed >= 1, (
        f"the sweeper did not reclaim an expired lease (reclaimed={reclaimed}, "
        f"dead_lettered={dead})"
    )

    out = notifications_poll_impl(agent, unread_only=True)
    assert "(empty)" not in out, (
        "a message whose lease lapsed was never offered again -- one dead "
        "worker strands it permanently"
    )


def test_structured_output_agrees_with_the_prose(agent):
    """A records caller must not see a different queue than a prose caller."""
    notify_impl(agent, "ping", {"work": 1})
    _claim(agent)

    payload = json.loads(notifications_poll_impl(agent, unread_only=True,
                                                 as_records=True))
    assert payload.get("items") == [], (
        f"as_records exposed a claimed message the prose view hides: {payload!r}"
    )


def test_a_dead_lettered_message_is_never_offered_again(agent):
    """The poison-message loop, and the reason this predicate has ONE owner.

    `read_at IS NULL AND claimed_by IS NULL` looks like PENDING and is not: it
    drops `failed_at IS NULL`. A row dead-lettered by `sweep_expired_leases`
    after `max_attempts` reclaims has claimed_by cleared, so a hand-written
    predicate hands it straight back out -- forever, to every poller, rendered
    as healthy queue activity. That is the exact failure dead-lettering exists
    to stop.

    Measured against the inline spelling before this test existed: a row with
    failed_at set was still returned by poll.
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

    out = notifications_poll_impl(agent, unread_only=True)
    assert "(empty)" in out, (
        f"a DEAD-LETTERED message was offered again: {out!r} -- this is the "
        f"poison-message loop, and it looks like healthy queue activity"
    )


def test_the_poll_predicate_has_exactly_one_owner():
    """§10a: a copied predicate is the defect independent of correctness.

    Pins the CALL SITE, not the rendered SQL -- the bug this guards against was
    a hand-written spelling that was subtly narrower than the owner. If someone
    inlines the columns again, this fails even if their spelling happens to be
    correct that day.
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(notifications_poll_impl))

    # CODE only. A guard that greps raw source cannot tell an inlined predicate
    # from a comment explaining why inlining is wrong -- and the impl now
    # carries exactly such a comment, so the naive version fails on its own
    # documentation. Parse and inspect string literals instead.
    tree = ast.parse(src)
    doc = ast.get_docstring(tree.body[0]) if tree.body else None
    code_strings = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value != doc
    ]
    joined = " ".join(code_strings)

    assert "message_state_sql" in src, (
        "notifications_poll must express PENDING through message_state_sql, "
        "not a hand-written column list"
    )
    assert "claimed_by" not in joined, (
        f"notifications_poll hand-writes the PENDING predicate in a SQL "
        f"string; it drifts from message_state_sql the moment a state gains a "
        f"nuance (it already did: the inline spelling dropped "
        f"`failed_at IS NULL`). Offending: "
        f"{[s for s in code_strings if 'claimed_by' in s]}"
    )


def test_polling_alone_reclaims_a_lapsed_lease(agent):
    """POLL ITSELF must sweep -- no manual sweep, no daemon, no fixture help.

    `test_a_lapsed_claim_is_offered_again` proves the SWEEPER works, but it
    calls `sweep_expired_leases` by hand. That is exactly how the real defect
    hid: the mechanism shipped with 16 green tests and, measured 2026-09-16,
    ZERO production callers. A crashed worker stranded its row permanently --
    claimed_by stayed set, poll skipped it as in-flight forever, nothing raised.

    This test deliberately does NOT call the sweeper. If the opportunistic call
    in notifications_poll_impl is removed, it fails.
    """
    notify_impl(agent, "ping", {"work": 1})
    got = _claim(agent)
    assert got is not None
    row_id = got[0]

    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with dispatch_conn_for_tests() as db:
        p_ = dialect().param()
        db.execute(
            f"UPDATE {_T} SET claim_expires_at = {p_} WHERE id = {p_}",
            (past, row_id),
        )

    # The ONLY call is poll. Nothing sweeps on this test's behalf.
    out = notifications_poll_impl(agent, unread_only=True)

    assert "(empty)" not in out, (
        "a lapsed lease was not reclaimed by polling alone.\n"
        f"observed: poll returned '(empty)' for row {row_id}, whose "
        f"claim_expires_at was backdated to {past}\n"
        "possible: the opportunistic sweep was removed from "
        "notifications_poll_impl, or it no longer runs before the read"
        "\n"
        "inspect: sweep_leases_opportunistically in bin/memory/orchestration.py"
    )

    with dispatch_conn_for_tests() as db:
        p_ = dialect().param()
        row = db.execute(
            f"SELECT claimed_by, lease_token FROM {_T} WHERE id = {p_}",
            (row_id,),
        ).fetchone()
    assert row["claimed_by"] is None and row["lease_token"] is None, (
        "the row was offered again but its fence was not cleared.\n"
        f"observed: claimed_by={row['claimed_by']!r} "
        f"lease_token={row['lease_token']!r}\n"
        "possible: the sweep reclaimed without clearing ownership, so the dead "
        "worker's token would still match and it could complete the row\n"
        "inspect: Dialect.sweep_expired_leases"
    )


def test_a_sweep_failure_never_fails_the_poll(agent, monkeypatch):
    """Returning an agent's mail matters more than reclaiming a stale lease.

    The next poll sweeps again, so the work is redundant by construction -- one
    of the few places swallowing an error is correct rather than a §3
    violation. If a sweep error could propagate, a single locked store would
    make every agent's inbox unreadable.
    """
    notify_impl(agent, "ping", {"work": 1})

    def boom(*a, **k):
        raise RuntimeError("store locked by another writer")

    monkeypatch.setattr(dialect().__class__, "sweep_expired_leases", boom)

    out = notifications_poll_impl(agent, unread_only=True)
    assert "(empty)" not in out, (
        "a sweep failure suppressed the caller's mail.\n"
        "observed: poll returned '(empty)' while a notification was pending\n"
        "cause: the sweep exception propagated instead of being reported\n"
        "inspect: sweep_leases_opportunistically in bin/memory/orchestration.py"
    )
