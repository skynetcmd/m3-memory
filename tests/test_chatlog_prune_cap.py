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


def _seed_decay_tier(db_path, *, n: int):
    """Seed n noise turns in the DECAY tier (14 <= age < prune_days=45) so they
    are suppressed (importance lowered + valid_to set), NOT soft-deleted."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE memory_items(id TEXT PRIMARY KEY, type TEXT, title TEXT, "
        "content TEXT, importance REAL, created_at TEXT, is_deleted INT DEFAULT 0, "
        "updated_at TEXT, valid_to TEXT)"
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = []
    for i in range(n):  # 20 days old => decay tier (>=fresh_days, <prune_days)
        ts = (now - datetime.timedelta(days=20)).isoformat()
        rows.append((f"decay{i}", "chat_log", "user@x", "status", 0.1, ts))
    conn.executemany(
        "INSERT INTO memory_items(id,type,title,content,importance,created_at) "
        "VALUES(?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_decay_converges_second_run_is_noop(tmp_path):
    # THE LOOP BUG: decay lowers importance to ~0.06 and sets valid_to, but a
    # decayed row still has importance<=generic_imp_max, so without a convergence
    # guard it is re-decayed every run forever (the ~11s "same 5000 rows over and
    # over" loop). Assert: run 1 decays all rows; run 2 finds nothing to do.
    db = str(tmp_path / "converge.db")
    _seed_decay_tier(db, n=8)

    first = cp.run(db, _opts(max_actions=0))
    assert first["writes_decay"] == 8, "run 1 should decay all 8 decay-tier rows"
    assert first["writes_prune"] == 0, "decay-tier rows must not be soft-deleted"

    # Confirm they were actually suppressed (valid_to set, importance lowered).
    conn = sqlite3.connect(db)
    decayed = conn.execute(
        "SELECT COUNT(*) FROM memory_items WHERE type='chat_log' "
        "AND valid_to IS NOT NULL AND valid_to <> '' AND importance <= 0.3"
    ).fetchone()[0]
    conn.close()
    assert decayed == 8

    second = cp.run(db, _opts(max_actions=0))
    assert second["writes_decay"] == 0, "run 2 re-decayed already-suppressed rows (loop bug)"
    assert second["writes_prune"] == 0
    assert second["kept_already_decayed"] == 8, "already-decayed rows should be counted + skipped"


def test_decayed_row_still_prunes_when_aged(tmp_path):
    # A row decayed in the DECAY tier must STILL be soft-deleted once it ages past
    # prune_days — the convergence guard skips only re-DECAY, never the eventual
    # prune. Simulate: a row already has valid_to set (decayed earlier) but is now
    # 90 days old (past prune_days) and unprotected.
    db = str(tmp_path / "agedprune.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE memory_items(id TEXT PRIMARY KEY, type TEXT, title TEXT, "
        "content TEXT, importance REAL, created_at TEXT, is_deleted INT DEFAULT 0, "
        "updated_at TEXT, valid_to TEXT)"
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    old_ts = (now - datetime.timedelta(days=90)).isoformat()
    decayed_ts = (now - datetime.timedelta(days=30)).isoformat()
    conn.execute(
        "INSERT INTO memory_items(id,type,title,content,importance,created_at,valid_to) "
        "VALUES(?,?,?,?,?,?,?)",
        ("aged", "chat_log", "user@x", "status", 0.06, old_ts, decayed_ts))
    conn.commit()
    conn.close()

    summary = cp.run(db, _opts(max_actions=0))
    assert summary["writes_prune"] == 1, "an aged, already-decayed, unprotected row must still prune"
    assert summary["writes_decay"] == 0
