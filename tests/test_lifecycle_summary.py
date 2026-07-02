"""Tests for memory_lifecycle_summary_impl — windowed lifecycle/contradiction
aggregate over memory_history (mig 009) + memory_corroborations (mig 036).

Read-only; both queries hit indexed `created_at`. Old-DB tolerance: a pre-036 DB
(no memory_corroborations) must degrade the contradiction section to zeros, not
raise — mirroring the reinforcement pass's _is_missing_schema guard.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


@pytest.fixture(autouse=True)
def _skip_migrations(monkeypatch):
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")


def _full_db(db_path):
    from conftest import create_full_main_schema
    create_full_main_schema(db_path)


def _seed_item(conn, mid, title="t"):
    conn.execute(
        "INSERT INTO memory_items (id, type, title, content, source, change_agent, "
        "created_at, importance, is_deleted) VALUES (?, 'fact', ?, 'c', 'agent', "
        "'claude', datetime('now'), 0.5, 0)",
        (mid, title),
    )


def _hist(conn, mid, event, *, days_ago=0):
    conn.execute(
        "INSERT INTO memory_history (id, memory_id, event, created_at) "
        "VALUES (?, ?, ?, datetime('now', ?))",
        (str(uuid.uuid4()), mid, event, f"-{days_ago} days"),
    )


def _corrob(conn, mid, delta, *, days_ago=0):
    conn.execute(
        "INSERT INTO memory_corroborations (id, memory_id, source_kind, source_ref, "
        "trust_at_write, delta, created_at) VALUES (?, ?, 'agent', 'a', 1.0, ?, "
        "datetime('now', ?))",
        (str(uuid.uuid4()), mid, delta, f"-{days_ago} days"),
    )


def test_event_counts_in_window(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    mid = str(uuid.uuid4())
    _seed_item(conn, mid)
    _hist(conn, mid, "create", days_ago=1)
    _hist(conn, mid, "update", days_ago=2)
    _hist(conn, mid, "update", days_ago=3)
    _hist(conn, mid, "supersede", days_ago=100)  # OUTSIDE 7-day window
    conn.commit()
    conn.close()

    monkeypatch.setenv("M3_DATABASE", str(db))
    import memory_maintenance
    out = memory_maintenance.memory_lifecycle_summary_impl(window_days=7)

    assert out["events"]["create"] == 1
    assert out["events"]["update"] == 2
    assert out["events"]["supersede"] == 0   # the 100-day-old one is excluded


def test_corroboration_vs_contradiction_split(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    mid = str(uuid.uuid4())
    _seed_item(conn, mid)
    _corrob(conn, mid, delta=+1.0, days_ago=1)
    _corrob(conn, mid, delta=-1.0, days_ago=1)
    _corrob(conn, mid, delta=-0.5, days_ago=2)
    conn.commit()
    conn.close()

    monkeypatch.setenv("M3_DATABASE", str(db))
    import memory_maintenance
    out = memory_maintenance.memory_lifecycle_summary_impl(window_days=7)

    assert out["corroboration"]["corroborated"] == 1
    assert out["corroboration"]["contradicted"] == 2


def test_top_lists_populated_and_capped(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    hot = str(uuid.uuid4())
    _seed_item(conn, hot, title="hot memory")
    for _ in range(3):
        _hist(conn, hot, "update", days_ago=1)
        _corrob(conn, hot, delta=-1.0, days_ago=1)
    conn.commit()
    conn.close()

    monkeypatch.setenv("M3_DATABASE", str(db))
    import memory_maintenance
    out = memory_maintenance.memory_lifecycle_summary_impl(window_days=7, top_n=5)

    assert out["most_revised"][0]["memory_id"] == hot
    assert out["most_revised"][0]["revisions"] == 3
    assert out["most_revised"][0]["title"] == "hot memory"
    assert out["top_contradicted"][0]["contradiction_count"] == 3

    # top_n=0 omits the lists
    out0 = memory_maintenance.memory_lifecycle_summary_impl(window_days=7, top_n=0)
    assert out0["most_revised"] == []
    assert out0["top_contradicted"] == []


def test_tolerates_missing_corroborations_table(tmp_path, monkeypatch):
    """Pre-036 DB: drop memory_corroborations. The contradiction section must
    degrade to zeros; the history-based section must still work."""
    db = tmp_path / "t.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    mid = str(uuid.uuid4())
    _seed_item(conn, mid)
    _hist(conn, mid, "create", days_ago=1)
    conn.execute("DROP TABLE memory_corroborations")  # → pre-036 state
    conn.commit()
    conn.close()

    monkeypatch.setenv("M3_DATABASE", str(db))
    import memory_maintenance
    out = memory_maintenance.memory_lifecycle_summary_impl(window_days=7)  # must not raise

    assert out["events"]["create"] == 1               # history still works
    assert out["corroboration"]["contradicted"] == 0  # degraded, not crashed
    assert out["top_contradicted"] == []
