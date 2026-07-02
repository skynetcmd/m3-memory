"""Tests for explainable-retrieval reason strings (Lever 2).

_explain_reason synthesizes a human-readable "why did this match?" summary from
the numeric _explanation components search already computes under explain=True.
It's the trust/debuggability surface: a false-positive is explainable, a recall
is trustable. Pure function — no DB/embedder needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from memory.search_ranking import _explain_reason  # noqa: E402


def test_strong_semantic_and_keyword_and_title():
    r = _explain_reason({"vector": 0.72, "bm25": 0.6, "title_overlap": 0.05, "importance": 0.6})
    assert "strong semantic match" in r
    assert "keyword (BM25) match" in r
    assert "title overlaps the query" in r


def test_moderate_semantic_and_importance_and_intent():
    r = _explain_reason({"vector": 0.4, "bm25": 0.2, "importance": 0.8,
                         "intent_hint": "temporal-reasoning"})
    assert "moderate semantic match" in r
    assert "high importance" in r
    assert "routed as temporal-reasoning" in r


def test_role_boost_surfaces():
    r = _explain_reason({"vector": 0.4, "role_boost": 0.1})
    assert "speaker/role match" in r


def test_weak_match_has_explicit_fallback():
    r = _explain_reason({"vector": 0.2, "bm25": 0.1})
    assert "weak" in r.lower()
    assert r  # never empty


def test_general_intent_not_reported():
    # 'general' is the default route — not worth surfacing as a reason.
    r = _explain_reason({"vector": 0.72, "intent_hint": "general"})
    assert "routed as" not in r


def test_handles_missing_and_none_fields():
    # Robust to a sparse dict — no KeyError / TypeError.
    assert _explain_reason({}) != ""
    assert _explain_reason({"vector": None, "bm25": None}) != ""
