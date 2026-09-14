"""Polling proves the POLLER alive — and says so when it cannot.

`agent_heartbeat` had no production caller, so `agents.last_seen` was written
only at registration: measured 2026-09-14, two continuously-active agents read
stale by 42+ minutes and six showed `status=active` with `last_seen` up to five
months old. Wiring the heartbeat into `notifications_poll` fixes that — an agent
that checks its mail needs no separate loop.

The three things this file pins are the ways that fix can be wrong while
looking right:

1. a bare-type poll must NOT stamp the sisters whose mail it read;
2. the timestamp must come from the DATABASE clock, not the caller's;
3. an unregistered poller must be TOLD it left no trace.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory.backends import dialect  # noqa: E402
from memory.orchestration import (  # noqa: E402
    _db,
    agent_register_impl,
    notifications_poll_impl,
)

_TYPE = "pytest-hb"
_S1 = f"{_TYPE}@s1"
_S2 = f"{_TYPE}@s2"
_ANCIENT = "2020-01-01T00:00:00Z"


@pytest.fixture
def agents():
    def _purge():
        with _db() as db:
            p = dialect().param()
            db.execute(
                f"DELETE FROM agents WHERE agent_id = {p} OR agent_id LIKE {p}",
                (_TYPE, _TYPE + "@%"),
            )
            db.execute(
                f"DELETE FROM notifications WHERE agent_id = {p} "
                f"OR agent_id LIKE {p}",
                (_TYPE, _TYPE + "@%"),
            )

    _purge()
    agent_register_impl(_TYPE, "test")
    agent_register_impl(_S1, "test")
    agent_register_impl(_S2, "test")
    yield
    _purge()


def _last_seen(agent_id: str):
    with _db() as db:
        p = dialect().param()
        row = db.execute(
            f"SELECT last_seen FROM agents WHERE agent_id = {p}", (agent_id,)
        ).fetchone()
    return row["last_seen"] if row else None


def _backdate(agent_id: str) -> None:
    with _db() as db:
        p = dialect().param()
        db.execute(
            f"UPDATE agents SET last_seen = {p} WHERE agent_id = {p}",
            (_ANCIENT, agent_id),
        )


def test_polling_records_a_heartbeat_for_the_poller(agents):
    _backdate(_S1)
    notifications_poll_impl(_S1)
    assert _last_seen(_S1) != _ANCIENT, (
        "polling did not record a heartbeat -- the agent is invisible to "
        "liveness checks despite demonstrably running"
    )


def test_a_fan_out_poll_does_not_revive_the_sisters_it_read_for(agents):
    """THE dangerous direction.

    A bare type is a FAN-OUT read: polling `pytest-hb` returns mail addressed to
    `pytest-hb@s1` and `@s2`. Heartbeating the addressing predicate would then
    stamp every registered sister alive whenever an orchestrator drained the
    shared queue -- including sisters that died hours ago. A liveness signal
    that cannot say "dead" is not a liveness signal, and the sweeper downstream
    would trust it. Reading a sister's mail is not evidence about the sister.
    """
    _backdate(_S1)
    _backdate(_S2)

    notifications_poll_impl(_TYPE, unread_only=False)

    assert _last_seen(_S1) == _ANCIENT, (
        "a bare-type poll revived sister s1 -- a dead agent now looks alive"
    )
    assert _last_seen(_S2) == _ANCIENT
    assert _last_seen(_TYPE) != _ANCIENT, "the poller itself must be stamped"


def test_the_heartbeat_uses_the_database_clock(agents):
    """Same argument as claim_message (e738e366): pg_sync spans two hosts, so a
    caller-supplied timestamp makes two agents compare their own clocks and
    reach different verdicts about the same agent. It also keeps ONE format in
    the column -- a Python isoformat() writes microseconds and a +00:00 offset
    where the DB writes ...Z.
    """
    notifications_poll_impl(_S1)
    stamped = _last_seen(_S1)

    with _db() as db:
        db_now = db.execute(f"SELECT {dialect().now()}").fetchone()[0]

    assert stamped.endswith("Z"), (
        f"last_seen {stamped!r} is not in the DB's format -- a Python "
        f"datetime.isoformat() leaks a +00:00 offset"
    )
    assert "." not in stamped, (
        f"last_seen {stamped!r} carries microseconds, which the DB clock does "
        f"not emit -- this was written by the caller"
    )
    assert stamped[:13] == db_now[:13], (
        f"last_seen {stamped!r} does not track the DB clock {db_now!r}"
    )


def test_an_unregistered_poller_is_told_it_left_no_trace(agents):
    """Not an error -- the mail is genuinely returned -- but not silent either.

    An agent that believes it is heartbeating while invisible to every liveness
    check is a §3 false negative on the one signal a reclaim decision needs.
    """
    out = notifications_poll_impl(f"{_TYPE}@never-registered")

    assert "not registered" in out, (
        f"an unregistered poll recorded no heartbeat and said nothing: {out!r}"
    )
    assert "agent_register" in out, "the note must say how to fix it"


def test_a_registered_poller_gets_no_warning(agents):
    """The other polarity. A note that fires when nothing is wrong trains
    people to ignore the one that matters (§3)."""
    out = notifications_poll_impl(_S1)
    assert "not registered" not in out, (
        f"false alarm on a registered agent: {out!r}"
    )


def test_structured_output_carries_the_heartbeat_flag(agents):
    """A records caller must be able to branch on it without parsing prose."""
    import json

    reg = json.loads(notifications_poll_impl(_S1, as_records=True))
    unreg = json.loads(
        notifications_poll_impl(f"{_TYPE}@never-registered", as_records=True)
    )
    assert reg.get("heartbeat_recorded") is True
    assert unreg.get("heartbeat_recorded") is False
