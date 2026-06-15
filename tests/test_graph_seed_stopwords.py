"""Regression test for the entity-graph seed stopword filter (memory/graph.py).

Locks the behavior added in PR #13 (dd6f1ed): sentence-initial Title-Case common
words ("Can", "What", "How", "I", ...) that _ENTITY_MENTION_RE captures as if they
were proper nouns must be dropped from the entity-graph seed candidate list before
any entity lookup runs. Without this, seed "Can" LIKE-matches "Canada"/"Canva" and
its BFS neighbors displace gold turns (measured -13.3pp at session-hit-rate@k=5 on
single-session-preference questions; the filter restores that to 0pp).

The fix was shipped with a pre-registered metric but no test gated it — this closes
that §3/§11 gap. The test exercises the real _ENTITY_MENTION_RE + _QUERY_STARTER_
STOPWORDS from memory.graph (no DB / embedder needed), replicating the exact Step-1
seed-extraction loop so a regression in either the regex or the stoplist is caught.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory.graph import _ENTITY_MENTION_RE, _QUERY_STARTER_STOPWORDS  # noqa: E402


def _extract_seeds(query: str) -> list[str]:
    """Mirror of the Step-1 seed-extraction loop in _entity_graph_neighbor_ids.

    Kept in lockstep with memory/graph.py: if that loop's filter condition changes,
    update here too — the point is to assert the observable seed list, using the
    real regex and stoplist objects (not copies) so drift in either is caught.
    """
    candidates: list[str] = []
    seen_cands: set[str] = set()
    for m in _ENTITY_MENTION_RE.finditer(query):
        text = m.group(0).strip("\"'")
        if text and text not in seen_cands and text not in _QUERY_STARTER_STOPWORDS:
            seen_cands.add(text)
            candidates.append(text)
    return candidates


# ── the stoplist itself ──────────────────────────────────────────────────────

def test_stoplist_is_nonempty_frozenset():
    assert isinstance(_QUERY_STARTER_STOPWORDS, frozenset)
    assert len(_QUERY_STARTER_STOPWORDS) >= 40  # 43 at time of writing


@pytest.mark.parametrize("word", ["Can", "What", "How", "Why", "Is", "Do", "I", "You", "Please", "Hello"])
def test_common_query_starters_are_in_stoplist(word):
    assert word in _QUERY_STARTER_STOPWORDS


# ── observable filtering behavior on real queries ────────────────────────────

@pytest.mark.parametrize(
    "query, must_keep, must_drop",
    [
        # The canonical regression case: "Can" must not survive to match "Canada".
        ("Can you recommend a show like Stranger Things",
         ["Stranger Things"], ["Can"]),
        # First-person + question starter dropped; real place kept.
        ("What should I serve at my dinner party in Canada",
         ["Canada"], ["What", "I"]),
        # "How"/"I" dropped, product name kept.
        ("How do I reset my Peloton",
         ["Peloton"], ["How", "I"]),
    ],
)
def test_starters_filtered_real_entities_survive(query, must_keep, must_drop):
    seeds = _extract_seeds(query)
    for kept in must_keep:
        assert kept in seeds, f"expected entity {kept!r} in seeds: {seeds}"
    for dropped in must_drop:
        assert dropped not in seeds, f"stopword {dropped!r} leaked into seeds: {seeds}"


def test_query_of_only_starters_yields_no_seeds():
    # A purely conversational opener should produce zero entity-graph seeds,
    # so the BFS short-circuits instead of chasing spurious neighbors.
    assert _extract_seeds("Can you help me") == []


def test_proper_noun_not_in_stoplist_is_kept():
    # Sanity: a genuine Title-Case entity that is NOT a starter must survive.
    seeds = _extract_seeds("Tell me about Barcelona")
    assert "Barcelona" in seeds
    assert "Tell" not in seeds  # "Tell" is a stoplisted imperative starter
