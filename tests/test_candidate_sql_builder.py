"""One builder for the SQLite fused candidate-fetch SQL, four variants.

`memory/search.py` assembled this SQL inline in FOUR places — semantic and
hybrid, each × sqlite-vec present/absent. That is the duplication shape that
produced the 2026-07-26 filter bug: row-scoping predicates lived in four
hand-maintained copies, a tenancy fix reached all four, type_filter/agent_filter
reached exactly one, and filtered searches silently returned unfiltered rows on
both backends. Sharing a PREDICATE is not the same as having one code path.

WHY THESE TESTS ARE TEXTUAL. `build_candidate_sql` is pure (str in, str out), so
the extraction is PROVABLE: `test_matches_pre_extraction_sql_byte_for_byte`
holds the four original strings verbatim and asserts the builder reproduces
them. That matters most for the two `vec_distance_cosine` variants, which do NOT
run on a box without the sqlite-vec extension — byte-identity is the strongest
guarantee available for code that cannot be executed here.

sqlite-vec is an ACCELERATOR, not the vector engine. When it is absent the same
cosine similarity is computed by the Rust core (`cosine_batch_packed`) from the
raw blobs the query returns either way — verified equal to a reference cosine to
~3e-09. The variants differ in WHERE the arithmetic happens and how rows are
ordered/limited in SQL, not in what similarity means.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory.search_routing import build_candidate_sql  # noqa: E402

WHERE = "mi.is_deleted = 0 AND mi.user_id = ?"
EXTRA = ", mi.scope"
LIMIT = 400


def _orig(mode: str, has_vec: bool) -> str:
    """The SQL exactly as it appeared inline in search.py before extraction."""
    if mode == "semantic" and has_vec:
        return f"""
                            WITH limited AS (
                                SELECT mi.id FROM memory_items mi
                                WHERE {WHERE}
                                LIMIT 50
                            )
                            SELECT mi.id, mi.content, mi.title, mi.type, mi.importance, me.embedding, 0.0 as bm25_score,
                                   (1.0 - vec_distance_cosine(me.embedding, ?)) as vec_score{EXTRA}
                            FROM memory_items mi
                            JOIN memory_embeddings me ON mi.id = me.memory_id
                            WHERE mi.id IN (SELECT id FROM limited)
                            ORDER BY vec_score DESC
                        """
    if mode == "semantic":
        return f"""
                            WITH limited AS (
                                SELECT mi.id FROM memory_items mi
                                WHERE {WHERE}
                                LIMIT 50
                            )
                            SELECT mi.id, mi.content, mi.title, mi.type, mi.importance, me.embedding, 0.0 as bm25_score{EXTRA}
                            FROM memory_items mi
                            JOIN memory_embeddings me ON mi.id = me.memory_id
                            WHERE mi.id IN (SELECT id FROM limited)
                            ORDER BY mi.created_at DESC
                        """
    if has_vec:
        return f"""
                        SELECT mi.id, mi.content, mi.title, mi.type, mi.importance, me.embedding,
                               bm25(memory_items_fts) as bm25_score,
                               (1.0 - vec_distance_cosine(me.embedding, ?)) as vec_score{EXTRA}
                        FROM memory_items mi
                        JOIN memory_embeddings me ON mi.id = me.memory_id
                        JOIN memory_items_fts fts ON mi.rowid = fts.rowid
                        WHERE {WHERE} AND memory_items_fts MATCH ?
                        ORDER BY bm25_score ASC
                        LIMIT {LIMIT}
                    """
    return f"""
                        SELECT mi.id, mi.content, mi.title, mi.type, mi.importance, me.embedding,
                               bm25(memory_items_fts) as bm25_score{EXTRA}
                        FROM memory_items mi
                        JOIN memory_embeddings me ON mi.id = me.memory_id
                        JOIN memory_items_fts fts ON mi.rowid = fts.rowid
                        WHERE {WHERE} AND memory_items_fts MATCH ?
                        ORDER BY bm25_score ASC
                        LIMIT {LIMIT}
                    """


@pytest.mark.parametrize(
    "mode,has_vec",
    [("semantic", True), ("semantic", False), ("hybrid", True), ("hybrid", False)],
)
def test_matches_pre_extraction_sql_byte_for_byte(mode, has_vec):
    """The load-bearing assertion: extraction changed no SQL, in any variant."""
    got = build_candidate_sql(
        search_mode=mode, has_vec=has_vec, where_sql=WHERE,
        extra_sql=EXTRA, row_limit=LIMIT,
    )
    assert got == _orig(mode, has_vec)


class TestVariantShape:
    def test_vec_variants_score_in_sql_others_do_not(self):
        """has_vec is exactly what decides SQL-side cosine."""
        for mode in ("semantic", "hybrid"):
            with_vec = build_candidate_sql(
                search_mode=mode, has_vec=True, where_sql=WHERE, row_limit=LIMIT
            )
            without = build_candidate_sql(
                search_mode=mode, has_vec=False, where_sql=WHERE, row_limit=LIMIT
            )
            assert "vec_distance_cosine" in with_vec
            assert "vec_distance_cosine" not in without
            # Both must still return the raw blob — without it the Rust cosine
            # has nothing to score, which would silently zero out the no-vec path.
            assert "me.embedding" in with_vec and "me.embedding" in without

    def test_semantic_has_no_fts_and_hybrid_does(self):
        sem = build_candidate_sql(
            search_mode="semantic", has_vec=False, where_sql=WHERE
        )
        hyb = build_candidate_sql(
            search_mode="hybrid", has_vec=False, where_sql=WHERE, row_limit=LIMIT
        )
        assert "memory_items_fts" not in sem
        assert "memory_items_fts MATCH ?" in hyb

    def test_semantic_limits_items_via_cte_not_the_join(self):
        """The CTE exists so LIMIT 50 counts ITEMS, not item x embedding rows.

        Flattening it into the outer query would silently change how many
        distinct memories reach the ranker whenever an item has >1 embedding.
        """
        sql = build_candidate_sql(
            search_mode="semantic", has_vec=False, where_sql=WHERE
        )
        assert "WITH limited AS" in sql
        cte = sql[sql.index("WITH limited AS"): sql.index(")", sql.index("LIMIT 50"))]
        assert "JOIN" not in cte, "LIMIT must apply before the embedding join"

    @pytest.mark.parametrize("mode", ["hybrid", "fts5"])
    def test_non_semantic_modes_take_the_hybrid_shape(self, mode):
        """Only "semantic" selects the vector-only shape; fts5 is hybrid-shaped."""
        sql = build_candidate_sql(
            search_mode=mode, has_vec=False, where_sql=WHERE, row_limit=LIMIT
        )
        assert "memory_items_fts MATCH ?" in sql
        assert "WITH limited AS" not in sql

    def test_where_sql_is_interpolated_verbatim(self):
        """Scoping predicates come from Dialect.scope_predicates and must not be
        rewritten here — the builder owns the query SHAPE, not the predicates."""
        marker = "mi.type = ? AND mi.agent_id = ?"
        for mode, hv in [("semantic", True), ("semantic", False),
                         ("hybrid", True), ("hybrid", False)]:
            sql = build_candidate_sql(
                search_mode=mode, has_vec=hv, where_sql=marker, row_limit=LIMIT
            )
            assert marker in sql

    def test_row_limit_applied_to_hybrid_only(self):
        hyb = build_candidate_sql(
            search_mode="hybrid", has_vec=False, where_sql=WHERE, row_limit=17
        )
        assert "LIMIT 17" in hyb
        sem = build_candidate_sql(
            search_mode="semantic", has_vec=False, where_sql=WHERE
        )
        assert "LIMIT 50" in sem and "LIMIT 17" not in sem


class TestSqlIsValid:
    """Every variant must PARSE as SQLite, including the ones that cannot run.

    sqlite-vec/FTS5 functions are undefined without their extensions, so the
    statements cannot be executed here — but `EXPLAIN` still catches a syntax
    error, which is the failure mode a text-assembly bug actually produces.
    """

    @pytest.mark.parametrize(
        "mode,has_vec",
        [("semantic", True), ("semantic", False),
         ("hybrid", True), ("hybrid", False)],
    )
    def test_parses(self, mode, has_vec):
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE memory_items (
                id TEXT PRIMARY KEY, type TEXT, title TEXT, content TEXT,
                importance REAL, is_deleted INTEGER DEFAULT 0,
                user_id TEXT DEFAULT '', scope TEXT DEFAULT 'agent',
                created_at TEXT
            );
            CREATE TABLE memory_embeddings (memory_id TEXT, embedding BLOB);
            CREATE VIRTUAL TABLE memory_items_fts USING fts5(
                title, content, content='memory_items', content_rowid='rowid'
            );
            """
        )
        sql = build_candidate_sql(
            search_mode=mode, has_vec=has_vec, where_sql=WHERE,
            extra_sql=EXTRA, row_limit=LIMIT,
        )
        try:
            conn.execute("EXPLAIN " + sql, ["x"] * sql.count("?"))
        except sqlite3.OperationalError as e:
            # "no such function: vec_distance_cosine" is EXPECTED without the
            # extension; a syntax error is not.
            msg = str(e)
            assert "no such function" in msg, f"{mode}/has_vec={has_vec}: {msg}"
        finally:
            conn.close()
