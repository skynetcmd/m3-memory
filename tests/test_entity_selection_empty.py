"""Entity-extraction selection must not re-feed processed-but-empty turns.

Bug: _query_eligible_rows treated "already extracted" as "has >=1 row in
memory_item_entities". A turn that legitimately extracts to ZERO entities left no
such row, so it was re-selected and re-sent to the LLM on EVERY run (worst-first
under ORDER BY LENGTH DESC), burning tokens forever. Fix: a processed turn (empty
OR success) is marked status='done' in entity_extraction_queue, and selection
skips done rows. These tests pin that + old-DB tolerance.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import m3_entities as E  # noqa: E402


def _seed(path: str, with_queue: bool = True) -> None:
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE memory_items(id TEXT PRIMARY KEY, type TEXT, title TEXT,
            content TEXT, is_deleted INTEGER DEFAULT 0, variant TEXT,
            conversation_id TEXT, metadata_json TEXT);
        CREATE TABLE memory_item_entities(memory_id TEXT, entity_id TEXT);
        INSERT INTO memory_items(id,type,content) VALUES
            ('empty-turn','note','ok thanks'),
            ('rich-turn','note','Alice met Bob in Paris');
        """
    )
    if with_queue:
        c.execute(
            "CREATE TABLE entity_extraction_queue(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " memory_id TEXT UNIQUE, enqueued_at TEXT, attempts INTEGER DEFAULT 0,"
            " last_error TEXT, last_attempt_at TEXT)"
        )
    c.commit()
    c.close()


def test_processed_empty_turn_not_reselected(tmp_path):
    db = str(tmp_path / "ent.db")
    _seed(db)
    al = ("note",)
    # Both eligible before anything is processed.
    before = {r[0] for r in E._query_eligible_rows(Path(db), al, None, None, True)}
    assert before == {"empty-turn", "rich-turn"}

    # empty-turn processed-empty → status='done'; rich-turn emitted an entity.
    conn = sqlite3.connect(db)
    E._ensure_extraction_status_column(conn)
    conn.execute(
        "INSERT INTO entity_extraction_queue(memory_id,attempts,last_attempt_at,status)"
        " VALUES('empty-turn',1,'now','done')"
    )
    conn.execute("INSERT INTO memory_item_entities VALUES('rich-turn','e1')")
    conn.commit()
    conn.close()

    after = {r[0] for r in E._query_eligible_rows(Path(db), al, None, None, True)}
    assert "empty-turn" not in after, "processed-empty turn was re-selected (bug)"
    assert after == set(), "nothing should remain eligible"


def test_failed_turn_stays_eligible_for_retry(tmp_path):
    db = str(tmp_path / "ent.db")
    _seed(db)
    conn = sqlite3.connect(db)
    E._ensure_extraction_status_column(conn)
    # A failed row (status='failed') must remain eligible — retryable.
    conn.execute(
        "INSERT INTO entity_extraction_queue(memory_id,attempts,last_attempt_at,status)"
        " VALUES('empty-turn',1,'now','failed')"
    )
    conn.commit()
    conn.close()
    eligible = {r[0] for r in E._query_eligible_rows(Path(db), ("note",), None, None, True)}
    assert "empty-turn" in eligible, "failed turn should stay eligible for retry"


def test_ensure_status_column_idempotent(tmp_path):
    db = str(tmp_path / "ent.db")
    _seed(db)
    conn = sqlite3.connect(db)
    E._ensure_extraction_status_column(conn)
    E._ensure_extraction_status_column(conn)  # twice → no error
    cols = [r[1] for r in conn.execute("PRAGMA table_info(entity_extraction_queue)")]
    assert cols.count("status") == 1
    conn.close()


def test_old_db_without_queue_table_degrades(tmp_path):
    # No entity_extraction_queue table at all → selection falls back to
    # entity-presence only, no crash.
    db = str(tmp_path / "old.db")
    _seed(db, with_queue=False)
    eligible = {r[0] for r in E._query_eligible_rows(Path(db), ("note",), None, None, True)}
    assert eligible == {"empty-turn", "rich-turn"}
