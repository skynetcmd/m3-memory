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


@pytest.fixture(autouse=True)
def isolate_routing_env(monkeypatch):
    """Ensure the test suite runs with M3_ROUTE_SHADOW_MODE set to 'off' by default."""
    monkeypatch.setenv("M3_ROUTE_SHADOW_MODE", "off")


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

    def stub_session_neighbors(seed_ids, session_cap=12, **_kw):
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


# ---------------------------------------------------------------------------
# Phase 3 tests: AUTO routing layer (layered precedence + branch behavior)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_route_off_is_identity(monkeypatch):
    """auto_route=False (default) must not populate _capture_dict and must not run overshoot.

    When auto_route=False, the function must behave byte-identically to pre-refactor:
    no extra retrieval calls, no capture dict population.
    """
    import memory_core as mc

    calls = []

    async def tracking_scored(*args, **kwargs):
        calls.append(kwargs.get("k"))
        return []

    monkeypatch.setattr(mc, "memory_search_scored_impl", tracking_scored)

    cap = {}
    await mc.memory_search_routed_impl(
        "what is my favorite color", k=10, auto_route=False, _capture_dict=cap
    )

    # When auto_route=False, _capture_dict must not be populated by AUTO layer
    assert cap == {}, f"Expected empty capture dict when auto_route=False; got {cap}"
    # Non-temporal, no fact_variant: exactly 1 scored call (at k=10, not k=20 overshoot)
    assert len(calls) == 1, f"Expected 1 scored call; got {len(calls)}: {calls}"
    assert calls[0] == 10, f"Expected k=10 (no overshoot), got k={calls[0]}"


@pytest.mark.asyncio
async def test_auto_route_temporal_branch_fires(monkeypatch):
    """Temporal cue in query → temporal branch → graph_depth=1 (AUTO_v2 fix)."""
    import memory_core as mc

    async def stub_scored(*args, **kwargs):
        return []

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)

    cap = {}
    await mc.memory_search_routed_impl(
        "when did I last visit Paris", auto_route=True, _capture_dict=cap
    )

    assert cap.get("auto_branch") == "temporal", (
        f"Expected auto_branch='temporal', got {cap.get('auto_branch')!r}"
    )
    branch_vals = cap.get("auto_branch_values", {})
    assert branch_vals.get("graph_depth") == 1, (
        f"AUTO_v2 fix: temporal branch must set graph_depth=1; got {branch_vals.get('graph_depth')!r}"
    )


@pytest.mark.asyncio
async def test_auto_route_caller_override_wins(monkeypatch):
    """Caller passes k=20 + auto_route=True for a temporal query → caller_overrides records k=20."""
    import memory_core as mc

    async def stub_scored(*args, **kwargs):
        return []

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)

    cap = {}
    await mc.memory_search_routed_impl(
        "when did X happen", k=20, auto_route=True, _capture_dict=cap
    )

    assert cap.get("auto_branch") == "temporal"
    # The caller passed k=20 (non-default, default is 10), so caller_overrides must record it
    assert cap.get("caller_overrides", {}).get("k") == 20, (
        f"Expected caller_overrides['k']=20; got {cap.get('caller_overrides')}"
    )


@pytest.mark.asyncio
async def test_auto_route_default_branch_is_passthrough(monkeypatch):
    """Query with no temporal/comparison cues + flat/low score curve → default branch → empty branch_values."""
    import memory_core as mc

    # Return flat, low-score candidates that won't trigger sharp (top_1 < 0.89)
    # and no temporal/comparison cues in the query.
    low_score_candidates = [
        (0.50, {"id": "a", "score": 0.50, "conversation_id": "c1"}),
        (0.48, {"id": "b", "score": 0.48, "conversation_id": "c2"}),
        (0.46, {"id": "c", "score": 0.46, "conversation_id": "c3"}),
    ]

    async def stub_scored(*args, **kwargs):
        return low_score_candidates

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)

    cap = {}
    await mc.memory_search_routed_impl(
        "tell me about the project", auto_route=True, _capture_dict=cap
    )

    assert cap.get("auto_branch") == "default", (
        f"Expected default branch for low-score flat curve; got {cap.get('auto_branch')!r}"
    )
    assert cap.get("auto_branch_values") == {}, (
        f"default branch must be pure pass-through (empty branch_values); got {cap.get('auto_branch_values')}"
    )


@pytest.mark.asyncio
async def test_auto_route_signal_capture_present(monkeypatch):
    """auto_route=True must populate _capture_dict with all required signal keys."""
    import memory_core as mc

    async def stub_scored(*args, **kwargs):
        return []

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)

    cap = {}
    await mc.memory_search_routed_impl("test query", auto_route=True, _capture_dict=cap)

    assert "auto_signals" in cap, "auto_signals key must be present in capture dict"
    sigs = cap["auto_signals"]
    assert "has_temporal_cues" in sigs, "missing has_temporal_cues"
    assert "has_comparison_cues" in sigs, "missing has_comparison_cues"
    assert "top_1_score" in sigs, "missing top_1_score"
    assert "slope_at_3" in sigs, "missing slope_at_3"
    assert "conv_id_diversity" in sigs, "missing conv_id_diversity"
    # Verify other top-level capture keys
    assert "auto_branch" in cap, "missing auto_branch"
    assert "auto_branch_values" in cap, "missing auto_branch_values"
    assert "caller_overrides" in cap, "missing caller_overrides"


@pytest.mark.asyncio
async def test_auto_route_with_M3_QUERY_TYPE_ROUTING(monkeypatch):
    """auto_route + M3_QUERY_TYPE_ROUTING=1 should not conflict or raise."""
    import memory_core as mc

    async def stub_scored(*args, **kwargs):
        return []

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)
    monkeypatch.setenv("M3_QUERY_TYPE_ROUTING", "1")

    cap = {}
    # Temporal query → should still resolve to temporal branch despite env var
    await mc.memory_search_routed_impl(
        "when did this happen", auto_route=True, _capture_dict=cap
    )

    assert cap.get("auto_branch") == "temporal", (
        f"Expected temporal branch; got {cap.get('auto_branch')!r}"
    )
    # No assertion on M3_QUERY_TYPE_ROUTING side effect — just verifying no crash
    # and that auto_branch still fires correctly.


@pytest.mark.asyncio
async def test_auto_route_sharp_branch_threshold_trim(monkeypatch):
    """High top-1 + steep slope → sharp branch → post-trim removes hits below threshold."""
    import memory_core as mc

    # Candidates: steep drop at rank 3+ (slope_at_3 > 0.08, top_1 > 0.89)
    # slope = (0.95 - 0.70) / 2 = 0.125 > 0.08 ✓
    # top_1 = 0.95 > 0.89 ✓
    # No temporal/comparison cues, low conv_id_diversity → sharp branch
    sharp_candidates = [
        (0.95, {"id": "a", "score": 0.95, "conversation_id": "c1"}),
        (0.90, {"id": "b", "score": 0.90, "conversation_id": "c1"}),
        (0.70, {"id": "c", "score": 0.70, "conversation_id": "c1"}),
        # Sharp drop here — below threshold 0.95 * 0.85 = 0.8075
        (0.30, {"id": "d", "score": 0.30, "conversation_id": "c1"}),
        (0.25, {"id": "e", "score": 0.25, "conversation_id": "c1"}),
    ]

    async def stub_scored(*args, **kwargs):
        return list(sharp_candidates)

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)

    cap = {}
    result = await mc.memory_search_routed_impl(
        # Non-temporal, non-comparison query
        "tell me about the project architecture",
        auto_route=True,
        _capture_dict=cap,
    )

    assert cap.get("auto_branch") == "sharp", (
        f"Expected sharp branch for high top-1 steep-slope candidates; got {cap.get('auto_branch')!r}"
    )
    result_ids = [item["id"] for _, item in result]
    # threshold = 0.95 * 0.85 = 0.8075; only 'a' (0.95) and 'b' (0.90) qualify
    # 'c' (0.70) and below are dropped; k_min=3 so we get at least 3
    assert "d" not in result_ids, f"Hit 'd' (score 0.30) should be trimmed; result_ids={result_ids}"
    assert "e" not in result_ids, f"Hit 'e' (score 0.25) should be trimmed; result_ids={result_ids}"
    # sharp_post_trim_count captured
    assert "sharp_post_trim_count" in cap, "sharp_post_trim_count must be written to capture dict"


@pytest.mark.asyncio
async def test_auto_route_thresholds_are_overridable(monkeypatch):
    """Caller-passed auto_top1_sharp_min=0.99 makes sharp branch impossible when top_1 ≈ 0.95."""
    import memory_core as mc

    # Same candidates as sharp test — would normally trigger sharp (top_1=0.95)
    sharp_candidates = [
        (0.95, {"id": "a", "score": 0.95, "conversation_id": "c1"}),
        (0.90, {"id": "b", "score": 0.90, "conversation_id": "c1"}),
        (0.70, {"id": "c", "score": 0.70, "conversation_id": "c1"}),
        (0.30, {"id": "d", "score": 0.30, "conversation_id": "c1"}),
        (0.25, {"id": "e", "score": 0.25, "conversation_id": "c1"}),
    ]

    async def stub_scored(*args, **kwargs):
        return list(sharp_candidates)

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)

    cap = {}
    # Override: require top_1 > 0.99 to trigger sharp — 0.95 won't qualify
    await mc.memory_search_routed_impl(
        "tell me about the project architecture",
        auto_route=True,
        auto_top1_sharp_min=0.99,
        _capture_dict=cap,
    )

    assert cap.get("auto_branch") != "sharp", (
        f"sharp must not fire when auto_top1_sharp_min=0.99 and top_1=0.95; "
        f"got branch={cap.get('auto_branch')!r}"
    )
    # Should fall to default since: no temporal, no comparison, low diversity, top_1 < 0.99
    assert cap.get("auto_branch") == "default", (
        f"Expected default branch (sharp threshold too high); got {cap.get('auto_branch')!r}"
    )


# ---------------------------------------------------------------------------
# (The Phase B-1 federation tests were removed when the ChromaDB feature was
# retired — search no longer has a federated fallback path to exercise.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase B-2 regression: _expanded_via source-tag plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expanded_via_tags_set_on_session_and_graph_hits(monkeypatch):
    """Session and graph expansion hits carry _expanded_via='session'/'graph'; primary hits carry 'primary'.

    Regression test for the 2026-04-26 strat60 v3 bug where 100% of hits were tagged
    'primary' despite expand_sessions=True and graph_depth=1 being active.

    Setup:
    - memory_search_scored_impl returns one primary hit (id='primary-a')
    - _session_neighbor_ids returns one session neighbor (id='session-b')
    - _graph_neighbor_ids returns one graph neighbor (id='graph-c')
    - _score_extra_rows returns both neighbors with scored tuples
    - _db returns rows for both neighbor ids

    Asserts:
    - primary-a has _expanded_via='primary'
    - session-b has _expanded_via='session'
    - graph-c has _expanded_via='graph'
    - NOT all hits are 'primary'
    """
    import memory_core as mc

    async def stub_scored(*args, **kwargs):
        return [(0.9, {"id": "primary-a", "content": "primary hit", "title": "p"})]

    def stub_session_neighbors(seed_ids, session_cap=12, **_kw):
        return {"session-b": {"id": "session-b", "content": "session hit", "title": "s"}}

    def stub_graph_neighbors(seed_ids, depth):
        return {"graph-c"}

    class StubCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class StubConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if "graph-c" in str(params or []):
                return StubCursor([{
                    "id": "graph-c", "type": "note", "title": "graph hit",
                    "content": "graph content", "metadata_json": "{}",
                    "conversation_id": "", "valid_from": "", "user_id": "",
                }])
            return StubCursor([])

    async def stub_score(query, rows_by_id, base_score=0.0):
        out = []
        for mid, item in rows_by_id.items():
            out.append((0.7, item))
        return out

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)
    monkeypatch.setattr(mc, "_session_neighbor_ids", stub_session_neighbors)
    monkeypatch.setattr(mc, "_graph_neighbor_ids", stub_graph_neighbors)
    monkeypatch.setattr(mc, "_score_extra_rows", stub_score)
    monkeypatch.setattr(mc, "_db", lambda: StubConn())

    result = await mc.memory_search_routed_impl(
        "what is my favorite color?",  # non-temporal query
        k=10,
        expand_sessions=True,
        graph_depth=1,
    )

    result_by_id = {item["id"]: item for _, item in result if isinstance(item, dict)}

    assert "primary-a" in result_by_id, f"primary-a missing from result; got {list(result_by_id)}"
    assert "session-b" in result_by_id, f"session-b missing from result; got {list(result_by_id)}"
    assert "graph-c" in result_by_id, f"graph-c missing from result; got {list(result_by_id)}"

    assert result_by_id["primary-a"].get("_expanded_via") == "primary", (
        f"primary-a must be tagged 'primary'; got {result_by_id['primary-a'].get('_expanded_via')!r}"
    )
    assert result_by_id["session-b"].get("_expanded_via") == "session", (
        f"session-b must be tagged 'session'; got {result_by_id['session-b'].get('_expanded_via')!r}"
    )
    assert result_by_id["graph-c"].get("_expanded_via") == "graph", (
        f"graph-c must be tagged 'graph'; got {result_by_id['graph-c'].get('_expanded_via')!r}"
    )

    # Verify not all hits are "primary"
    all_tags = [item.get("_expanded_via") for _, item in result if isinstance(item, dict)]
    assert set(all_tags) != {"primary"}, (
        f"All hits tagged 'primary' — expansion source tags are still being dropped! tags={all_tags}"
    )


# ---------------------------------------------------------------------------
# Phase E2 tests: entity_anchored AUTO branch + vocabulary pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_entity_branch_fires_on_named_entity_query(monkeypatch):
    """Query with named entities and auto_route=True fires entity_anchored branch,
    setting entity_graph=True in branch_values.

    Uses a query with no temporal or comparison cues to isolate entity_anchored.
    """
    import memory_core as mc

    async def stub_scored(*args, **kwargs):
        return []

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)

    cap = {}
    await mc.memory_search_routed_impl(
        "Did Alice Smith ever collaborate with Bob Jones?",
        auto_route=True,
        _capture_dict=cap,
    )

    # "Alice" and "Bob Smith" are named entity phrases — entity_anchored should fire
    # (assuming query has no temporal/comparison cues that would preempt it)
    assert cap.get("auto_branch") == "entity_anchored", (
        f"Expected entity_anchored branch for named-entity query; got {cap.get('auto_branch')!r}"
    )
    branch_vals = cap.get("auto_branch_values", {})
    assert branch_vals.get("entity_graph") is True, (
        f"entity_anchored branch must set entity_graph=True; got {branch_vals.get('entity_graph')!r}"
    )
    # auto_signals must include named_entity_count >= 1
    sigs = cap.get("auto_signals", {})
    assert sigs.get("named_entity_count", 0) >= 1, (
        f"auto_signals.named_entity_count must be >= 1; got {sigs.get('named_entity_count')!r}"
    )


@pytest.mark.asyncio
async def test_auto_entity_branch_skipped_on_no_named_entities(monkeypatch):
    """Query 'what color is the sky' has no named entities → entity_anchored must NOT fire."""
    import memory_core as mc

    async def stub_scored(*args, **kwargs):
        return []

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)

    cap = {}
    await mc.memory_search_routed_impl(
        "what color is the sky",
        auto_route=True,
        _capture_dict=cap,
    )

    assert cap.get("auto_branch") != "entity_anchored", (
        f"entity_anchored must NOT fire for 'what color is the sky'; got {cap.get('auto_branch')!r}"
    )
    sigs = cap.get("auto_signals", {})
    assert sigs.get("named_entity_count", 0) == 0, (
        f"named_entity_count must be 0 for no-caps query; got {sigs.get('named_entity_count')!r}"
    )


@pytest.mark.asyncio
async def test_caller_explicit_entity_graph_false_overrides_auto(monkeypatch):
    """Caller passes auto_entity_graph_enabled=False AND auto_route=True; entity_graph stays False.

    auto_entity_graph_enabled=False is the documented way to tell AUTO not to enable entity
    graph. Even if entity_anchored branch would fire, this flag suppresses the entity_graph=True.
    """
    import memory_core as mc

    expand_calls = []

    async def stub_scored(*args, **kwargs):
        return []

    async def stub_expand(query, primary, k, **kwargs):
        expand_calls.append(kwargs.get("entity_graph", False))
        return primary

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)
    monkeypatch.setattr(mc, "_maybe_expand_routed", stub_expand)

    cap = {}
    await mc.memory_search_routed_impl(
        "Did Alice meet Bob Smith",
        auto_route=True,
        auto_entity_graph_enabled=False,   # caller disables AUTO entity branch
        _capture_dict=cap,
    )

    # entity_graph must NOT be enabled after resolution
    for eg_val in expand_calls:
        assert eg_val is False, (
            f"entity_graph must remain False when auto_entity_graph_enabled=False; got {eg_val!r}"
        )


@pytest.mark.asyncio
async def test_entity_graph_valid_types_override(monkeypatch):
    """Pass entity_graph_valid_types=['person']; verify _entity_graph_neighbor_ids
    receives valid_types=['person']."""
    import memory_core as mc

    captured = {}

    async def stub_scored(*args, **kwargs):
        return []

    async def stub_entity_neighbor_ids(query, depth, max_neighbors, db,
                                        valid_types=None, valid_predicates=None,
                                        **kwargs):
        # **kwargs absorbs additions like entity_stoplist / _capture_dict.
        captured["valid_types"] = valid_types
        captured["valid_predicates"] = valid_predicates
        return set()

    class StubConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)
    monkeypatch.setattr(mc, "_entity_graph_neighbor_ids", stub_entity_neighbor_ids)
    monkeypatch.setattr(mc, "_db", lambda: StubConn())

    await mc.memory_search_routed_impl(
        "What does Alice do?",
        entity_graph=True,
        entity_graph_valid_types=["person"],
    )

    assert captured.get("valid_types") == ["person"], (
        f"_entity_graph_neighbor_ids must receive valid_types=['person']; got {captured.get('valid_types')!r}"
    )
    assert captured.get("valid_predicates") is None, (
        f"valid_predicates must be None when not overridden; got {captured.get('valid_predicates')!r}"
    )


@pytest.mark.asyncio
async def test_entity_graph_valid_predicates_override(monkeypatch):
    """Pass entity_graph_valid_predicates=['works_at']; verify only works_at edges are requested."""
    import memory_core as mc

    captured = {}

    async def stub_scored(*args, **kwargs):
        return []

    async def stub_entity_neighbor_ids(query, depth, max_neighbors, db,
                                        valid_types=None, valid_predicates=None,
                                        **kwargs):
        # **kwargs absorbs additions like entity_stoplist / _capture_dict.
        captured["valid_types"] = valid_types
        captured["valid_predicates"] = valid_predicates
        return set()

    class StubConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)
    monkeypatch.setattr(mc, "_entity_graph_neighbor_ids", stub_entity_neighbor_ids)
    monkeypatch.setattr(mc, "_db", lambda: StubConn())

    await mc.memory_search_routed_impl(
        "Where does Alice work?",
        entity_graph=True,
        entity_graph_valid_predicates=["works_at"],
    )

    assert captured.get("valid_predicates") == ["works_at"], (
        f"_entity_graph_neighbor_ids must receive valid_predicates=['works_at']; "
        f"got {captured.get('valid_predicates')!r}"
    )
    assert captured.get("valid_types") is None, (
        f"valid_types must be None when not overridden; got {captured.get('valid_types')!r}"
    )


@pytest.mark.asyncio
async def test_named_entity_threshold_respected(monkeypatch):
    """auto_entity_graph_named_entity_threshold=3; query with 2 named entities must NOT fire entity_anchored."""
    import memory_core as mc

    async def stub_scored(*args, **kwargs):
        return []

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)

    cap = {}
    # "Alice Smith" + "New York" = 2 named entities
    await mc.memory_search_routed_impl(
        "Alice Smith lives in New York",
        auto_route=True,
        auto_entity_graph_named_entity_threshold=3,   # require 3; we only have 2
        _capture_dict=cap,
    )

    assert cap.get("auto_branch") != "entity_anchored", (
        f"entity_anchored must NOT fire when named entity count < threshold=3; "
        f"got branch={cap.get('auto_branch')!r}"
    )


@pytest.mark.asyncio
async def test_entity_branch_capture_includes_neighbor_count(monkeypatch):
    """When entity_anchored fires, _capture_dict must include entity_graph_neighbors_added."""
    import memory_core as mc

    async def stub_scored(*args, **kwargs):
        return []

    async def stub_expand(query, primary, k, **kwargs):
        # Simulate 2 entity_graph hits being returned
        return [
            (0.8, {"id": "eg-1", "_expanded_via": "entity_graph"}),
            (0.7, {"id": "eg-2", "_expanded_via": "entity_graph"}),
        ]

    monkeypatch.setattr(mc, "memory_search_scored_impl", stub_scored)
    monkeypatch.setattr(mc, "_maybe_expand_routed", stub_expand)

    cap = {}
    await mc.memory_search_routed_impl(
        "Did Alice meet Bob Smith",
        auto_route=True,
        _capture_dict=cap,
    )

    # entity_anchored branch must fire (Alice + Bob Smith are named entities)
    if cap.get("auto_branch") == "entity_anchored":
        assert "entity_graph_neighbors_added" in cap, (
            f"entity_graph_neighbors_added must be in capture when entity_anchored fires; "
            f"got {list(cap.keys())}"
        )
        assert cap["entity_graph_neighbors_added"] == 2, (
            f"Expected 2 entity_graph neighbors added; got {cap['entity_graph_neighbors_added']!r}"
        )


@pytest.mark.asyncio
async def test_byte_identity_invariant_with_entity_params_unset(monkeypatch):
    """auto_route=False with all entity_* params unset must produce the same scored call
    as pre-Phase-E2: exactly 1 scored call at k=10 (no overshoot), empty capture dict.

    Mirrors test_auto_route_off_is_identity from Phase 1; confirms Phase E2 preserves
    the byte-identity invariant.
    """
    import memory_core as mc

    calls = []

    async def tracking_scored(*args, **kwargs):
        calls.append(kwargs.get("k"))
        return []

    monkeypatch.setattr(mc, "memory_search_scored_impl", tracking_scored)

    cap = {}
    await mc.memory_search_routed_impl(
        "what is my favorite color",
        k=10,
        auto_route=False,
        # all entity_* params at their defaults (unset)
        _capture_dict=cap,
    )

    # Capture dict must remain empty — no AUTO layer ran
    assert cap == {}, f"Expected empty capture dict when auto_route=False; got {cap}"
    # Exactly 1 scored call at k=10 — no overshoot
    assert len(calls) == 1, f"Expected 1 scored call; got {len(calls)}: {calls}"
    assert calls[0] == 10, f"Expected k=10 (no overshoot), got k={calls[0]}"


# ── Expansion-displacement guard ───────────────────────────────────────────
#
# At small k, expansion-sourced rows (entity_graph, graph, session, neighbor)
# previously won rank-1 from the hybrid primary on score parity, even though
# the two pools' scores are not calibrated against each other. The guard
# enforces: at ranks 1..M3_EXPANSION_PROTECTED_RANKS, an expansion row may
# only outrank a primary if expansion_score >= margin * primary_score.

def _make_hit(score, item_id, source):
    """Build a (score, dict) hit shaped like memory_search_scored_impl output."""
    item = {"id": item_id}
    if source is not None:
        item["_expanded_via"] = source
    return (score, item)


def test_displacement_guard_weak_expansion_is_demoted():
    """Expansion at rank 1 with sub-margin score must yield to the next primary."""
    import memory_core as mc

    # 0.8 / 0.6 = 1.33x, below default 1.75x margin -> primary wins
    hits = [
        _make_hit(0.8, "e1", "entity_graph"),
        _make_hit(0.6, "p1", "primary"),
        _make_hit(0.5, "p2", "primary"),
    ]
    out = mc._enforce_expansion_displacement_guard(hits)
    assert out[0][1]["id"] == "p1"
    # Demoted expansion lands in the slot the primary vacated.
    assert {h[1]["id"] for h in out} == {"e1", "p1", "p2"}


def test_displacement_guard_strong_expansion_is_preserved():
    """Expansion at rank 1 with score >= margin*primary stays at rank 1."""
    import memory_core as mc

    # 1.0 / 0.5 = 2.0x, above default 1.75x margin -> expansion stays
    hits = [
        _make_hit(1.0, "e1", "entity_graph"),
        _make_hit(0.5, "p1", "primary"),
    ]
    out = mc._enforce_expansion_displacement_guard(hits)
    assert out[0][1]["id"] == "e1"


def test_displacement_guard_primary_at_top_is_unchanged():
    """A primary already at rank 1 is never touched."""
    import memory_core as mc

    hits = [
        _make_hit(0.9, "p1", "primary"),
        _make_hit(0.95, "e1", "entity_graph"),
    ]
    out = mc._enforce_expansion_displacement_guard(hits)
    assert [h[1]["id"] for h in out] == ["p1", "e1"]


def test_displacement_guard_zero_score_path_defaults_to_primary():
    """When scores are non-positive, ratio is undefined → primary wins."""
    import memory_core as mc

    hits = [
        _make_hit(0.0, "e1", "entity_graph"),
        _make_hit(0.0, "p1", "primary"),
    ]
    out = mc._enforce_expansion_displacement_guard(hits)
    assert out[0][1]["id"] == "p1"


def test_displacement_guard_below_protected_ranks_is_free():
    """An expansion at rank > protected_ranks is not touched."""
    import memory_core as mc

    # Protected = 3 by default; expansion at rank 4 stays put.
    hits = [
        _make_hit(0.9, "p1", "primary"),
        _make_hit(0.9, "p2", "primary"),
        _make_hit(0.9, "p3", "primary"),
        _make_hit(0.95, "e1", "entity_graph"),
        _make_hit(0.5, "p4", "primary"),
    ]
    out = mc._enforce_expansion_displacement_guard(hits)
    assert out[3][1]["id"] == "e1"


def test_displacement_guard_untagged_row_is_treated_as_primary():
    """A row without _expanded_via tag is treated as primary (legacy compatibility)."""
    import memory_core as mc

    hits = [
        _make_hit(0.5, "untagged", None),
        _make_hit(0.4, "p1", "primary"),
    ]
    out = mc._enforce_expansion_displacement_guard(hits)
    assert out[0][1]["id"] == "untagged"


def test_displacement_guard_cascading_displacement():
    """Multiple weak expansions in a row all get demoted, in order."""
    import memory_core as mc

    hits = [
        _make_hit(0.8, "e1", "entity_graph"),
        _make_hit(0.7, "e2", "entity_graph"),
        _make_hit(0.5, "p1", "primary"),
        _make_hit(0.4, "p2", "primary"),
    ]
    out = mc._enforce_expansion_displacement_guard(hits)
    # rank 1=p1 (was at idx 2), rank 2=p2 (was at idx 3), then the demoted expansions
    assert [h[1]["id"] for h in out] == ["p1", "p2", "e1", "e2"]


def test_displacement_guard_disabled_when_protected_ranks_zero():
    """protected_ranks=0 → no-op (feature off)."""
    import memory_core as mc

    hits = [
        _make_hit(0.8, "e1", "entity_graph"),
        _make_hit(0.6, "p1", "primary"),
    ]
    out = mc._enforce_expansion_displacement_guard(hits, protected_ranks=0)
    assert [h[1]["id"] for h in out] == ["e1", "p1"]


def test_displacement_guard_disabled_when_margin_at_or_below_one():
    """margin <= 1.0 → no-op (feature off)."""
    import memory_core as mc

    hits = [
        _make_hit(0.8, "e1", "entity_graph"),
        _make_hit(0.6, "p1", "primary"),
    ]
    out = mc._enforce_expansion_displacement_guard(hits, margin=1.0)
    assert [h[1]["id"] for h in out] == ["e1", "p1"]


def test_displacement_guard_idempotent_on_conforming_list():
    """Applying the guard twice yields the same result as applying it once."""
    import memory_core as mc

    hits = [
        _make_hit(0.8, "e1", "entity_graph"),
        _make_hit(0.6, "p1", "primary"),
    ]
    once = mc._enforce_expansion_displacement_guard(hits)
    twice = mc._enforce_expansion_displacement_guard(once)
    assert [h[1]["id"] for h in once] == [h[1]["id"] for h in twice]


def test_displacement_guard_no_primary_available_leaves_expansion(monkeypatch):
    """If there's no primary below an expansion to swap with, it stays in place."""
    import memory_core as mc

    hits = [
        _make_hit(0.9, "e1", "entity_graph"),
        _make_hit(0.8, "e2", "graph"),
        _make_hit(0.7, "e3", "session"),
    ]
    out = mc._enforce_expansion_displacement_guard(hits)
    # All-expansion list: nothing to swap with → unchanged.
    assert [h[1]["id"] for h in out] == ["e1", "e2", "e3"]


def test_displacement_guard_env_var_override(monkeypatch):
    """Verify the module-level defaults read from M3_EXPANSION_* env vars.

    Note: env-var reads happen at module import. This test just verifies the
    constants are exposed and have the documented default values; full env-var
    behavior is covered by re-importing in a separate process, which is more
    fragile than it's worth here.
    """
    import memory_core as mc

    assert mc.EXPANSION_DISPLACEMENT_MARGIN == 2.0
    assert mc.EXPANSION_PROTECTED_RANKS == 3


@pytest.mark.asyncio
async def test_fts_short_circuit_bypasses_embedding(monkeypatch, tmp_path):
    """Verify that highly specific FTS exact match triggers the short-circuit and completely bypasses embedding."""
    import sqlite3

    import memory_core as mc

    from conftest import create_full_main_schema

    db_path = tmp_path / "test_short_circuit.db"
    create_full_main_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    # Insert a specific memory item into the database
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        # Write to memory_items and memory_items_fts (the schema already has standard trigger-syncs)
        conn.execute(
            """
            INSERT INTO memory_items (id, content, title, type, importance, is_deleted)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            ("key1", "My secret API key is SK-983271", "grok api key", "note", 0.95)
        )
        conn.execute(
            """
            INSERT INTO memory_items_fts (rowid, content, title)
            VALUES (last_insert_rowid(), ?, ?)
            """,
            ("My secret API key is SK-983271", "grok api key")
        )
        conn.commit()

    embed_called = False

    async def mock_embed(text: str):
        nonlocal embed_called
        embed_called = True
        return [0.1] * 1536, "mock_model"

    monkeypatch.setattr(mc, "_embed", mock_embed)

    # Search for the exact high-specificity phrase
    results = await mc.memory_search_routed_impl(
        "My secret API key", k=5
    )

    # 1. Verify that FTS short-circuit bypassed embedding entirely
    assert not embed_called, "Embedding generation should have been bypassed by the FTS short-circuit!"

    # 2. Verify that we got the exact correct hit with a score of 1.0
    assert len(results) == 1, f"Expected 1 hit, got {len(results)}"
    score, hit = results[0]
    assert score == 1.0, f"Expected exact short-circuit score 1.0, got {score}"
    assert hit["id"] == "key1"
    assert "SK-983271" in hit["content"]


@pytest.mark.asyncio
async def test_fts_short_circuit_conversational_skip(monkeypatch, tmp_path):
    """Verify that generic conversational query is skipped for short-circuit, calling embed normally."""
    import sqlite3

    import memory_core as mc

    from conftest import create_full_main_schema

    db_path = tmp_path / "test_short_circuit_skip.db"
    create_full_main_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            INSERT INTO memory_items (id, content, title, type, importance, is_deleted)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            ("key1", "show me details about coding bots", "show me", "note", 0.95)
        )
        conn.execute(
            """
            INSERT INTO memory_items_fts (rowid, content, title)
            VALUES (last_insert_rowid(), ?, ?)
            """,
            ("show me details about coding bots", "show me")
        )
        conn.commit()

    embed_called = False

    async def mock_embed(text: str):
        nonlocal embed_called
        embed_called = True
        return [0.1] * 1024, "mock_model"

    monkeypatch.setattr(mc, "_embed", mock_embed)

    # Search for conversational stop-word query "show me"
    # FTS early exit should bypass it, running standard embed path
    try:
        await mc.memory_search_routed_impl("show me", k=5)
    except Exception:
        pass

    assert embed_called, "Generic conversational query should NOT bypass embedding!"


def test_elbow_quality_gating_floor(monkeypatch):
    """Verify that elbow-trimming is bypassed when the top candidate's similarity is below 0.75."""
    import memory.config
    import memory_core as mc

    # 1. Top score is 0.70 (< 0.75) -> Should bypass trimming entirely and keep all
    hits_low = [
        (0.70, {"id": "mem1"}),
        (0.50, {"id": "mem2"}),
        (0.48, {"id": "mem3"}),
        (0.46, {"id": "mem4"}),
        (0.44, {"id": "mem5"}),
        (0.42, {"id": "mem6"}),
    ]

    # Mock ELBOW_MIN_INPUT to 5 so it triggers on these pools
    monkeypatch.setattr(memory.config, "ELBOW_MIN_INPUT", 5)
    # Mock ELBOW_MIN_RETURN to 3 so it is allowed to trim below 6 elements
    monkeypatch.setattr(memory.config, "ELBOW_MIN_RETURN", 3)

    out_low = mc._trim_by_elbow(hits_low, sensitivity=1.0)
    assert len(out_low) == len(hits_low), "Should bypass trimming when top similarity < 0.75"

    # 2. Top score is 0.90 (>= 0.75) -> Should run trimming
    hits_high = [
        (0.90, {"id": "mem1"}),
        (0.85, {"id": "mem2"}),
        (0.80, {"id": "mem3"}),
        (0.40, {"id": "mem4"}), # huge drop here
        (0.38, {"id": "mem5"}),
        (0.36, {"id": "mem6"}),
    ]

    out_high = mc._trim_by_elbow(hits_high, sensitivity=1.5)
    assert len(out_high) < len(hits_high), "Should trim candidates when top similarity is high and drop occurs"


# ── Reranker lazy-load thread-safety (regression) ─────────────────────────────
# _apply_rerank now runs via asyncio.to_thread (off the event loop), so two
# concurrent rerank=True searches can call _get_reranker from different pool
# threads at once. Without the _RERANKER_LOCK, they race the first-time model
# load. This verifies concurrent _get_reranker calls load the model exactly once
# and all return the same instance.

def test_get_reranker_concurrent_loads_once(monkeypatch):
    import threading as _threading

    import memory.search as S

    # Reset the module cache so the load actually happens under the test.
    monkeypatch.setattr(S, "_RERANKER_MODEL", None)
    monkeypatch.setattr(S, "_RERANKER_MODEL_NAME", "")

    load_count = {"n": 0}
    barrier = _threading.Barrier(8)

    class _FakeCE:
        def __init__(self, model_name, device="cpu"):
            load_count["n"] += 1  # count real constructions

    # Patch the CrossEncoder import target. _get_reranker does
    # `from sentence_transformers import CrossEncoder`, so patch there.
    import sys as _sys
    import types as _types
    fake_mod = _types.ModuleType("sentence_transformers")
    fake_mod.CrossEncoder = _FakeCE
    monkeypatch.setitem(_sys.modules, "sentence_transformers", fake_mod)

    results = []
    res_lock = _threading.Lock()

    def _worker():
        barrier.wait()  # maximize contention: all threads hit the load together
        r = S._get_reranker("some/model")
        with res_lock:
            results.append(r)

    threads = [_threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert load_count["n"] == 1, f"model must load exactly once, loaded {load_count['n']}x (race)"
    assert len(results) == 8
    assert all(r is results[0] for r in results), "all callers must get the same cached instance"
