"""Tests for the WriteQueueDaemon — coalesced single-writer commit queue (M4).

The daemon funnels concurrent single-row writes through one writer task that
batches statements within a short window and commits them in ONE transaction,
while each caller awaits its own per-item future. These tests verify:

  - correctness: every caller gets its own rowcount result back
  - batching: N concurrent writes commit in far fewer transactions than N
  - error isolation: one bad statement fails only its own future, not the batch
  - bypass: M3_WRITE_QUEUE_DISABLE=1 commits inline
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Fresh tmp DB + purge cached memory.* modules so each test re-reads env."""
    monkeypatch.setenv("M3_DATABASE", str(tmp_path / "wq.db"))
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")
    monkeypatch.delenv("M3_WRITE_QUEUE_DISABLE", raising=False)
    saved = {m: sys.modules[m] for m in list(sys.modules) if m.startswith("memory")}
    for mod in list(sys.modules):
        if mod.startswith("memory"):
            del sys.modules[mod]
    yield
    for mod in [m for m in sys.modules if m.startswith("memory")]:
        del sys.modules[mod]
    for name, module in saved.items():
        if module is not None:
            sys.modules[name] = module


def _make_table(db_path: str) -> None:
    """Create a minimal table the daemon can write into via the _db() pool."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS wq_test (id INTEGER PRIMARY KEY, v TEXT)")
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_single_write_returns_rowcount(tmp_path):
    db_path = str(tmp_path / "wq.db")
    _make_table(db_path)
    from memory.db import _enqueue_write

    rc = await _enqueue_write("INSERT INTO wq_test (v) VALUES (?)", ("a",))
    assert rc == 1

    # Row actually landed.
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM wq_test").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_concurrent_writes_all_resolve(tmp_path):
    """50 concurrent writes must each resolve, and all rows must land."""
    db_path = str(tmp_path / "wq.db")
    _make_table(db_path)
    from memory.db import _enqueue_write

    N = 50
    results = await asyncio.gather(
        *(_enqueue_write("INSERT INTO wq_test (v) VALUES (?)", (f"v{i}",)) for i in range(N))
    )
    assert all(rc == 1 for rc in results)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM wq_test").fetchone()[0] == N
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_writes_are_actually_batched(tmp_path, monkeypatch):
    """Concurrent writes should commit in far fewer transactions than rows.

    Each `_commit_batch` opens the `_db()` context exactly once (one
    transaction per batch). We count `_db()` entries: a burst of 30
    concurrent writes batched within the aggregation window must produce
    far fewer than 30 transactions — proving coalescing, not one-per-row.
    """
    db_path = str(tmp_path / "wq.db")
    _make_table(db_path)
    from memory import db as dbmod

    txn_count = {"n": 0}
    real_db = dbmod._db

    import contextlib

    @contextlib.contextmanager
    def _counting_db():
        txn_count["n"] += 1
        with real_db() as conn:
            yield conn

    monkeypatch.setattr(dbmod, "_db", _counting_db)

    N = 30
    await asyncio.gather(
        *(dbmod._enqueue_write("INSERT INTO wq_test (v) VALUES (?)", (f"v{i}",)) for i in range(N))
    )
    # Batched: a handful of transactions, never one-per-row.
    assert txn_count["n"] < N, f"expected batching, got {txn_count['n']} transactions for {N} writes"
    assert txn_count["n"] >= 1
    # And every row still landed.
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM wq_test").fetchone()[0] == N
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_error_isolation(tmp_path):
    """A failing statement fails only its own future; siblings still commit."""
    db_path = str(tmp_path / "wq.db")
    _make_table(db_path)
    from memory.db import _enqueue_write

    good = _enqueue_write("INSERT INTO wq_test (v) VALUES (?)", ("ok",))
    bad = _enqueue_write("INSERT INTO no_such_table (v) VALUES (?)", ("x",))
    good2 = _enqueue_write("INSERT INTO wq_test (v) VALUES (?)", ("ok2",))

    results = await asyncio.gather(good, bad, good2, return_exceptions=True)
    assert results[0] == 1
    assert isinstance(results[1], sqlite3.Error)
    assert results[2] == 1

    # Both good rows landed despite the bad one failing.
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM wq_test").fetchone()[0] == 2
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_disable_bypasses_daemon(tmp_path, monkeypatch):
    """M3_WRITE_QUEUE_DISABLE=1 commits inline without creating a daemon."""
    db_path = str(tmp_path / "wq.db")
    _make_table(db_path)
    monkeypatch.setenv("M3_WRITE_QUEUE_DISABLE", "1")
    from memory import db as dbmod

    rc = await dbmod._enqueue_write("INSERT INTO wq_test (v) VALUES (?)", ("a",))
    assert rc == 1
    # No daemon registered when disabled.
    assert dbmod._write_daemons == {}
