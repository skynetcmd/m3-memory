"""reembed_space must handle the vector_kind-LESS embeddings schema.

Regression for `no such column: vector_kind` when the tool was pointed at
agent_chatlog.db (or any pre-partition core DB): its memory_embeddings has only
embed_model/dim. The queries now adapt — a constant 'default' space stands in for
the missing column so a scan/delete works on both schemas.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import reembed_space as r  # noqa: E402


def _make(path, *, with_vector_kind: bool):
    cols = "id INTEGER PRIMARY KEY, memory_id TEXT, embedding BLOB, embed_model TEXT, dim INTEGER"
    if with_vector_kind:
        cols += ", vector_kind TEXT"
    c = sqlite3.connect(path)
    c.execute(f"CREATE TABLE memory_embeddings ({cols})")
    if with_vector_kind:
        c.execute("INSERT INTO memory_embeddings(memory_id, embed_model, dim, vector_kind) "
                  "VALUES ('m1','bge-m3',1024,'default')")
    else:
        c.execute("INSERT INTO memory_embeddings(memory_id, embed_model, dim) "
                  "VALUES ('m1','bge-m3',1024)")
    c.commit()
    return c


def test_detects_missing_vector_kind_and_uses_constant(tmp_path):
    p = str(tmp_path / "chatlog.db")
    conn = _make(p, with_vector_kind=False)
    try:
        assert r._has_vector_kind(conn) is False
        assert r._vector_kind_expr(conn) == "'default'"
        # the adaptive expression must actually run against this schema
        expr = r._vector_kind_expr(conn)
        rows = conn.execute(
            f"SELECT {expr}, COALESCE(embed_model,''), COALESCE(dim,0), COUNT(*) "
            "FROM memory_embeddings GROUP BY 1,2,3"
        ).fetchall()
        assert rows == [("default", "bge-m3", 1024, 1)]
    finally:
        conn.close()


def test_detects_present_vector_kind_and_uses_column(tmp_path):
    p = str(tmp_path / "core.db")
    conn = _make(p, with_vector_kind=True)
    try:
        assert r._has_vector_kind(conn) is True
        assert r._vector_kind_expr(conn) == "COALESCE(vector_kind, 'default')"
    finally:
        conn.close()
