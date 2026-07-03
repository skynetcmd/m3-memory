"""Regression: the cognitive loop checkpoints the WAL at each cycle boundary.

The loop is the heavy writer on agent_memory.db; a co-reader (the MCP memory
server) runs on the same DB. SQLite's passive wal_autocheckpoint BUSY-FAILS
under a concurrent reader, so the WAL grew to its 64 MiB journal_size_limit
ceiling and wedged both writer and reader (2026-07-03: a 32-min memory_search
hang). `_checkpoint_wal` issues an explicit TRUNCATE checkpoint at the cycle
boundary to reset the WAL. It must (a) actually shrink a bloated WAL, and
(b) never raise (fail-safe — a bad path / busy DB must not crash the loop).
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import m3_cognitive_loop as L  # noqa: E402


def _make_wal_db(path: str, rows: int = 500) -> sqlite3.Connection:
    """Create a WAL-mode DB, grow the -wal sidecar, and RETURN an OPEN
    connection. The connection must stay open for the -wal file to persist —
    SQLite auto-checkpoints and removes the sidecar on the LAST connection
    close, which is exactly the concurrent-reader scenario the loop faces
    (the MCP server keeps a connection open, so the WAL never auto-clears)."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    # Disable auto-checkpoint so the WAL actually grows and we can prove the
    # explicit checkpoint is what shrinks it (mirrors the concurrent-reader
    # busy-fail that leaves the WAL un-checkpointed in production).
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, blob TEXT)")
    payload = "x" * 4096
    for _ in range(rows):
        conn.execute("INSERT INTO t (blob) VALUES (?)", (payload,))
    conn.commit()
    return conn


def test_checkpoint_shrinks_wal(tmp_path):
    db = str(tmp_path / "agent_memory.db")
    holder = _make_wal_db(db, rows=500)  # keep open so the -wal persists
    try:
        wal = db + "-wal"
        assert os.path.exists(wal) and os.path.getsize(wal) > 0, "WAL non-empty before checkpoint"

        L._checkpoint_wal(db)

        # TRUNCATE checkpoint resets the WAL file to (near) zero. A concurrent
        # open reader can leave a tiny WAL header, so assert it shrank hard
        # rather than demanding exactly 0 (robust to the busy-header case).
        after = os.path.getsize(wal)
        assert after < 4096, f"WAL should be truncated to ~0 after checkpoint, got {after}"
    finally:
        holder.close()


def test_checkpoint_failsafe_on_bad_path(tmp_path):
    # A non-existent / unwritable path must NOT raise — the loop's heartbeat
    # must survive a failed checkpoint (fail-safe, §3).
    L._checkpoint_wal(str(tmp_path / "does_not_exist" / "nope.db"))
    L._checkpoint_wal(None)  # no path resolvable -> silent no-op, no raise


def test_checkpoint_noop_when_no_db(monkeypatch):
    # When neither an explicit path nor M3_DATABASE resolves, it's a clean no-op.
    monkeypatch.delenv("M3_DATABASE", raising=False)
    L._checkpoint_wal(None)  # must not raise
