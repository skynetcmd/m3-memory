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
from memory.backends import active_backend
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
        # Structural contract, not class identity: conftest's module-restore can
        # reimport memory.backends.base, making a second KeywordHit class object
        # so `isinstance` false-fails under cross-file ordering. The seam's
        # promise is the SHAPE (memory_id + score), which survives a reload.
        assert all(hasattr(h, "memory_id") and hasattr(h, "score") for h in hits)
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


class TestTsqueryModeMirrorsFts5:
    """`search_mode` must change the tsquery, mirroring FTS5's own operators.

    REGRESSION (2026-07-26). _compile_tsquery's unquoted multi-token path fell
    through to `" | ".join(toks)` in BOTH modes — the same OR as fts5 mode — so
    search_mode was accepted and SILENTLY IGNORED on PostgreSQL while SQLite
    honoured it. Verified live against PG 16.14 before and after.

    These assert the OPERATOR each mode compiles to, and thus set MEMBERSHIP
    (which documents match). They deliberately do NOT assert cross-backend row
    order or scores: PG has no bm25, so ts_rank + m3's fuzzy bm25-like handling
    ranks differently BY DESIGN. Order is not the contract; the matched set is.
    """

    @pytest.mark.parametrize(
        "query,mode,fts5_op,ts_op",
        [
            # fts5 mode: OR / |
            ("portable m3 command", "fts5", " OR ", " | "),
            ("core tenets", "fts5", " OR ", " | "),
            # hybrid: FTS5 leaves tokens space-separated == IMPLICIT AND; the
            # tsquery analogue is & (NOT <->, which is stricter adjacency).
            ("portable m3 command", "hybrid", " ", " & "),
            ("core tenets", "hybrid", " ", " & "),
        ],
    )
    def test_operator_per_mode(self, query, mode, fts5_op, ts_op):
        fts, ok_f = _compile_fts_query(query, mode)
        ts, ok_t = _compile_tsquery(query, mode)
        assert ok_f is ok_t is True
        assert fts_op_present(fts, fts5_op), f"fts5 {mode}: {fts!r}"
        assert ts_op in ts, f"tsquery {mode}: {ts!r}"

    def test_modes_actually_differ_on_postgres(self):
        """The bug in one line: hybrid and fts5 must NOT compile identically."""
        q = "portable m3 command"
        assert _compile_tsquery(q, "hybrid")[0] != _compile_tsquery(q, "fts5")[0]

    def test_quoted_phrase_uses_adjacency_not_and(self):
        """Adjacency stays reserved for the explicitly quoted branch."""
        ts, ok = _compile_tsquery('"portable m3 command"', "hybrid")
        assert ok and ts == "portable <-> m3 <-> command"
        # ...and FTS5 likewise keeps the quotes, rather than AND-ing.
        assert _compile_fts_query('"portable m3 command"', "hybrid")[0].startswith('"')

    def test_single_token_wildcards_in_both_modes(self):
        """Single-token behaviour is mode-independent and must stay so."""
        for mode in ("fts5", "hybrid"):
            assert _compile_tsquery("solo", mode)[0] == "solo:*"
            assert _compile_fts_query("solo", mode)[0] == "solo*"


def fts_op_present(compiled: str, op: str) -> bool:
    """FTS5 hybrid mode joins with a bare space, so ' ' means 'no OR/AND word'."""
    if op == " ":
        return " " in compiled and " OR " not in compiled
    return op in compiled


class TestKeywordSearchWithRowDataMatchMode:
    """``search_mode`` must reach the compiler — the phrase/OR distinction.

    REGRESSION (2026-07-26). ``keyword_search_with_row_data`` originally
    hardcoded the "fts5" (OR) compile. The search.py exact-substring
    short-circuit, which RETURNS EARLY with no ranking downstream, then had its
    phrase hits buried under single-token matches: a parity sweep showed 0 exact
    hits at limit=20 but 1 at limit=160 — NON-MONOTONIC in the caller's k, so a
    user asking for MORE results got FEWER. The mode is now a parameter.

    Asserted at the seam rather than through search.py because the failure is a
    property of candidate ORDER under a limit, which needs a controlled corpus:
    filler rows that match single tokens must outrank the phrase row.
    """

    def _corpus(self):
        # Local schema, not TestSqliteKeywordSearch._make_fts_db: that fixture
        # predates this method and has no `importance` column, which
        # keyword_search_with_row_data projects as a base column.
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE memory_items (
                id TEXT PRIMARY KEY, type TEXT, title TEXT, content TEXT,
                importance REAL DEFAULT 0.5, is_deleted INTEGER DEFAULT 0,
                user_id TEXT DEFAULT '', scope TEXT DEFAULT 'agent'
            );
            CREATE VIRTUAL TABLE memory_items_fts USING fts5(
                title, content, content='memory_items', content_rowid='rowid'
            );
            """
        )

        def ins(conn, _id, title, content):
            cur = conn.execute(
                "INSERT INTO memory_items (id, title, content) VALUES (?,?,?)",
                (_id, title, content),
            )
            conn.execute(
                "INSERT INTO memory_items_fts (rowid, title, content) VALUES (?,?,?)",
                (cur.lastrowid, title, content),
            )
            conn.commit()

        # The one row containing the exact phrase.
        ins(conn, "phrase", "notes", "the portable m3 command runs anywhere")
        # Filler matching individual tokens only. Short docs score BETTER under
        # bm25, so these crowd out the phrase row when compiled as OR.
        for n in range(30):
            ins(conn, f"f{n}", "portable", "portable")
        return conn

    def test_fts5_mode_admits_rows_without_the_phrase(self):
        """OR compile returns single-token matches; phrase compile must not.

        This is the property that makes "fts5" wrong for an early-returning
        exact-substring caller: the candidate list it competes in is padded
        with rows that do not contain the phrase at all.
        """
        conn = self._corpus()
        rows = active_backend().keyword_search_with_row_data(
            conn, "portable m3 command", limit=5, search_mode="fts5",
        )
        assert len(rows) == 5, "OR compile should fill the limit with filler"
        non_phrase = [r["id"] for r in rows if "portable m3 command" not in r["content"]]
        assert non_phrase, "OR compile must admit rows lacking the phrase"

    def test_hybrid_mode_keeps_the_phrase_row_first(self):
        conn = self._corpus()
        rows = active_backend().keyword_search_with_row_data(
            conn, "portable m3 command", limit=5, search_mode="hybrid",
        )
        assert [r["id"] for r in rows] == ["phrase"], (
            "phrase compile must match ONLY the row containing the phrase"
        )

    def test_phrase_recall_is_monotonic_in_limit(self):
        """The actual defect: more budget must never yield fewer phrase hits."""
        conn = self._corpus()
        backend = active_backend()
        counts = []
        for limit in (2, 5, 20, 50):
            rows = backend.keyword_search_with_row_data(
                conn, "portable m3 command", limit=limit, search_mode="hybrid",
            )
            counts.append(sum(1 for r in rows if "portable m3 command" in r["content"]))
        assert counts == sorted(counts), f"non-monotonic phrase recall: {counts}"
        assert counts[-1] >= 1

    def test_default_mode_is_the_broad_or_form(self):
        """Callers that don't pass search_mode keep ranked-recall semantics."""
        conn = self._corpus()
        backend = active_backend()
        default = backend.keyword_search_with_row_data(
            conn, "portable m3 command", limit=5,
        )
        explicit = backend.keyword_search_with_row_data(
            conn, "portable m3 command", limit=5, search_mode="fts5",
        )
        assert [r["id"] for r in default] == [r["id"] for r in explicit]


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
        # Structural contract, not class identity (see keyword test note).
        assert all(hasattr(h, "memory_id") and hasattr(h, "score") for h in hits)
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
