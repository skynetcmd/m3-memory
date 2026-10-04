"""memory_link_impl(db=conn) writes on the caller's connection in PRODUCTION.

Passing `db=` routed the connection through `_db(db)`. Only test fakes accept an
argument; the canonical `_db()` takes none, so every production caller that
shares its transaction — distillation provenance and belief consolidation —
raised `TypeError: _db() takes 0 positional arguments but 1 was given` AFTER its
memory write had landed. Observed 2026-10-04: each loop cycle wrote another copy
of the same distilled procedure (superseding the last) and crashed before
marking the task done.

No `_db` override is installed here: this exercises the real wrapper.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def test_link_with_a_shared_connection_uses_it(tmp_path, monkeypatch):
    from conftest import create_full_main_schema
    from memory import write as W

    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    db_path = tmp_path / "t.db"
    create_full_main_schema(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        out = W.memory_link_impl("a" * 8, "b" * 8, "distills_from", db=conn)
        conn.commit()
        assert out.startswith("Linked"), out
        n = conn.execute(
            "SELECT COUNT(*) FROM memory_relationships "
            "WHERE from_id=? AND to_id=? AND relationship_type='distills_from'",
            ("a" * 8, "b" * 8),
        ).fetchone()[0]
        assert n == 1, "the link must land on the caller's connection"
    finally:
        conn.close()
