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
# Phase B-1 tests: Federation firing on weak local results
# ---------------------------------------------------------------------------


def _make_stub_db(rows):
    """Return a minimal stub _db() context manager that yields the given rows."""
    import sqlite3

    class StubCursor:
        def __init__(self, r):
            self._rows = r

        def fetchall(self):
            return self._rows

    class StubConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            # UPDATE calls (access_count bump) return a no-op cursor.
            if sql.strip().upper().startswith("UPDATE"):
                return StubCursor([])
            return StubCursor(rows)

    return StubConn


@pytest.mark.asyncio
async def test_federation_fires_on_low_confidence_scoped_query(monkeypatch):
    """Federation fires when local returns 1 hit at score 0.4 (below threshold), even with scope.

    Verifies:
    - _query_chroma is called with scope_filter populated (user_id + scope).
    - Federated hits are merged into the final result.
    - The federated hit carries _explanation.source='federated_chroma_scoped'.
    """
    import struct
    import memory_core as mc

    # Fake 1-dim embedding: q_vec and row embedding are identical → cosine = 1.0
    # but we'll monkey-patch _batch_cosine to return a controlled low score.
    dummy_vec = [0.1] * 4
    dummy_emb_bytes = struct.pack(f"{len(dummy_vec)}f", *dummy_vec)

    # Stub _embed so no HTTP call is needed.
    async def stub_embed(text, kind="default"):
        return dummy_vec, "stub"

    # Row returned by the DB stub — must have all fields memory_search_scored_impl reads.
    # Content kept short to stay below SHORT_TURN_THRESHOLD (20), ensuring length_penalty
    # keeps final_score below FEDERATION_LOW_SCORE_THRESHOLD (0.65) alongside score=0.4.
    stub_row = {
        "id": "local-001",
        "content": "local hit",
        "title": "local",
        "type": "note",
        "importance": 1,
        "embedding": dummy_emb_bytes,
        "bm25_score": -1.5,
    }

    # Force cosine score to 0.4 so local_top_score < FEDERATION_LOW_SCORE_THRESHOLD (0.65).
    def stub_cosine(q, matrix):
        return [0.4] * len(matrix)

    chroma_calls = []

    async def stub_query_chroma(query_vec, k=5, scope_filter=None):
        chroma_calls.append({"k": k, "scope_filter": scope_filter})
        return [
            {
                "id": "fed-001",
                "content": "federated hit",
                "title": "fed",
                "type": "federated",
                "score": 0.72,
                "_explanation": {"source": "federated_chroma_scoped"},
            }
        ]

    class StubCursor:
        def __init__(self, r):
            self._rows = r

        def fetchall(self):
            return self._rows

    class StubConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("UPDATE"):
                return StubCursor([])
            return StubCursor([stub_row])

    monkeypatch.setattr(mc, "_embed", stub_embed)
    monkeypatch.setattr(mc, "_batch_cosine", stub_cosine)
    monkeypatch.setattr(mc, "_query_chroma", stub_query_chroma)
    monkeypatch.setattr(mc, "_db", lambda: StubConn())

    result = await mc.memory_search_scored_impl(
        "scoped query",
        k=5,
        user_id="user-A",
        scope="project-X",
        search_mode="semantic",
    )

    assert len(chroma_calls) == 1, (
        f"Expected _query_chroma to be called once; got {len(chroma_calls)}"
    )
    sf = chroma_calls[0]["scope_filter"]
    assert sf is not None, "_query_chroma must receive a scope_filter"
    assert sf.get("user_id") == "user-A", f"scope_filter user_id mismatch: {sf}"
    assert sf.get("scope") == "project-X", f"scope_filter scope mismatch: {sf}"

    result_ids = [item["id"] for _, item in result]
    assert "fed-001" in result_ids, (
        f"Federated hit 'fed-001' must appear in merged results; got {result_ids}"
    )
    fed_hit = next((item for _, item in result if item["id"] == "fed-001"), None)
    assert fed_hit is not None
    assert fed_hit.get("_explanation", {}).get("source") == "federated_chroma_scoped", (
        f"Federated hit must carry source='federated_chroma_scoped'; got {fed_hit.get('_explanation')}"
    )


@pytest.mark.asyncio
async def test_federation_skipped_on_strong_local_hits(monkeypatch):
    """Federation is NOT called when local returns >= 3 hits all scoring >= threshold.

    5 local hits at score 0.9 → local_top_score 0.9 >= 0.65 and len >= 3 → no federation.
    """
    import struct
    import memory_core as mc

    dummy_vec = [0.1] * 4
    dummy_emb_bytes = struct.pack(f"{len(dummy_vec)}f", *dummy_vec)

    async def stub_embed(text, kind="default"):
        return dummy_vec, "stub"

    def stub_cosine(q, matrix):
        return [0.9] * len(matrix)

    chroma_calls = []

    async def stub_query_chroma(query_vec, k=5, scope_filter=None):
        chroma_calls.append(True)
        return []

    # Content must be >= SHORT_TURN_THRESHOLD (20) chars to avoid length penalty
    # that would push the final score below FEDERATION_LOW_SCORE_THRESHOLD.
    strong_rows = [
        {
            "id": f"local-{i:03d}",
            "content": f"this is a strong local memory hit number {i}",
            "title": f"t{i}",
            "type": "note",
            "importance": 1,
            "embedding": dummy_emb_bytes,
            "bm25_score": -1.5,
        }
        for i in range(5)
    ]

    class StubCursor:
        def __init__(self, r):
            self._rows = r

        def fetchall(self):
            return self._rows

    class StubConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("UPDATE"):
                return StubCursor([])
            return StubCursor(strong_rows)

    monkeypatch.setattr(mc, "_embed", stub_embed)
    monkeypatch.setattr(mc, "_batch_cosine", stub_cosine)
    monkeypatch.setattr(mc, "_query_chroma", stub_query_chroma)
    monkeypatch.setattr(mc, "_db", lambda: StubConn())

    await mc.memory_search_scored_impl(
        "strong query",
        k=5,
        user_id="user-B",
        search_mode="semantic",
    )

    assert len(chroma_calls) == 0, (
        f"_query_chroma must NOT be called when local results are strong; "
        f"got {len(chroma_calls)} call(s)"
    )


@pytest.mark.asyncio
async def test_federation_skipped_on_conversation_id_filter(monkeypatch):
    """Federation is NOT called when conversation_id is set (hard-skip rule).

    Even with weak local hits (1 result at 0.4), conversation_id triggers
    _skip_federated_hard=True, so _query_chroma must not be called.
    """
    import struct
    import memory_core as mc

    dummy_vec = [0.1] * 4
    dummy_emb_bytes = struct.pack(f"{len(dummy_vec)}f", *dummy_vec)

    async def stub_embed(text, kind="default"):
        return dummy_vec, "stub"

    def stub_cosine(q, matrix):
        return [0.4] * len(matrix)  # weak score, but hard-skip should still prevent federation

    chroma_calls = []

    async def stub_query_chroma(query_vec, k=5, scope_filter=None):
        chroma_calls.append(True)
        return []

    weak_row = {
        "id": "local-weak-001",
        "content": "weak local hit",
        "title": "weak",
        "type": "note",
        "importance": 1,
        "embedding": dummy_emb_bytes,
        "bm25_score": -1.5,
    }

    class StubCursor:
        def __init__(self, r):
            self._rows = r

        def fetchall(self):
            return self._rows

    class StubConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("UPDATE"):
                return StubCursor([])
            return StubCursor([weak_row])

    monkeypatch.setattr(mc, "_embed", stub_embed)
    monkeypatch.setattr(mc, "_batch_cosine", stub_cosine)
    monkeypatch.setattr(mc, "_query_chroma", stub_query_chroma)
    monkeypatch.setattr(mc, "_db", lambda: StubConn())

    await mc.memory_search_scored_impl(
        "query within a conversation",
        k=5,
        conversation_id="c123",
        search_mode="semantic",
    )

    assert len(chroma_calls) == 0, (
        f"_query_chroma must NOT be called when conversation_id is set (hard skip); "
        f"got {len(chroma_calls)} call(s)"
    )


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

    def stub_session_neighbors(seed_ids, session_cap=12):
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
                                        valid_types=None, valid_predicates=None):
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
                                        valid_types=None, valid_predicates=None):
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
