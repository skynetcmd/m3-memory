"""`agent_type@instance` addressing: N sessions of one agent, N inboxes.

`agent_id` was one flat identity with ONE ROW PER AGENT TYPE, so three sister
Claude sessions all addressed ``claude-code`` -- three processes, one inbox.
Measured 2026-09-12 with two sessions polling one identity::

    session 1 sees: [856, 857]
    session 2 sees: [856, 857]      <- same items, duplicate work
    session 1 acks -> session 2 now sees: []   <- work silently vanished

The second failure is the serious one: session 2 had already READ those items
and they disappeared mid-flight, with no error and no trace. ``received_at``
(#151) does not help -- it records that *an* instance received the message, not
which one.

See #170 for the separator analysis. The short version: "-" was disqualified
because it is already inside 14 of 26 live ids, and ":" because on NTFS it
creates an alternate data stream instead of a file.
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
    _addressing_predicate,
    _db,  # noqa: E402
    agent_type_of,
    notifications_ack_all_impl,
    notifications_unread_ids_impl,
    notify_impl,
    split_agent_id,
)

_PREFIX = "pytest-addr"


@pytest.fixture
def clean_inboxes():
    def _purge():
        with _db() as db:
            p = dialect().param()
            db.execute(
                f"DELETE FROM notifications WHERE agent_id = {p} "
                f"OR agent_id LIKE {p}",
                (_PREFIX, _PREFIX + "@%"),
            )

    _purge()
    yield
    _purge()


# ── parsing ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("claude-code", ("claude-code", None)),
        ("claude-code@abc123", ("claude-code", "abc123")),
        ("agy@s1", ("agy", "s1")),
        # maxsplit=1: an instance id may itself contain "@".
        ("weird@a@b", ("weird", "a@b")),
        ("", ("", None)),
        # A trailing separator is a type with no instance, not an empty instance.
        ("claude-code@", ("claude-code", None)),
    ],
)
def test_split_agent_id(raw, expected):
    assert split_agent_id(raw) == expected


def test_a_bare_type_is_unchanged():
    """THE back-compat property. Every existing caller passes a bare type; if
    this ever returns something else, every integration breaks at once."""
    assert agent_type_of("claude-code") == "claude-code"
    assert split_agent_id("claude-code") == ("claude-code", None)


def test_hyphen_is_not_the_separator():
    """"-" is inside 14 of 26 live ids. Splitting on it would parse
    `claude-code` as type "claude", instance "code" -- silently rerouting every
    notification for the most-used agent in the system."""
    assert agent_type_of("claude-code") == "claude-code", (
        "the separator is splitting on '-'; `claude-code` is one type name"
    )
    assert agent_type_of("antigravity-agent") == "antigravity-agent"


# ── addressing semantics ─────────────────────────────────────────────────────

def test_qualified_id_reads_only_that_instance(clean_inboxes):
    notify_impl(f"{_PREFIX}@s1", "ping", "for s1")
    notify_impl(f"{_PREFIX}@s2", "ping", "for s2")

    s1 = notifications_unread_ids_impl(f"{_PREFIX}@s1")
    s2 = notifications_unread_ids_impl(f"{_PREFIX}@s2")

    assert len(s1) == 1 and len(s2) == 1
    assert not set(s1) & set(s2), "instances are seeing each other's work"


def test_bare_type_fans_out_to_the_type_and_its_instances(clean_inboxes):
    """A broadcast must reach every session, AND a legacy caller must keep
    seeing what it used to."""
    notify_impl(f"{_PREFIX}@s1", "ping", "for s1")
    notify_impl(f"{_PREFIX}@s2", "ping", "for s2")
    notify_impl(_PREFIX, "ping", "broadcast")

    everything = notifications_unread_ids_impl(_PREFIX)
    assert len(everything) == 3, (
        f"fan-out saw {len(everything)} of 3 -- a bare type must match the "
        f"type's own inbox AND every instance"
    )


def test_one_instance_ack_does_not_empty_another(clean_inboxes):
    """THE silent-theft regression.

    Before #170 this was the failure: session 1's ack cleared rows session 2
    had already read, with no error and no trace.
    """
    notify_impl(f"{_PREFIX}@s1", "ping", "for s1")
    notify_impl(f"{_PREFIX}@s2", "ping", "for s2")

    before = notifications_unread_ids_impl(f"{_PREFIX}@s2")
    notifications_ack_all_impl(f"{_PREFIX}@s1")
    after = notifications_unread_ids_impl(f"{_PREFIX}@s2")

    assert after == before, (
        "one instance's ack consumed another instance's unread rows"
    )
    assert notifications_unread_ids_impl(f"{_PREFIX}@s1") == [], (
        "the acking instance's own rows were not acked"
    )


def test_ack_by_bare_type_still_clears_everything(clean_inboxes):
    """The flip side: ack must follow the SAME rule as the read that produced
    the list. A bare-type read that saw 3 rows must be ackable by a bare-type
    ack, or a legacy caller can never clear its inbox."""
    notify_impl(f"{_PREFIX}@s1", "ping", "a")
    notify_impl(_PREFIX, "ping", "b")
    notifications_ack_all_impl(_PREFIX)
    assert notifications_unread_ids_impl(_PREFIX) == []


# ── injection safety ─────────────────────────────────────────────────────────

def test_sql_wildcards_in_an_agent_id_stay_literal():
    """The fan-out uses LIKE. An id containing % or _ must not become a
    wildcard and silently match other agents' inboxes."""
    pred, params = _addressing_predicate("we%rd", "?")
    assert "ESCAPE" in pred, "LIKE has no ESCAPE clause; % in an id is a wildcard"
    assert params[1] == "we\\%rd@%", params


def test_underscore_is_escaped_too():
    """`_` is a single-character wildcard in SQL LIKE, and it is common in agent
    names -- easy to forget while remembering %."""
    _pred, params = _addressing_predicate("e2e_builder", "?")
    assert params[1] == "e2e\\_builder@%", params


def test_qualified_reads_use_equality_not_like():
    """A direct read needs no pattern matching. Using LIKE there would make
    `agy@s1` match `agy@s10` on a prefix mistake."""
    pred, params = _addressing_predicate("agy@s1", "?")
    assert "LIKE" not in pred, pred
    assert params == ("agy@s1",)
