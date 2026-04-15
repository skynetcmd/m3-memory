import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory_core import _apply_recency_bonus


def _mk(score, valid_from, id_="x"):
    return (score, {"id": id_, "valid_from": valid_from, "content": id_})


def test_bias_zero_is_noop():
    scored = [_mk(0.6, "2026-01-01"), _mk(0.5, "2026-03-01")]
    out = _apply_recency_bonus(scored, recency_bias=0.0)
    assert out == scored


def test_empty_input():
    assert _apply_recency_bonus([], recency_bias=0.05) == []


def test_fewer_than_two_dated_returns_unchanged():
    # Only one dated item -> no basis for interpolation.
    scored = [_mk(0.6, "2026-01-01", "a"), _mk(0.5, "", "b")]
    out = _apply_recency_bonus(scored, recency_bias=0.05)
    assert out == scored


def test_newest_gets_full_bonus_oldest_gets_none():
    a = _mk(0.60, "2026-01-01", "a")  # oldest
    b = _mk(0.60, "2026-02-01", "b")  # middle
    c = _mk(0.60, "2026-03-01", "c")  # newest
    out = _apply_recency_bonus([a, b, c], recency_bias=0.10)
    new_scores = {item["id"]: s for s, item in out}
    assert new_scores["a"] == pytest.approx(0.60)
    assert new_scores["b"] == pytest.approx(0.65)
    assert new_scores["c"] == pytest.approx(0.70)


def test_undated_item_gets_no_bonus():
    dated_old = _mk(0.60, "2026-01-01", "old")
    dated_new = _mk(0.60, "2026-03-01", "new")
    undated = _mk(0.60, "", "undated")
    out = _apply_recency_bonus([dated_old, dated_new, undated], recency_bias=0.10)
    new_scores = {item["id"]: s for s, item in out}
    assert new_scores["old"] == pytest.approx(0.60)
    assert new_scores["new"] == pytest.approx(0.70)
    assert new_scores["undated"] == pytest.approx(0.60)


def test_bonus_can_flip_close_scores():
    # The point of the feature: a newer item with a slightly lower base
    # score should be promoted above an older item.
    old_high = _mk(0.628, "2026-02-28", "director")
    new_low = _mk(0.570, "2026-04-01", "vp")
    out = _apply_recency_bonus([old_high, new_low], recency_bias=0.10)
    sorted_by_score = sorted(out, key=lambda x: x[0], reverse=True)
    assert sorted_by_score[0][1]["id"] == "vp"


def test_ties_on_same_valid_from_are_stable():
    # Two items with identical valid_from should both receive the same
    # rank-based bonus (whatever rank they happen to land at in the sort).
    a = _mk(0.60, "2026-02-01", "a")
    b = _mk(0.60, "2026-02-01", "b")
    c = _mk(0.60, "2026-03-01", "c")
    out = _apply_recency_bonus([a, b, c], recency_bias=0.10)
    bonuses = sorted(s - 0.60 for s, _ in out)
    # Three rank positions: 0, 0.5, 1.0 of 0.10. Two tied entries get the
    # two lowest rank slots (not identical, but both below the newest).
    assert bonuses[-1] == pytest.approx(0.10)  # newest always gets full bonus
    assert bonuses[0] == pytest.approx(0.0)
    assert bonuses[1] == pytest.approx(0.05)


def test_explain_mode_annotates_items():
    a = (0.60, {"id": "a", "valid_from": "2026-01-01", "_explanation": {}})
    b = (0.60, {"id": "b", "valid_from": "2026-03-01", "_explanation": {}})
    out = _apply_recency_bonus([a, b], recency_bias=0.10, explain=True)
    assert out[0][1]["_explanation"]["recency_bonus"] == pytest.approx(0.0)
    assert out[1][1]["_explanation"]["recency_bonus"] == pytest.approx(0.10)
