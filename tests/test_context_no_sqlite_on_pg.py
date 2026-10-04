"""Building an M3Context off SQLite must not create a SQLite store.

Hazard: memory_core binds a default context at import; an eager SQLite pool
would create an empty agent_memory.db in the engine root of a PostgreSQL
install.
"""
from __future__ import annotations

import os
import sys

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from m3_core.context import M3Context, _close_context_pool  # noqa: E402


def test_no_store_file_until_sqlite_is_used(monkeypatch, tmp_path):
    db = tmp_path / "agent_memory.db"
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    ctx = M3Context(str(db))
    try:
        assert not db.exists()
        # A SQLite-only caller still gets a working connection on demand.
        with ctx.get_sqlite_conn() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        assert db.exists()
    finally:
        _close_context_pool(ctx)


def test_sqlite_backend_still_opens_eagerly(monkeypatch, tmp_path):
    db = tmp_path / "agent_memory.db"
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    monkeypatch.delenv("DB_BACKEND", raising=False)
    ctx = M3Context(str(db))
    try:
        assert ctx._pool is not None
    finally:
        _close_context_pool(ctx)
