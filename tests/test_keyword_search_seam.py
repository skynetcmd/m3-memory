"""Tests for the keyword_search seam and the tsquery compiler.

Two layers:
  * pure compiler tests (_compile_tsquery) — no DB, assert it mirrors the FTS5
    compiler's structure (same sanitization, parallel operators);
  * SQLite keyword_search behavior against an in-memory FTS5 DB — the seam is a
    faithful extraction of the inline query it replaces.

The live PostgreSQL side is covered by tests/test_postgres_backend_live.py, which
skips without a cluster.
"""
from __future__ import annotations

import sqlite3
import struct

import pytest
from memory.backends import KeywordHit, VectorHit, active_backend
from memory.backends import selector as _selector
from memory.fts import _compile_fts_query, _compile_tsquery


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)  # default sqlite
    _selector._reset_for_tests()
    yield
    _selector._reset_for_tests()


class TestCompileTsquery:
    @pytest.mark.parametrize(
        "query,expected",
        [
            ("postgresql", "postgresql:*"),      # single alnum -> prefix wildcard
            ("core tenets", "core | tenets"),    # multi-token fts5-mode -> OR (|)
            ('"exact phrase"', "exact <-> phrase"),  # quoted -> phrase (<->)
            ("gpt-4o", "gpt | 4o"),              # punctuation split like FTS5
        ],
    )
    def test_shapes(self, query, expected):
        ts, ok = _compile_tsquery(query, "fts5")
        assert ok is True
        assert ts == expected

    @pytest.mark.parametrize("query", ["!!!", "   ", "", "()"])
    def test_no_matchable_tokens_returns_not_ok(self, query):
        ts, ok = _compile_tsquery(query, "fts5")
        assert ok is False
        assert ts == ""

    def test_ok_contract_matches_fts5_compiler(self):
        # For the same input, both compilers must agree on WHETHER there are
        # matchable tokens (the `ok` flag), even though the syntax differs.
        for q in ["postgresql", "core tenets", "!!!", "  ", "gpt-4o", '"a b"']:
            _, ok_fts = _compile_fts_query(q, "fts5")
            _, ok_ts = _compile_tsquery(q, "fts5")
            assert ok_fts == ok_ts, f"ok disagreement on {q!r}"


class TestSqliteKeywordSearch:
    @staticmethod
    def _make_fts_db() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE memory_items (
                id TEXT PRIMARY KEY, type TEXT, title TEXT, content TEXT,
                is_deleted INTEGER DEFAULT 0, user_id TEXT DEFAULT '', scope TEXT DEFAULT 'agent'
            );
            CREATE VIRTUAL TABLE memory_items_fts USING fts5(
                title, content, content='memory_items', content_rowid='rowid'
            );
            """
        )
        return conn

    def _insert(self, conn, _id, title, content, is_deleted=0):
        cur = conn.execute(
            "INSERT INTO memory_items (id, title, content, is_deleted) VALUES (?,?,?,?)",
            (_id, title, content, is_deleted),
        )
        conn.execute(
            "INSERT INTO memory_items_fts (rowid, title, content) VALUES (?,?,?)",
            (cur.lastrowid, title, content),
        )
        conn.commit()

    def test_returns_keyword_hits_ordered(self):
        conn = self._make_fts_db()
        self._insert(conn, "a", "postgres tuning", "shared buffers guide")
        self._insert(conn, "b", "misc", "we chose postgres for the warehouse")
        self._insert(conn, "c", "lunch", "tacos and salad")

        backend = active_backend()
        hits = backend.keyword_search(conn, "postgres", limit=10)
        assert all(isinstance(h, KeywordHit) for h in hits)
        ids = {h.memory_id for h in hits}
        assert ids == {"a", "b"}  # 'c' does not match
        # bm25: lower is better; results must be ascending
        scores = [h.score for h in hits]
        assert scores == sorted(scores)

    def test_excludes_deleted(self):
        conn = self._make_fts_db()
        self._insert(conn, "live", "postgres", "x", is_deleted=0)
        self._insert(conn, "dead", "postgres", "x", is_deleted=1)
        hits = active_backend().keyword_search(conn, "postgres", limit=10)
        assert {h.memory_id for h in hits} == {"live"}

    def test_empty_token_query_returns_empty(self):
        conn = self._make_fts_db()
        self._insert(conn, "a", "postgres", "x")
        assert active_backend().keyword_search(conn, "!!!", limit=10) == []

    def test_tenancy_sql_composes(self):
        conn = self._make_fts_db()
        self._insert(conn, "u1", "postgres", "x")
        conn.execute("UPDATE memory_items SET user_id='alice' WHERE id='u1'")
        self._insert(conn, "u2", "postgres", "x")
        conn.execute("UPDATE memory_items SET user_id='bob' WHERE id='u2'")
        conn.commit()
        hits = active_backend().keyword_search(
            conn, "postgres", limit=10,
            tenancy_sql=" AND mi.user_id = ?", tenancy_params=("alice",),
        )
        assert {h.memory_id for h in hits} == {"u1"}


class TestSqliteVectorSearch:
    DIM = 4

    @staticmethod
    def _blob(vec):
        return struct.pack(f"{len(vec)}f", *vec)

    def _make_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE memory_items(
                id TEXT PRIMARY KEY, is_deleted INTEGER DEFAULT 0, user_id TEXT DEFAULT '');
            CREATE TABLE memory_embeddings(
                memory_id TEXT, embedding BLOB, dim INTEGER, embed_model TEXT);
            """
        )
        return conn

    def _seed(self, conn, mid, vec, model="test-model", is_deleted=0):
        conn.execute(
            "INSERT INTO memory_items(id, is_deleted) VALUES (?,?)", (mid, is_deleted)
        )
        conn.execute(
            "INSERT INTO memory_embeddings(memory_id,embedding,dim,embed_model) "
            "VALUES (?,?,?,?)",
            (mid, self._blob(vec), self.DIM, model),
        )
        conn.commit()

    def test_ranks_by_cosine_higher_better(self):
        conn = self._make_db()
        self._seed(conn, "near", [0.9, 0.1, 0.0, 0.0])
        self._seed(conn, "mid", [0.5, 0.5, 0.5, 0.5])
        self._seed(conn, "opp", [-1.0, 0.0, 0.0, 0.0])
        hits = active_backend().vector_search(
            conn, [1.0, 0.0, 0.0, 0.0], limit=10, dim=self.DIM,
            embed_models=("test-model",),
        )
        assert all(isinstance(h, VectorHit) for h in hits)
        ids = [h.memory_id for h in hits]
        assert ids[0] == "near"          # highest cosine first
        assert ids[-1] == "opp"          # opposite last
        # scores descending (higher = better)
        assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)

    def test_excludes_deleted_and_wrong_identity(self):
        conn = self._make_db()
        self._seed(conn, "keep", [1.0, 0.0, 0.0, 0.0])
        self._seed(conn, "deleted", [1.0, 0.0, 0.0, 0.0], is_deleted=1)
        self._seed(conn, "othermodel", [1.0, 0.0, 0.0, 0.0], model="other")
        hits = active_backend().vector_search(
            conn, [1.0, 0.0, 0.0, 0.0], limit=10, dim=self.DIM,
            embed_models=("test-model",),
        )
        assert {h.memory_id for h in hits} == {"keep"}

    def test_empty_candidates_returns_empty(self):
        conn = self._make_db()
        hits = active_backend().vector_search(
            conn, [1.0, 0.0, 0.0, 0.0], limit=10, dim=self.DIM,
            embed_models=("test-model",),
        )
        assert hits == []
