"""Phase 5 contract tests — confidence in retrieval ranking.

The zero-regression contract (DESIGN_PHILOSOPHIES §5/§11): with
M3_CONFIDENCE_RANKING OFF (default), ranking is byte-identical regardless of any
stored confidence. With it ON, confidence is an additive term that can break ties
toward better-corroborated facts.

Drives memory_search_scored_impl against a real full-schema DB with seeded rows +
embeddings (query embedder stubbed for determinism).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from embedding_utils import pack as _pack  # noqa: E402


@pytest.fixture(autouse=True)
def _skip_migrations(monkeypatch):
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")


def _full_db(db_path):
    from conftest import create_full_main_schema
    create_full_main_schema(db_path)


# Two unit vectors with EMBED_DIM dims so the packed-blob fast path is well-formed.
def _vec(primary: float, dim: int):
    v = [0.0] * dim
    v[0] = primary
    v[1] = (1.0 - primary * primary) ** 0.5
    return v


def _seed(conn, mid, content, vec, confidence, *, importance=0.5):
    conn.execute(
        "INSERT INTO memory_items (id, type, title, content, source, change_agent, "
        "created_at, importance, confidence, is_deleted) VALUES (?,?,?,?,?,?,?,?,?,0)",
        (mid, "fact", content[:20], content, "agent", "claude",
         "2026-01-01T00:00:00Z", importance, confidence),
    )
    conn.execute(
        "INSERT INTO memory_embeddings (memory_id, embedding, embed_model, dim) VALUES (?,?,?,?)",
        (mid, _pack(vec), "test", len(vec)),
    )


def _patch(monkeypatch, db_path, qvec):
    import memory_core

    @contextmanager
    def fake_db(existing=None, *a, **k):
        if existing is not None:
            yield existing
            return
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    async def fake_embed(_q):
        return (qvec, "test-embed")

    monkeypatch.setattr(memory_core, "_db", fake_db)
    monkeypatch.setattr(memory_core, "_embed", fake_embed)
    return memory_core


async def _ranked_ids(mc, **kw):
    res = await mc.memory_search_scored_impl(
        query="anything", k=5, mmr=False, search_mode="vector", **kw
    )
    # scored impl returns a list of (score, item) or item dicts depending on path;
    # normalize to a list of ids in rank order.
    out = []
    for r in res:
        if isinstance(r, tuple):
            out.append(r[1]["id"])
        elif isinstance(r, dict):
            out.append(r["id"])
    return out


@pytest.mark.asyncio
async def test_flag_off_ranking_ignores_confidence(monkeypatch, tmp_path):
    """The contract: with the flag OFF, two rows of equal relevance rank by the
    existing signals only — their confidence is irrelevant to order and score."""
    from memory.config import EMBED_DIM
    db = tmp_path / "t.db"
    _full_db(db)
    qv = _vec(1.0, EMBED_DIM)
    with sqlite3.connect(str(db)) as conn:
        # Two rows, identical relevance to the query (same vector), DIFFERENT
        # confidence. Flag off => confidence must not change anything.
        _seed(conn, "lowconf", "alpha fact one", qv, confidence=0.10)
        _seed(conn, "hiconf", "alpha fact two", qv, confidence=0.99)
        conn.commit()

    monkeypatch.delenv("M3_CONFIDENCE_RANKING", raising=False)
    import memory.config as cfg
    monkeypatch.setattr(cfg, "CONFIDENCE_RANKING", False)
    mc = _patch(monkeypatch, db, qv)

    res = await mc.memory_search_scored_impl(query="x", k=5, mmr=False, search_mode="vector")
    scores = {r[1]["id"] if isinstance(r, tuple) else r["id"]:
              (r[0] if isinstance(r, tuple) else None) for r in res}
    # Both present; with the flag off their scores are equal (confidence ignored).
    assert "lowconf" in scores and "hiconf" in scores
    if scores["lowconf"] is not None and scores["hiconf"] is not None:
        assert scores["lowconf"] == pytest.approx(scores["hiconf"]), (
            "flag OFF must not let confidence change the score")


@pytest.mark.asyncio
async def test_flag_on_confidence_breaks_ties(monkeypatch, tmp_path):
    """With the flag ON, the higher-confidence row of an equal-relevance pair
    ranks first."""
    from memory.config import EMBED_DIM
    db = tmp_path / "t.db"
    _full_db(db)
    qv = _vec(1.0, EMBED_DIM)
    with sqlite3.connect(str(db)) as conn:
        _seed(conn, "lowconf", "alpha fact one", qv, confidence=0.10)
        _seed(conn, "hiconf", "alpha fact two", qv, confidence=0.99)
        conn.commit()

    import memory.config as cfg
    monkeypatch.setattr(cfg, "CONFIDENCE_RANKING", True)
    monkeypatch.setattr(cfg, "CONFIDENCE_WEIGHT", 0.5)  # exaggerate for a clear gap
    mc = _patch(monkeypatch, db, qv)

    ids = await _ranked_ids(mc)
    assert ids[0] == "hiconf", f"flag ON: high-confidence row should rank first; got {ids}"


@pytest.mark.asyncio
async def test_flag_on_null_confidence_falls_back_to_importance(monkeypatch, tmp_path):
    """A row with NULL confidence must not be penalized — it falls back to its
    importance, so an un-derived legacy row is unaffected."""
    from memory.config import EMBED_DIM
    db = tmp_path / "t.db"
    _full_db(db)
    qv = _vec(1.0, EMBED_DIM)
    with sqlite3.connect(str(db)) as conn:
        _seed(conn, "nullconf", "alpha fact one", qv, confidence=None, importance=0.9)
        _seed(conn, "lowconf", "alpha fact two", qv, confidence=0.1, importance=0.1)
        conn.commit()

    import memory.config as cfg
    monkeypatch.setattr(cfg, "CONFIDENCE_RANKING", True)
    monkeypatch.setattr(cfg, "CONFIDENCE_WEIGHT", 0.5)
    mc = _patch(monkeypatch, db, qv)

    ids = await _ranked_ids(mc)
    # nullconf falls back to importance 0.9 > lowconf's 0.1 -> ranks first.
    assert ids[0] == "nullconf", f"NULL confidence should use importance; got {ids}"
