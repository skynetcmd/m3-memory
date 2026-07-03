"""Zero-lag classification: writes defer LLM classify; a sweep resolves it.

memory_write with auto_classify no longer blocks the write on an LLM call
(zero-lag, §3/§8) — it persists the row as type='auto' and the cognitive loop's
classification sweep resolves the real type later, fail-open (a down/slow LLM
leaves the row 'auto', retried next sweep — never lost, never blocking).
"""
import asyncio
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import m3_cognitive_loop as L  # noqa: E402


def _mk_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memory_items (id TEXT PRIMARY KEY, type TEXT, content TEXT, "
        "title TEXT, is_deleted INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO memory_items (id, type, content, title, created_at) "
        "VALUES ('r1', 'auto', 'a decision was made', '', '2026-01-01')"
    )
    conn.commit()
    conn.close()


class _Args:
    def __init__(self, db):
        self.database = db
        self.limit_per_pass = 5


def test_has_classify_work_detects_auto_rows(tmp_path):
    db = str(tmp_path / "m.db")
    _mk_db(db)
    assert L.has_classify_work(db) is True
    # after there are no auto rows, the gate is False
    c = sqlite3.connect(db)
    c.execute("UPDATE memory_items SET type='decision' WHERE id='r1'")
    c.commit()
    c.close()
    assert L.has_classify_work(db) is False


def test_sweep_resolves_type(tmp_path):
    db = str(tmp_path / "m.db")
    _mk_db(db)

    async def fake_classify(content, title):
        return "decision"

    with patch("memory.enrich._auto_classify", side_effect=fake_classify):
        asyncio.run(L.run_classify_pass(_Args(db)))

    c = sqlite3.connect(db)
    typ = c.execute("SELECT type FROM memory_items WHERE id='r1'").fetchone()[0]
    c.close()
    assert typ == "decision"


def test_sweep_failopen_leaves_auto_on_llm_timeout(tmp_path):
    db = str(tmp_path / "m.db")
    _mk_db(db)

    async def hang_classify(content, title):
        await asyncio.sleep(60)  # simulate a wedged/slow LLM
        return "decision"

    # Tight deadline so the test is fast; the row must stay 'auto', no raise.
    with patch.dict("os.environ", {"M3_CLASSIFY_DEADLINE_S": "0.2"}), \
         patch("memory.enrich._auto_classify", side_effect=hang_classify):
        asyncio.run(L.run_classify_pass(_Args(db)))

    c = sqlite3.connect(db)
    typ = c.execute("SELECT type FROM memory_items WHERE id='r1'").fetchone()[0]
    c.close()
    assert typ == "auto", "LLM timeout must leave the row as 'auto' (fail-open, retried next sweep)"


def test_sweep_noop_when_no_work(tmp_path):
    db = str(tmp_path / "m.db")
    _mk_db(db)
    c = sqlite3.connect(db)
    c.execute("UPDATE memory_items SET type='note' WHERE id='r1'")
    c.commit()
    c.close()
    # No auto rows -> pass returns immediately, no error.
    asyncio.run(L.run_classify_pass(_Args(db)))
