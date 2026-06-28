"""Exhaustive unit tests for bin/memory/confidence.py — the pure confidence model.

These pin every branch of the transparent aggregation and the Beta posterior so
later phases (write-path, ranking, reinforcement) build on a proven foundation.
No DB, no I/O — pure math.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory import confidence as C  # noqa: E402

# ── clamps ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("x,expected", [
    (-1.0, 0.0), (-0.0001, 0.0), (0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (1.5, 1.0),
])
def test_clamp01(x, expected):
    assert C.clamp01(x) == expected


@pytest.mark.parametrize("x,expected", [
    (0.0, 0.5), (0.4, 0.5), (0.5, 0.5), (0.75, 0.75), (1.0, 1.0), (2.0, 1.0),
])
def test_clamp_trust(x, expected):
    assert C.clamp_trust(x) == expected


# ── base_source_conf: every resolution branch ────────────────────────────────

def test_observer_confidence_wins_and_is_clamped():
    # Observer value trusted as-is, regardless of source/change_agent...
    assert C.base_source_conf(source="internet", observer_confidence=0.83) == 0.83
    # ...but still clamped into [0,1].
    assert C.base_source_conf(observer_confidence=1.4) == 1.0
    assert C.base_source_conf(observer_confidence=-0.2) == 0.0


def test_user_and_manual_prior():
    assert C.base_source_conf(source="user") == C.USER_PRIOR
    assert C.base_source_conf(change_agent="manual") == C.USER_PRIOR
    assert C.base_source_conf(source="USER") == C.USER_PRIOR  # case-insensitive


@pytest.mark.parametrize("src", ["internet", "web", "web_research", "Internet"])
def test_internet_prior(src):
    assert C.base_source_conf(source=src) == C.INTERNET_PRIOR


def test_agent_prior_scaled_by_trust():
    # Neutral trust → exactly AGENT_PRIOR.
    assert C.base_source_conf(change_agent="claude") == C.AGENT_PRIOR
    # Lower trust scales the prior down.
    assert C.base_source_conf(change_agent="gemini", agent_trust=0.5) == pytest.approx(
        C.AGENT_PRIOR * 0.5)
    # Trust is clamped before scaling (2.0 → 1.0).
    assert C.base_source_conf(change_agent="claude", agent_trust=2.0) == C.AGENT_PRIOR


@pytest.mark.parametrize("ca", ["", "unknown", "system"])
def test_neutral_prior_for_unknown_or_system(ca):
    assert C.base_source_conf(change_agent=ca) == C.NEUTRAL_PRIOR


# ── corroboration / contradiction ────────────────────────────────────────────

def test_corroboration_bonus_zero_and_negative():
    assert C.corroboration_bonus(0.0) == 0.0
    assert C.corroboration_bonus(-3.0) == 0.0


def test_corroboration_bonus_linear_then_capped():
    assert C.corroboration_bonus(1.0) == pytest.approx(C.CORROBORATION_UNIT)
    assert C.corroboration_bonus(2.0) == pytest.approx(2 * C.CORROBORATION_UNIT)
    # Far past the cap → capped.
    assert C.corroboration_bonus(1000.0) == C.CORROBORATION_CAP


def test_contradiction_penalty_zero_and_capped():
    assert C.contradiction_penalty(0) == 0.0
    assert C.contradiction_penalty(-1) == 0.0
    assert C.contradiction_penalty(1) == pytest.approx(C.CONTRADICTION_UNIT)
    assert C.contradiction_penalty(100) == C.CONTRADICTION_CAP


# ── aggregate: the user-facing number ────────────────────────────────────────

def test_aggregate_neutral_default_matches_importance_default():
    # No provenance, no signal → NEUTRAL_PRIOR (0.5), exactly today's importance
    # default, so un-enriched memories are unaffected.
    assert C.aggregate() == C.NEUTRAL_PRIOR


def test_aggregate_user_with_corroboration_capped_at_one():
    # User prior 0.95 + corroboration can't exceed 1.0.
    val = C.aggregate(source="user", distinct_trust_sum=100.0)
    assert val == 1.0


def test_aggregate_agent_corroborated_beats_solo_agent():
    solo = C.aggregate(change_agent="claude")
    corroborated = C.aggregate(change_agent="claude", distinct_trust_sum=2.0)
    assert corroborated > solo


def test_aggregate_contradiction_lowers_confidence():
    clean = C.aggregate(change_agent="claude")
    contradicted = C.aggregate(change_agent="claude", contradiction_count=3)
    assert contradicted < clean
    assert contradicted >= 0.0


def test_aggregate_never_exceeds_bounds_under_extremes():
    lo = C.aggregate(source="internet", contradiction_count=10_000)
    hi = C.aggregate(source="user", distinct_trust_sum=10_000.0)
    assert 0.0 <= lo <= 1.0
    assert 0.0 <= hi <= 1.0


def test_corroboration_cannot_overwhelm_user_prior_with_agent_base():
    # Three agreeing agents (cap 0.20) lift an agent-based fact to at most
    # AGENT_PRIOR + 0.20 = 0.90 — still below a bare user statement (0.95).
    agents_consensus = C.aggregate(change_agent="claude", distinct_trust_sum=100.0)
    user_solo = C.aggregate(source="user")
    assert agents_consensus <= C.AGENT_PRIOR + C.CORROBORATION_CAP
    assert agents_consensus < user_solo


# ── Beta posterior ───────────────────────────────────────────────────────────

def test_beta_seed_mean_matches_confidence():
    a, b = C.beta_seed(0.8, strength=10.0)
    assert C.beta_mean(a, b) == pytest.approx(0.8, abs=1e-9)
    assert a > 0 and b > 0


def test_beta_seed_extremes_stay_proper():
    for c in (0.0, 1.0):
        a, b = C.beta_seed(c)
        assert a > 0 and b > 0  # never degenerate


def test_beta_update_corroboration_raises_mean():
    a, b = C.beta_seed(0.5, strength=2.0)
    a2, b2 = C.beta_update(a, b, corroboration_weight=5.0)
    assert C.beta_mean(a2, b2) > C.beta_mean(a, b)


def test_beta_update_contradiction_lowers_mean():
    a, b = C.beta_seed(0.5, strength=2.0)
    a2, b2 = C.beta_update(a, b, contradiction_weight=5.0)
    assert C.beta_mean(a2, b2) < C.beta_mean(a, b)


def test_beta_update_ignores_negative_weights():
    a, b = C.beta_seed(0.5, strength=2.0)
    a2, b2 = C.beta_update(a, b, corroboration_weight=-9.0, contradiction_weight=-9.0)
    assert (a2, b2) == pytest.approx((a, b))


def test_beta_mean_clamped_and_safe_on_degenerate_input():
    # Even with zero/negative inputs the mean stays in [0,1] (1e-6 guards).
    m = C.beta_mean(0.0, 0.0)
    assert 0.0 <= m <= 1.0
