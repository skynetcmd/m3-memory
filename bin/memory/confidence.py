"""Confidence & trust math for m3-memory — PURE functions only.

This module is deliberately free of DB, I/O, and global state so the confidence
model can be unit-tested exhaustively and reasoned about in isolation. Every
function is total, bounded, and deterministic.

Two representations live here (the "hybrid" model from the knowledge-maintenance
plan):

1. **Transparent aggregation** (`aggregate`) — the user-facing `confidence` in
   [0,1]. A provenance prior plus a diminishing corroboration bonus minus a
   capped contradiction penalty. Inspectable and explainable.

2. **Bayesian posterior** (`beta_update`, `beta_mean`) — an optional Beta(α,β)
   kept alongside for ranking experiments. Corroboration adds to α, contradiction
   to β. Never the displayed number; consulted only when
   M3_CONFIDENCE_MODEL=bayesian.

Tuning constants live here as module-level literals (not env-driven) because they
are model parameters, not deployment knobs — the deployment knobs (weights,
on/off) live in config.py.
"""
from __future__ import annotations

# ── Provenance priors ────────────────────────────────────────────────────────
# Base confidence implied by *where a memory came from*, before any
# corroboration/contradiction signal. Reuses fields that already exist on
# memory_items (source, change_agent) plus the Observer SLM's own confidence.
USER_PRIOR: float = 0.95          # source='user' or change_agent='manual'
AGENT_PRIOR: float = 0.70         # an agent asserted it (scaled by agent trust)
INTERNET_PRIOR: float = 0.40      # source='internet' / web research
NEUTRAL_PRIOR: float = 0.50       # unknown — matches importance's neutral default

# ── Aggregation caps (keep any single signal from dominating) ────────────────
CORROBORATION_UNIT: float = 0.05  # per unit of distinct-source trust
CORROBORATION_CAP: float = 0.20   # max total corroboration bonus
CONTRADICTION_UNIT: float = 0.10  # per contradiction
CONTRADICTION_CAP: float = 0.30   # max total contradiction penalty

# ── Reinforcement / decay (Phase 3) ──────────────────────────────────────────
NEUTRAL: float = 0.50             # the floor confidence decays TOWARD (not 0)
DECAY_RATE: float = 0.02          # per pass: confidence moves this fraction of
#                                   the distance toward NEUTRAL when un-reinforced
ACCESS_REINFORCE_UNIT: float = 0.01   # weak positive per access-bucket
ACCESS_REINFORCE_CAP: float = 0.05    # hard cap so "frequently read" != "corroborated"

# ── Trust bounds ─────────────────────────────────────────────────────────────
TRUST_MIN: float = 0.5
TRUST_MAX: float = 1.0
TRUST_NEUTRAL: float = 1.0


def clamp01(x: float) -> float:
    """Clamp a value into the closed unit interval [0, 1]."""
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def clamp_trust(x: float) -> float:
    """Clamp a trust score into [TRUST_MIN, TRUST_MAX]."""
    if x < TRUST_MIN:
        return TRUST_MIN
    if x > TRUST_MAX:
        return TRUST_MAX
    return x


def base_source_conf(
    source: str = "",
    change_agent: str = "",
    observer_confidence: float | None = None,
    agent_trust: float = TRUST_NEUTRAL,
) -> float:
    """Provenance prior for a single memory, in [0, 1].

    Resolution order (most authoritative first):
      1. An explicit Observer-SLM confidence (already 0.6–1.0) is trusted as-is.
      2. A human/manual origin → USER_PRIOR.
      3. Internet/web origin → INTERNET_PRIOR.
      4. An agent assertion → AGENT_PRIOR scaled by that agent's trust.
      5. Otherwise NEUTRAL_PRIOR.

    `agent_trust` only matters for the agent-assertion branch; it is clamped.
    """
    if observer_confidence is not None:
        return clamp01(observer_confidence)

    s = (source or "").strip().lower()
    ca = (change_agent or "").strip().lower()

    if s == "user" or ca == "manual":
        return USER_PRIOR
    if s in ("internet", "web", "web_research"):
        return INTERNET_PRIOR
    # An identifiable agent asserted it (claude/gemini/… or any non-empty,
    # non-system change_agent). Scale the agent prior by trust.
    if ca and ca not in ("unknown", "system"):
        return clamp01(AGENT_PRIOR * clamp_trust(agent_trust))
    return NEUTRAL_PRIOR


def corroboration_bonus(distinct_trust_sum: float) -> float:
    """Diminishing, capped bonus for independent corroboration.

    `distinct_trust_sum` is the summed trust of *distinct* corroborating sources
    (so three agreeing agents contribute more than one, weighted by their trust).
    Bounded by CORROBORATION_CAP so corroboration can lift but not overwhelm a
    high provenance prior.
    """
    if distinct_trust_sum <= 0.0:
        return 0.0
    return min(CORROBORATION_CAP, CORROBORATION_UNIT * distinct_trust_sum)


def contradiction_penalty(contradiction_count: int) -> float:
    """Capped penalty for recorded contradictions against a memory."""
    if contradiction_count <= 0:
        return 0.0
    return min(CONTRADICTION_CAP, CONTRADICTION_UNIT * float(contradiction_count))


def aggregate(
    *,
    source: str = "",
    change_agent: str = "",
    observer_confidence: float | None = None,
    agent_trust: float = TRUST_NEUTRAL,
    distinct_trust_sum: float = 0.0,
    contradiction_count: int = 0,
) -> float:
    """The transparent, user-facing confidence in [0, 1].

        confidence = clamp01(
            base_source_conf
          + corroboration_bonus(distinct_trust_sum)
          - contradiction_penalty(contradiction_count)
        )
    """
    base = base_source_conf(
        source=source,
        change_agent=change_agent,
        observer_confidence=observer_confidence,
        agent_trust=agent_trust,
    )
    return clamp01(
        base
        + corroboration_bonus(distinct_trust_sum)
        - contradiction_penalty(contradiction_count)
    )


# ── Reinforcement (Phase 3) ───────────────────────────────────────────────────

def decay_toward_neutral(confidence: float, floor: float = NEUTRAL) -> float:
    """Move a confidence one step toward NEUTRAL (un-reinforced decay).

    Unlike importance decay (which heads to 0), confidence forgets toward
    *uncertainty* — a fact nobody has reconfirmed becomes less certain, not
    worthless. `floor` is a corroborated lower bound the decay never crosses
    (so a well-corroborated fact above NEUTRAL still can't be dragged below its
    earned floor; a low-confidence fact below NEUTRAL rises toward it). Idempotent
    at the fixed point (confidence == NEUTRAL) and monotone (never overshoots).
    """
    c = clamp01(confidence)
    target = NEUTRAL
    stepped = c + (target - c) * DECAY_RATE
    # Respect a corroborated floor: never decay an above-neutral fact below it.
    if floor > NEUTRAL:
        stepped = max(stepped, min(c, clamp01(floor)))
    return clamp01(stepped)


def access_reinforcement(access_count: int) -> float:
    """Weak, hard-capped positive signal from repeated retrieval. Logarithmic so
    a hot fact can't masquerade as a corroborated one — capped at
    ACCESS_REINFORCE_CAP regardless of how often it's read."""
    if access_count <= 0:
        return 0.0
    # log2(1+n) buckets: 1->1, 3->2, 7->3, 15->4 ... times the unit, capped.
    import math
    buckets = math.log2(1.0 + access_count)
    return min(ACCESS_REINFORCE_CAP, ACCESS_REINFORCE_UNIT * buckets)


# ── Bayesian (Beta) posterior — optional, ranking-experiments only ────────────

BETA_PRIOR_ALPHA: float = 1.0
BETA_PRIOR_BETA: float = 1.0


def beta_seed(confidence: float, strength: float = 2.0) -> tuple[float, float]:
    """Seed a Beta(α,β) from an initial transparent confidence.

    `strength` is the pseudo-count mass: α+β = strength, mean = confidence.
    Keeps α,β strictly positive so the distribution is always proper.
    """
    c = clamp01(confidence)
    s = max(strength, 1e-6)
    alpha = max(c * s, 1e-6)
    beta = max((1.0 - c) * s, 1e-6)
    return alpha, beta


def beta_update(
    alpha: float,
    beta: float,
    *,
    corroboration_weight: float = 0.0,
    contradiction_weight: float = 0.0,
) -> tuple[float, float]:
    """Update a Beta posterior: corroboration → α, contradiction → β.

    Weights are typically the trust of the asserting/contradicting source.
    Negative weights are ignored (treated as 0) so the posterior never shrinks.
    """
    a = max(alpha, 1e-6) + max(corroboration_weight, 0.0)
    b = max(beta, 1e-6) + max(contradiction_weight, 0.0)
    return a, b


def beta_mean(alpha: float, beta: float) -> float:
    """Posterior mean α/(α+β), clamped to [0,1]."""
    a = max(alpha, 1e-6)
    b = max(beta, 1e-6)
    return clamp01(a / (a + b))
