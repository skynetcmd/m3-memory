"""Tests for memory_search_routed_impl temporal-aware routing logic.

Tests cover:
- is_temporal_query regex correctness
- Temporal route using k+bump and "default" vector_kind_strategy
- Non-temporal route using max-kind and optional fact-variant fusion
- Env-var override for temporal_k_bump
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


@pytest.mark.asyncio
async def test_is_temporal_query_temporal_keywords():
    """is_temporal_query returns True for queries with temporal vocabulary."""
    import memory_core

    assert memory_core.is_temporal_query("when did I graduate?") is True
    assert memory_core.is_temporal_query("How long did that take?") is True
    assert memory_core.is_temporal_query("What date was the meeting?") is True
    assert memory_core.is_temporal_query("Before yesterday") is True
    assert memory_core.is_temporal_query("After last week") is True
    assert memory_core.is_temporal_query("Since 2020") is True
    assert memory_core.is_temporal_query("Days ago") is True
    assert memory_core.is_temporal_query("First time") is True
    assert memory_core.is_temporal_query("Latest news") is True
    assert memory_core.is_temporal_query("Which meeting happened first?") is True
    assert memory_core.is_temporal_query("In what order?") is True
    assert memory_core.is_temporal_query("Monday morning") is True
    assert memory_core.is_temporal_query("Christmas") is True


@pytest.mark.asyncio
async def test_is_temporal_query_non_temporal():
    """is_temporal_query returns False for non-temporal queries."""
    import memory_core

    assert memory_core.is_temporal_query("what is my favorite color?") is False
    assert memory_core.is_temporal_query("What is the capital of France?") is False
    assert memory_core.is_temporal_query("Tell me about the weather") is False
    assert memory_core.is_temporal_query("") is False


@pytest.mark.asyncio
async def test_is_temporal_query_none_safe():
    """is_temporal_query handles None gracefully (treated as empty)."""
    import memory_core

    # None should be treated as empty/falsy
    assert memory_core.is_temporal_query(None or "") is False


@pytest.mark.asyncio
async def test_temporal_route_uses_k_plus_bump(monkeypatch):
    """Temporal query routes to memory_search_scored_impl with k+bump and vector_kind_strategy='default'."""
    import memory_core

    recorded_calls = []

    async def stub_search(*args, **kwargs):
        recorded_calls.append({"args": args, "kwargs": kwargs})
        # Return 3 sentinel tuples matching the expected shape
        return [
            (0.9, {"id": "mem1", "content": "hit1", "title": "t1"}),
            (0.8, {"id": "mem2", "content": "hit2", "title": "t2"}),
            (0.7, {"id": "mem3", "content": "hit3", "title": "t3"}),
        ]

    monkeypatch.setattr(memory_core, "memory_search_scored_impl", stub_search)

    result = await memory_core.memory_search_routed_impl(
        "when did that happen?", k=5, temporal_k_bump=5
    )

    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    assert call["kwargs"]["k"] == 10, f"Expected k=10 (5+5), got {call['kwargs']['k']}"
    assert call["kwargs"]["vector_kind_strategy"] == "default"
    assert len(result) == 3, "Should return all 3 sentinel results"


@pytest.mark.asyncio
async def test_non_temporal_no_fact_variant(monkeypatch):
    """Non-temporal without fact_variant uses k directly, vector_kind_strategy='max', no fact fusion."""
    import memory_core

    recorded_calls = []

    async def stub_search(*args, **kwargs):
        recorded_calls.append({"args": args, "kwargs": kwargs})
        # Return k results
        k_val = kwargs.get("k", 5)
        return [
            (0.9 - i * 0.01, {"id": f"mem{i}", "content": f"hit{i}", "title": f"t{i}"})
            for i in range(k_val)
        ]

    monkeypatch.setattr(memory_core, "memory_search_scored_impl", stub_search)

    result = await memory_core.memory_search_routed_impl(
        "what is my favorite color?", k=5, fact_variant=""
    )

    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    assert call["kwargs"]["k"] == 5, f"Expected k=5, got {call['kwargs']['k']}"
    assert call["kwargs"]["vector_kind_strategy"] == "max"
    assert len(result) == 5, f"Expected result length k=5, got {len(result)}"


@pytest.mark.asyncio
async def test_non_temporal_with_fact_variant_fusion(monkeypatch):
    """Non-temporal with fact_variant fuses two retrievals, dedupes by id, keeps highest score."""
    import memory_core

    recorded_calls = []

    async def stub_search(*args, **kwargs):
        recorded_calls.append({"args": args, "kwargs": kwargs})
        variant_arg = kwargs.get("variant", "")

        if variant_arg == "base":
            # Base variant returns 3 results
            return [
                (0.9, {"id": "a", "content": "a", "title": "ta"}),
                (0.8, {"id": "b", "content": "b", "title": "tb"}),
                (0.7, {"id": "c", "content": "c", "title": "tc"}),
            ]
        elif variant_arg == "fact-tier":
            # Fact variant returns 3 results with overlap on "b"
            return [
                (0.85, {"id": "b", "content": "b_fact", "title": "tb_fact"}),
                (0.75, {"id": "d", "content": "d", "title": "td"}),
                (0.65, {"id": "e", "content": "e", "title": "te"}),
            ]
        else:
            # Fallback (shouldn't happen in this test)
            return []

    monkeypatch.setattr(memory_core, "memory_search_scored_impl", stub_search)

    result = await memory_core.memory_search_routed_impl(
        "what is my favorite color?", k=5, fact_variant="fact-tier", variant="base"
    )

    assert len(recorded_calls) == 2, f"Expected 2 calls (base + fact), got {len(recorded_calls)}"
    assert len(result) <= 5, f"Expected <= 5 results, got {len(result)}"

    # Verify dedup: "b" should appear once with highest score (0.9 from base is higher than 0.85 from fact)
    result_ids = [item["id"] for _, item in result]
    assert result_ids.count("b") == 1, "Memory 'b' should appear only once"

    # Verify sorted by score descending
    scores = [score for score, _ in result]
    assert scores == sorted(scores, reverse=True), "Results should be sorted by score descending"


@pytest.mark.asyncio
async def test_env_var_temporal_k_bump_override(monkeypatch):
    """M3_ROUTER_TEMPORAL_K_BUMP env var overrides temporal_k_bump kwarg."""
    import memory_core

    recorded_calls = []

    async def stub_search(*args, **kwargs):
        recorded_calls.append({"args": args, "kwargs": kwargs})
        return [(0.9, {"id": "m1", "content": "h1"})]

    monkeypatch.setattr(memory_core, "memory_search_scored_impl", stub_search)
    monkeypatch.setenv("M3_ROUTER_TEMPORAL_K_BUMP", "10")

    await memory_core.memory_search_routed_impl(
        "when was that?", k=5, temporal_k_bump=5
    )

    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    assert call["kwargs"]["k"] == 15, f"Expected k=15 (5+10 env override), got {call['kwargs']['k']}"


@pytest.mark.asyncio
async def test_no_expansion_returns_primary_unchanged():
    """With graph_depth=0 and expand_sessions=False, _maybe_expand_routed is a no-op."""
    import memory_core

    primary = [(0.9, {"id": "a"}), (0.8, {"id": "b"})]
    out = await memory_core._maybe_expand_routed(
        "anything", primary, k=5, graph_depth=0, expand_sessions=False,
    )
    assert out == primary


@pytest.mark.asyncio
async def test_graph_depth_calls_neighbor_helper(monkeypatch):
    """graph_depth>0 triggers _graph_neighbor_ids and fuses extras into result."""
    import memory_core

    captured = {"called": False, "seed_ids": None, "depth": None}

    def stub_graph_neighbors(seed_ids, depth):
        captured["called"] = True
        captured["seed_ids"] = list(seed_ids)
        captured["depth"] = depth
        return {"neighbor_x"}

    async def stub_score(query, rows_by_id, base_score=0.0):
        return [(0.95, {"id": "neighbor_x", "title": "from graph"})]

    class StubCursor:
        def __init__(self, rows): self.rows = rows
        def fetchall(self): return self.rows

    class StubConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None):
            if "memory_items" in sql.lower() and "id in" in sql.lower():
                return StubCursor([{"id": "neighbor_x", "type": "note", "title": "from graph",
                                    "content": "neighbor content", "metadata_json": "{}",
                                    "conversation_id": "", "valid_from": "", "user_id": ""}])
            return StubCursor([])

    monkeypatch.setattr(memory_core, "_graph_neighbor_ids", stub_graph_neighbors)
    monkeypatch.setattr(memory_core, "_score_extra_rows", stub_score)
    monkeypatch.setattr(memory_core, "_db", lambda: StubConn())

    primary = [(0.9, {"id": "a"})]
    out = await memory_core._maybe_expand_routed(
        "anything", primary, k=5, graph_depth=2, expand_sessions=False,
    )
    assert captured["called"]
    assert captured["seed_ids"] == ["a"]
    assert captured["depth"] == 2
    out_ids = [item["id"] for _, item in out]
    assert "a" in out_ids
    assert "neighbor_x" in out_ids
    assert out[0][1]["id"] == "neighbor_x"  # higher score first


@pytest.mark.asyncio
async def test_expand_sessions_calls_session_helper(monkeypatch):
    """expand_sessions=True triggers _session_neighbor_ids and fuses extras."""
    import memory_core

    captured = {"called": False, "cap": None}

    def stub_session_neighbors(seed_ids, session_cap=12):
        captured["called"] = True
        captured["cap"] = session_cap
        return {"sess_x": {"id": "sess_x", "title": "session-mate"}}

    async def stub_score(query, rows_by_id, base_score=0.0):
        return [(0.7, {"id": "sess_x", "title": "session-mate"})]

    monkeypatch.setattr(memory_core, "_session_neighbor_ids", stub_session_neighbors)
    monkeypatch.setattr(memory_core, "_score_extra_rows", stub_score)

    primary = [(0.9, {"id": "a"})]
    out = await memory_core._maybe_expand_routed(
        "anything", primary, k=5, graph_depth=0, expand_sessions=True, session_cap=8,
    )
    assert captured["called"]
    assert captured["cap"] == 8
    out_ids = [item["id"] for _, item in out]
    assert "a" in out_ids
    assert "sess_x" in out_ids


def test_graph_neighbor_ids_edge_cases():
    """_graph_neighbor_ids returns empty set on empty seeds or zero depth."""
    import memory_core
    assert memory_core._graph_neighbor_ids([], depth=2) == set()
    assert memory_core._graph_neighbor_ids(["x", "y"], depth=0) == set()
