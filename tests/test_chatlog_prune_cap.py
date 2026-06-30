"""Tests for chatlog_prune's per-run action cap and aged-window scan scoping.

Guards the §4/§8 bounding added so a large chatlog backlog drains across many
cognitive-loop cycles instead of one unbounded whole-table pass.
"""
from __future__ import annotations

import datetime
import os
import sqlite3
import sys
from types import SimpleNamespace

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import chatlog_prune as cp


def _opts(**over):
    base = dict(
        fresh_days=14.0, prune_days=45.0, status_min_cluster=5,
        generic_imp_max=0.3, keep_imp_floor=0.4, generic_protect_len=300,
        generic_delete_maxlen=300, no_generic=False, max_actions=0,
        apply=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _seed(db_path, *, aged_noise: int, fresh_noise: int):
    """Create a minimal memory_items table with N aged + M fresh noise turns."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memory_items(id TEXT PRIMARY KEY, type TEXT, title TEXT, "
        "content TEXT, importance REAL, created_at TEXT, is_deleted INT DEFAULT 0, "
        "updated_at TEXT, valid_to TEXT)"
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = []
    for i in range(aged_noise):  # 90+ days old => prune-eligible
        ts = (now - datetime.timedelta(days=90 + i)).isoformat()
        rows.append((f"old{i}", "chat_log", "user@x", "status", 0.1, ts))
    for i in range(fresh_noise):  # 1 day old => must be left untouched
        ts = (now - datetime.timedelta(days=1)).isoformat()
        rows.append((f"fresh{i}", "chat_log", "user@x", "status", 0.1, ts))
    conn.executemany(
        "INSERT INTO memory_items(id,type,title,content,importance,created_at) "
        "VALUES(?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_max_actions_caps_writes_and_flags(tmp_path):
    db = str(tmp_path / "cap.db")
    _seed(db, aged_noise=10, fresh_noise=3)
    summary = cp.run(db, _opts(max_actions=4))
    # Exactly the cap is acted on, capped flag is surfaced (not silent).
    assert summary["writes_prune"] + summary["writes_decay"] == 4
    assert summary["capped"] is True
    # 6 aged rows remain un-deleted for the next run.
    conn = sqlite3.connect(db)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM memory_items WHERE type='chat_log' AND is_deleted=0"
    ).fetchone()[0]
    conn.close()
    # 6 aged not-yet-pruned + 3 fresh untouched = 9.
    assert remaining == 9


def test_no_cap_processes_all_aged(tmp_path):
    db = str(tmp_path / "nocap.db")
    _seed(db, aged_noise=10, fresh_noise=3)
    summary = cp.run(db, _opts(max_actions=0))
    assert summary["capped"] is False
    assert summary["writes_prune"] + summary["writes_decay"] == 10


def test_scan_scoped_to_aged_window_skips_fresh(tmp_path):
    # The fresh rows must never be scanned/acted on: the SELECT filters by
    # created_at < fresh cutoff, so `scanned` reflects only the aged window.
    db = str(tmp_path / "scope.db")
    _seed(db, aged_noise=5, fresh_noise=100)
    summary = cp.run(db, _opts(max_actions=0))
    # Only the 5 aged rows are pulled into Python, not the 100 fresh ones.
    assert summary["scanned"] == 5
    assert summary["writes_prune"] + summary["writes_decay"] == 5
