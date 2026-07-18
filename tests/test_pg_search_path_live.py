"""Phase 1b live test: memory_search_scored_impl over the PostgreSQL backend.

Skips cleanly without a reachable cluster. Exercises the PG candidate-fetch path
(search_pg.fetch_candidates_pg: keyword_search + vector_search via the seam) end
to end through the real scored-search function, and asserts the query-aligned,
title-matching item ranks first — the functional contract, backend-independent.
"""
from __future__ import annotations

import asyncio
import os
import struct

import pytest

# Hosts these destructive tests must never touch, supplied via env (comma-
# separated) so no internal infrastructure address is hardcoded in source.
_FORBIDDEN = [
    h.strip() for h in os.environ.get("M3_PG_FORBIDDEN_HOSTS", "").split(",") if h.strip()
]


# Gated by the requires_pg marker (auto-skips when no Postgres reachable).
# pg_dsn() centralizes the M3_PRIMARY_PG_URL > M3_PG_URL precedence — NEVER PG_URL.
from conftest import pg_dsn

pytestmark = pytest.mark.requires_pg
_DSN = pg_dsn()


def _blob(vec):
    return struct.pack(f"{len(vec)}f", *vec)


@pytest.fixture()
def pg_seeded(monkeypatch):
    """Seed 3 deterministic items+embeddings on the PG cluster; backend selected."""
    assert _DSN is not None
    if any(f in _DSN for f in _FORBIDDEN):
        pytest.fail("refusing to run destructive tests against a forbidden host")
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)

    from memory.backends import selector as _selector
    from memory.embed import _compatible_model_names

    from memory import config

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    dim = config.EMBED_DIM
    model = (sorted(_compatible_model_names()) or ["test-model"])[0]

    def unit(i):
        v = [0.0] * dim
        v[i % dim] = 1.0
        return v

    items = [
        ("sp_a", "postgres tuning", "shared buffers and wal", unit(0)),
        ("sp_b", "random notes", "we mentioned postgres once", unit(1)),
        ("sp_c", "lunch", "tacos only", unit(2)),
    ]
    qvec = unit(0)  # aligned with sp_a

    b = PostgresBackend(dsn=_DSN)
    with b.connection() as c:
        cur = c.cursor()
        # Own the schema (other live tests recreate memory_items with different
        # columns): drop and build the full shape including search_vector.
        cur.execute("DROP TABLE IF EXISTS memory_items CASCADE")
        cur.execute(
            """CREATE TABLE memory_items(
                id TEXT PRIMARY KEY, type TEXT DEFAULT 'note', title TEXT, content TEXT,
                metadata_json JSONB, importance DOUBLE PRECISION DEFAULT 0.5,
                is_deleted INTEGER DEFAULT 0, user_id TEXT DEFAULT '',
                scope TEXT DEFAULT 'agent', created_at TIMESTAMPTZ DEFAULT NOW(),
                valid_from TIMESTAMPTZ, valid_to TIMESTAMPTZ, confidence DOUBLE PRECISION,
                search_vector tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('english', coalesce(title,'')),'A') ||
                    setweight(to_tsvector('english', coalesce(content,'')),'B')) STORED)"""
        )
        # Own memory_embeddings too (another test's ensure_schema may have built
        # the full shape with a NOT NULL id PK; this fixture inserts no id).
        cur.execute("DROP TABLE IF EXISTS memory_embeddings CASCADE")
        cur.execute(
            "CREATE TABLE memory_embeddings(memory_id TEXT, embedding BYTEA, "
            "dim BIGINT, embed_model TEXT, vector_kind TEXT DEFAULT 'default')"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sp_sv ON memory_items USING GIN(search_vector)")
        cur.execute("DELETE FROM memory_embeddings WHERE memory_id LIKE 'sp_%'")
        cur.execute("DELETE FROM memory_items WHERE id LIKE 'sp_%'")
        for mid, title, content, vec in items:
            cur.execute(
                "INSERT INTO memory_items(id,type,title,content) VALUES (%s,'note',%s,%s)",
                (mid, title, content),
            )
            cur.execute(
                "INSERT INTO memory_embeddings(memory_id,embedding,dim,embed_model) "
                "VALUES (%s,%s,%s,%s)",
                (mid, _blob(vec), dim, model),
            )
    yield qvec, [i[0] for i in items]
    with b.connection() as c:
        c.cursor().execute("DELETE FROM memory_embeddings WHERE memory_id LIKE 'sp_%'")
        c.cursor().execute("DELETE FROM memory_items WHERE id LIKE 'sp_%'")
    b.close()


def _id(r):
    if isinstance(r, tuple):
        return r[1].get("id") if isinstance(r[1], dict) else r[1]
    return r.get("id") if isinstance(r, dict) else r


def test_pg_memory_write_impl_end_to_end(monkeypatch):
    """memory_write_impl writes a real memory on PG and it reads back — proves the
    full write path (dialected SQL + connection routing + sqlite-compat adapter +
    JSONB empty-metadata normalization) works end to end."""
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)

    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend
    from memory.write import memory_write_impl

    b = PostgresBackend(dsn=_DSN)
    # Own the schema deterministically: other live tests recreate memory_items
    # with varying columns, so drop and build the full shape the write path binds
    # (21 columns) rather than relying on IF NOT EXISTS against a polluted table.
    with b.connection() as c:
        c.cursor().execute("DROP TABLE IF EXISTS memory_items CASCADE")
        c.cursor().execute(
            """CREATE TABLE memory_items(
                id TEXT PRIMARY KEY, type TEXT NOT NULL, title TEXT, content TEXT,
                metadata_json JSONB, agent_id TEXT, model_id TEXT,
                change_agent TEXT DEFAULT 'unknown', importance DOUBLE PRECISION DEFAULT 0.5,
                source TEXT DEFAULT 'agent', origin_device TEXT DEFAULT 'dev',
                user_id TEXT DEFAULT '', scope TEXT DEFAULT 'agent', expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(), valid_from TIMESTAMPTZ, valid_to TIMESTAMPTZ,
                conversation_id TEXT, refresh_on TIMESTAMPTZ, refresh_reason TEXT, variant TEXT,
                content_hash TEXT, confidence DOUBLE PRECISION, is_deleted INTEGER DEFAULT 0)"""
        )
        c.cursor().execute("DELETE FROM memory_items WHERE title LIKE 'e2e_%'")

    try:
        res_normal = asyncio.run(memory_write_impl(
            "note", "e2e content one", title="e2e_normal",
            metadata='{"role":"user"}', embed=False,
        ))
        res_empty = asyncio.run(memory_write_impl(
            "note", "e2e content two", title="e2e_empty",
            metadata="", embed=False,  # JSONB '' trap -> normalized to {}
        ))
        assert isinstance(res_normal, str) and isinstance(res_empty, str)

        with b.connection() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT title, metadata_json FROM memory_items "
                "WHERE title LIKE 'e2e_%' ORDER BY title"
            )
            rows = {r[0]: r[1] for r in cur.fetchall()}
        assert rows["e2e_normal"] == {"role": "user"}
        assert rows["e2e_empty"] == {}  # '' was normalized, not a crash
    finally:
        with b.connection() as c:
            c.cursor().execute("DELETE FROM memory_items WHERE title LIKE 'e2e_%'")
        b.close()


def test_pg_write_supersede_lifecycle_persists(monkeypatch):
    """Full write -> supersede lifecycle on PG persists all side effects.

    Regression: memory_history was missing from the PG schema and _record_history
    used ? placeholders, so on PG the history INSERT raised, aborted the shared
    transaction, and silently rolled back the supersede's is_deleted/edge writes —
    the caller logged success but nothing persisted. This exercises the whole
    chain end-to-end and asserts persistence."""
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)

    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend
    from memory.write import memory_supersede_impl, memory_write_impl

    b = PostgresBackend(dsn=_DSN)
    # Own the schema: other live tests recreate memory_items with a minimal shape
    # (no updated_at etc.), so drop the core tables and let ensure_schema rebuild
    # the full schema — including memory_history/agents/corroborations.
    with b.connection() as c:
        c.cursor().execute(
            "DROP TABLE IF EXISTS memory_history, memory_corroborations, "
            "memory_relationships, memory_embeddings, memory_items CASCADE"
        )
    b._schema_ready = False
    b.ensure_schema()
    with b.connection() as c:
        c.cursor().execute("DELETE FROM memory_items WHERE title LIKE 'lc_%'")

    try:
        r1 = asyncio.run(memory_write_impl(
            "note", "original content", title="lc_original", embed=False
        ))
        old_id = r1.split("Created:", 1)[1].strip().split()[0]
        asyncio.run(memory_supersede_impl(
            old_id, "replacement content", title="lc_replacement", embed=False
        ))

        with b.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT is_deleted FROM memory_items WHERE id = %s", (old_id,))
            assert cur.fetchone()[0] == 1  # old row actually closed
            cur.execute(
                "SELECT count(*) FROM memory_relationships "
                "WHERE to_id = %s AND relationship_type = 'supersedes'",
                (old_id,),
            )
            assert cur.fetchone()[0] == 1  # supersedes edge written
            cur.execute(
                "SELECT count(*) FROM memory_history WHERE memory_id = %s", (old_id,)
            )
            assert cur.fetchone()[0] >= 1  # history event recorded
    finally:
        with b.connection() as conn:
            conn.cursor().execute("DELETE FROM memory_items WHERE title LIKE 'lc_%'")
        b.close()


@pytest.mark.parametrize("mode", ["hybrid", "fts5", "semantic"])
def test_pg_scored_search_ranks_aligned_item_first(pg_seeded, monkeypatch, mode):
    qvec, seeded_ids = pg_seeded

    import memory.embed as emb
    from memory.search import memory_search_scored_impl

    async def fake_embed_for_search(query, embed_fn=None, gate=False):
        return qvec, None

    monkeypatch.setattr(emb, "embed_for_search", fake_embed_for_search)

    rows = asyncio.run(memory_search_scored_impl("postgres", k=5, search_mode=mode))
    ids = [_id(r) for r in rows]
    # sp_a is query-aligned (vector) AND title-matches "postgres" (keyword) — it
    # must rank first in every mode.
    assert ids, f"no results for mode={mode}"
    assert ids[0] == "sp_a", f"mode={mode}: expected sp_a first, got {ids}"
