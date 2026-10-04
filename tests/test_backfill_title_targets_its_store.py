"""m3_chatlog_backfill_title must write the store it was pointed at.

Hazard: an unscoped seam connection follows the process-active store, so a
chatlog pass would rewrite the main store while its pre-write snapshot covers
the chatlog store.
"""
from __future__ import annotations

import os
import sqlite3
import sys

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)


def _store(path):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE memory_items (id TEXT PRIMARY KEY, content TEXT, title TEXT, "
              "is_deleted INTEGER DEFAULT 0, created_at TEXT)")
    c.execute("INSERT INTO memory_items VALUES ('a', ?, NULL, 0, '2026-01-01')",
              ("A first line long enough to become a title\nmore",))
    c.commit()
    c.close()


def _title(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT title FROM memory_items WHERE id='a'").fetchone()[0]
    finally:
        c.close()


def test_chatlog_pass_writes_the_chatlog_store_only(tmp_path, monkeypatch):
    main, chat = tmp_path / "agent_memory.db", tmp_path / "agent_chatlog.db"
    _store(main)
    _store(chat)
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    monkeypatch.setenv("M3_DATABASE", str(main))  # the process-active store
    from memory.backends import selector

    selector._reset_for_tests()
    import m3_chatlog_backfill_title as t

    counters = t._backfill(chat, ("user", "assistant"), 10, 100, None)
    assert counters["updated"] == 1
    assert _title(chat), "the targeted store gets the title"
    assert _title(main) is None, "the active store must be left alone"
    assert os.environ["M3_DATABASE"] == str(main), "the scope must be restored"
