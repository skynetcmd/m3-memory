"""Derived message state: the SQL and the Python must never disagree.

There is no `status` column by design. All four states are derived from
failed_at / read_at / claimed_by, because a stored status would give "is this
pending" two sources of truth and six live call sites already filter on
`read_at IS NULL` -- including the waiter that delivers agent mail.

Deriving it twice (once as SQL for filtering, once in Python for display) is
itself a duplication risk, so this file exists to pin them together: for every
combination of the three columns, the row the SQL selects must be the row the
Python labels.
"""
from __future__ import annotations

import itertools
import os
import sqlite3
import sys
import tempfile

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory.orchestration import (  # noqa: E402
    MESSAGE_STATES,
    message_state_of,
    message_state_sql,
)

_TS = "2026-09-14T00:00:00Z"


@pytest.fixture
def conn():
    path = os.path.join(tempfile.mkdtemp(), "state.db")
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE notifications (id INTEGER PRIMARY KEY, failed_at TEXT, "
        "read_at TEXT, claimed_by TEXT)"
    )
    # Every combination of the three columns being set or not.
    rows = list(itertools.product([None, _TS], [None, _TS], [None, "agent@s1"]))
    db.executemany(
        "INSERT INTO notifications (id, failed_at, read_at, claimed_by) "
        "VALUES (?, ?, ?, ?)",
        [(i, *combo) for i, combo in enumerate(rows, start=1)],
    )
    db.commit()
    yield db
    db.close()


def test_every_row_has_exactly_one_state(conn):
    """The predicates must PARTITION the table -- no row in two states, none in
    none. Overlapping predicates would make a row appear in two filtered views
    and vanish from a third."""
    for row in conn.execute("SELECT id FROM notifications").fetchall():
        matched = [
            state for state in MESSAGE_STATES
            if conn.execute(
                f"SELECT 1 FROM notifications WHERE id = ? "
                f"AND ({message_state_sql(state)})", (row["id"],)
            ).fetchone()
        ]
        assert len(matched) == 1, (
            f"row {row['id']} matched {matched} -- the state predicates must "
            f"partition the table, not overlap"
        )


def test_sql_and_python_agree_on_every_combination(conn):
    """THE drift guard. If these diverge, a row is filtered as one state and
    displayed as another -- the exact failure a stored status column would have
    institutionalised."""
    for row in conn.execute("SELECT * FROM notifications").fetchall():
        python_says = message_state_of(row)
        sql_says = [
            state for state in MESSAGE_STATES
            if conn.execute(
                f"SELECT 1 FROM notifications WHERE id = ? "
                f"AND ({message_state_sql(state)})", (row["id"],)
            ).fetchone()
        ]
        assert sql_says == [python_says], (
            f"row {dict(row)}: SQL says {sql_says}, Python says "
            f"{python_says!r} -- the two derivations have drifted"
        )


def test_all_four_states_are_reachable(conn):
    """A predicate that can never match is dead code pretending to be coverage."""
    seen = {
        state for state in MESSAGE_STATES
        if conn.execute(
            f"SELECT 1 FROM notifications WHERE {message_state_sql(state)}"
        ).fetchone()
    }
    assert seen == set(MESSAGE_STATES), f"unreachable states: {set(MESSAGE_STATES) - seen}"


def test_a_completed_row_stays_completed_even_though_it_is_still_claimed(conn):
    """Order matters: a finished row keeps its claimed_by, so a naive
    claimed_by-first check would report it as still in flight forever."""
    conn.execute(
        "INSERT INTO notifications (id, failed_at, read_at, claimed_by) "
        "VALUES (99, NULL, ?, 'agent@s1')", (_TS,)
    )
    row = conn.execute("SELECT * FROM notifications WHERE id = 99").fetchone()
    assert message_state_of(row) == "COMPLETED"


def test_a_failed_row_outranks_everything(conn):
    conn.execute(
        "INSERT INTO notifications (id, failed_at, read_at, claimed_by) "
        "VALUES (98, ?, ?, 'agent@s1')", (_TS, _TS)
    )
    row = conn.execute("SELECT * FROM notifications WHERE id = 98").fetchone()
    assert message_state_of(row) == "FAILED"


def test_an_unknown_state_is_refused_not_silently_empty(conn):
    """A typo must raise, not render a predicate that matches nothing -- an
    empty result is indistinguishable from 'no rows in that state' (§3)."""
    with pytest.raises(ValueError, match="unknown message state"):
        message_state_sql("PENDNIG")
