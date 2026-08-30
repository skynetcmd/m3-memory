"""Regression guard: the KB browser's type filter must not break the query.

Reported 2026-08-29 (m3-memory 2026.8.19.16, Ubuntu, Python 3.12): selecting any
type in the dashboard's KB Browser produced

    Error scanning DB: no such column: mi.type

while "all types" worked fine.

Cause: `dialect().scope_predicates()` qualifies its predicates with its
`table_alias` (default ``"mi"``), so a type filter emits ``AND mi.type LIKE ?``.
The dashboard's browse query selected ``FROM memory_items`` with NO alias, so the
qualified column had nothing to bind to. It only surfaced under a filter because
an empty ``type_filter`` emits no predicate at all — which is exactly why
browse-all looked healthy and hid the bug.

This test pins the contract at the seam rather than asserting on dashboard HTML:
whatever `scope_predicates` emits must be executable against the table shape the
dashboard actually queries.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)


# The SELECT shape the dashboard browse path uses. Kept in the test so a future
# edit that drops the alias fails here instead of in a user's browser.
_BROWSE_SQL = """
    SELECT mi.id, mi.type, mi.title, mi.content, mi.metadata_json,
           mi.importance, mi.origin_device, mi.change_agent,
           mi.created_at, mi.updated_at, mi.confidence, mi.pinned,
           mi.source, mi.valid_from, mi.valid_to,
           mi.corroboration_count, mi.contradiction_count
    FROM memory_items AS mi
    WHERE mi.is_deleted = 0
"""


@pytest.fixture()
def store(tmp_path):
    """A minimal memory_items table with the columns the browse query projects."""
    db = tmp_path / "agent_memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY, type TEXT, title TEXT, content TEXT,
            metadata_json TEXT, importance REAL, origin_device TEXT,
            change_agent TEXT, created_at TEXT, updated_at TEXT,
            confidence REAL, pinned INTEGER, source TEXT, valid_from TEXT,
            valid_to TEXT, corroboration_count INTEGER,
            contradiction_count INTEGER, is_deleted INTEGER DEFAULT 0,
            user_id TEXT, scope TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO memory_items (id, type, title, is_deleted) VALUES (?, ?, ?, 0)",
        [("a", "reference", "one"), ("b", "procedure", "two"), ("c", "reference", "three")],
    )
    conn.commit()
    yield conn
    conn.close()


def _dialect():
    from memory.backends import active_backend
    return active_backend().dialect()


def test_browse_query_runs_unfiltered(store):
    """Baseline: no type filter emits no predicate, and the query runs."""
    sql, params = _dialect().scope_predicates(type_filter="")
    rows = store.execute(_BROWSE_SQL + sql, params).fetchall()
    assert len(rows) == 3


@pytest.mark.parametrize("type_filter", ["reference", "procedure"])
def test_browse_query_runs_with_a_type_filter(store, type_filter):
    """The reported bug: a type filter must not reference an unbound alias.

    Before the fix this raised sqlite3.OperationalError: no such column: mi.type.
    """
    sql, params = _dialect().scope_predicates(type_filter=type_filter)
    rows = store.execute(_BROWSE_SQL + sql, params).fetchall()
    # type is the second projected column.
    assert rows, f"type filter {type_filter!r} returned nothing"
    assert all(r[1] == type_filter for r in rows)


def test_scope_predicates_qualifies_with_its_alias():
    """Pin WHY the alias is required, so the coupling is not silently dropped.

    If scope_predicates ever stops qualifying (or changes its default alias),
    this fails and whoever changes it sees the dashboard depends on it.
    """
    sql, _ = _dialect().scope_predicates(type_filter="reference")
    assert "mi." in sql, (
        "scope_predicates no longer qualifies with the 'mi' alias — the "
        "dashboard browse query aliases memory_items AS mi to match it"
    )
