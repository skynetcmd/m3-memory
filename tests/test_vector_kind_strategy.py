"""Tests for memory_search_scored_impl's vector_kind_strategy kwarg (v022).

Verifies:
  * Signature exposes vector_kind_strategy with default 'default'.
  * Invalid values raise ValueError.
  * 'default' strategy adds a vector_kind='default' filter to the SQL so
    non-default kinds (e.g. 'enriched') don't produce phantom second rows.
  * 'max' strategy lets all kinds through, then dedupes by memory_id taking
    the row with the highest vector cosine.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


@pytest.mark.asyncio
async def test_signature_exposes_vector_kind_strategy():
    import memory_core

    sig = inspect.signature(memory_core.memory_search_scored_impl)
    assert "vector_kind_strategy" in sig.parameters
    assert sig.parameters["vector_kind_strategy"].default == "default"


@pytest.mark.asyncio
async def test_invalid_strategy_raises(monkeypatch):
    import memory_core

    async def fake_embed(_q):
        return ([0.1, 0.2, 0.3], "stub")
    monkeypatch.setattr(memory_core, "_embed", fake_embed)

    with pytest.raises(ValueError):
        await memory_core.memory_search_scored_impl(
            "hello", vector_kind_strategy="bogus"
        )


@pytest.mark.asyncio
async def test_default_strategy_adds_vector_kind_filter(monkeypatch):
    """With strategy='default' the SQL must constrain me.vector_kind='default'."""
    import memory_core

    async def fake_embed(_q):
        return ([1.0, 0.0, 0.0], "stub")
    monkeypatch.setattr(memory_core, "_embed", fake_embed)

    captured: list[str] = []

    class _FakeCur:
        def fetchall(self): return []
        def fetchone(self): return None

    class _FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            captured.append(sql)
            return _FakeCur()
        def commit(self): pass

    monkeypatch.setattr(memory_core, "_db", lambda: _FakeDB())

    await memory_core.memory_search_scored_impl(
        "anything", search_mode="semantic", vector_kind_strategy="default"
    )
    assert any("me.vector_kind = 'default'" in s for s in captured), captured


@pytest.mark.asyncio
async def test_max_strategy_omits_vector_kind_filter(monkeypatch):
    """With strategy='max' the SQL must NOT pin vector_kind — both kinds
    flow through so we can pick the better-scoring row downstream."""
    import memory_core

    async def fake_embed(_q):
        return ([1.0, 0.0, 0.0], "stub")
    monkeypatch.setattr(memory_core, "_embed", fake_embed)

    captured: list[str] = []

    class _FakeCur:
        def fetchall(self): return []
        def fetchone(self): return None

    class _FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            captured.append(sql)
            return _FakeCur()
        def commit(self): pass

    monkeypatch.setattr(memory_core, "_db", lambda: _FakeDB())

    await memory_core.memory_search_scored_impl(
        "anything", search_mode="semantic", vector_kind_strategy="max"
    )
    assert captured, "expected at least one query"
    assert not any("vector_kind" in s for s in captured), captured


@pytest.mark.asyncio
async def test_max_strategy_dedupes_by_memory_id_taking_best_vector(monkeypatch):
    """When both a 'default' and an 'enriched' vector exist for the same
    memory_id, max-kind keeps the row with the higher cosine similarity.

    We fabricate two rows sharing memory_id='mem1'. The 'enriched' row's
    vector is aligned with the query vector and should win; the 'default'
    row's vector is orthogonal. The returned scored list must contain
    exactly one result for mem1 with the winning vector's score baked in.
    """
    import memory_core
    from embedding_utils import pack

    q_vec = [1.0, 0.0, 0.0]

    async def fake_embed(_q):
        return (q_vec, "stub")
    monkeypatch.setattr(memory_core, "_embed", fake_embed)

    # Orthogonal → cosine ≈ 0 ; aligned → cosine = 1
    default_vec = pack([0.0, 1.0, 0.0])
    enriched_vec = pack([1.0, 0.0, 0.0])

    class _Row(dict):
        def keys(self):
            return list(super().keys())

    rows = [
        _Row({
            "id": "mem1", "content": "hello world", "title": "t", "type": "message",
            "importance": 0.0, "embedding": default_vec, "bm25_score": 0.0,
        }),
        _Row({
            "id": "mem1", "content": "hello world", "title": "t", "type": "message",
            "importance": 0.0, "embedding": enriched_vec, "bm25_score": 0.0,
        }),
    ]

    class _FakeCur:
        def __init__(self, rows): self._rows = rows
        def fetchall(self): return self._rows
        def fetchone(self): return self._rows[0] if self._rows else None

    class _FakeDB:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            return _FakeCur(rows)
        def commit(self): pass

    monkeypatch.setattr(memory_core, "_db", lambda: _FakeDB())

    scored = await memory_core.memory_search_scored_impl(
        "hello", search_mode="semantic", vector_kind_strategy="max",
        explain=True,
    )
    # Exactly one entry for mem1, and it should carry the aligned vector's score.
    assert len(scored) == 1, scored
    score, item = scored[0]
    assert item["id"] == "mem1"
    # vector contribution should be the max (≈1.0), not the loser (≈0.0).
    assert item["_explanation"]["vector"] > 0.9, item["_explanation"]
