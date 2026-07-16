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


def _dsn():
    return (os.environ.get("M3_PG_URL") or os.environ.get("PG_URL") or "").strip() or None


def _reachable(dsn):
    try:
        import psycopg2

        psycopg2.connect(dsn, connect_timeout=3).close()
        return True
    except Exception:
        return False


_DSN = _dsn()
pytestmark = pytest.mark.skipif(
    _DSN is None or not _reachable(_DSN),
    reason="no reachable PostgreSQL (set M3_PG_URL to a throwaway cluster)",
)


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
        cur.execute(
            """CREATE TABLE IF NOT EXISTS memory_items(
                id TEXT PRIMARY KEY, type TEXT DEFAULT 'note', title TEXT, content TEXT,
                metadata_json JSONB, importance DOUBLE PRECISION DEFAULT 0.5,
                is_deleted INTEGER DEFAULT 0, user_id TEXT DEFAULT '',
                scope TEXT DEFAULT 'agent', created_at TIMESTAMPTZ DEFAULT NOW(),
                valid_from TIMESTAMPTZ, valid_to TIMESTAMPTZ, confidence DOUBLE PRECISION,
                search_vector tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('english', coalesce(title,'')),'A') ||
                    setweight(to_tsvector('english', coalesce(content,'')),'B')) STORED)"""
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS memory_embeddings(memory_id TEXT, embedding BYTEA, "
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
