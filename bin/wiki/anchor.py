"""Knowledge Anchor Report — a linear scoring framework for compiled-wiki health.

Every metric here is the SAME KIND of object: a per-unit score that is a weighted
sum of normalized factors,

    score(unit) = Σ_i  w_i · f_i(unit)          with each f_i in [0, 1]

so the metrics share one algebra. FACTORS are fixed, measured, deterministic (what
we observe); WEIGHTS are the tunable config with published defaults (what we
value). A metric with a single factor (Coverage) is just the n=1 case — no special
casing. The "unit" differs by metric — a cluster, the corpus, or a cluster pair —
but the scoring KERNEL (`weighted_score`) is uniform and tested once.

Because a weighted sum is only meaningful if the factors are genuinely comparable,
each factor is normalized to [0, 1] with a documented mapping; that normalization
is the real design work, not the algebra.

Metrics:
  - KAS (per cluster)      : is a topic anchored by real relationships, or adrift?
  - Coverage (corpus)      : did high-value memories reach real topics, or orphan?
  - Staleness (per cluster): are a topic's sources superseded / aged out?
  - Redundancy (pairs)     : are two topics really one?

All pure: no model, no new query, deterministic (inside the drift-tested surface).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ── the kernel ───────────────────────────────────────────────────────────────


def weighted_score(factors: "dict[str, float]", weights: "dict[str, float]") -> float:
    """score = Σ w_i·f_i over the factors present in BOTH dicts, divided by the
    total weight applied — so the result stays in [0,1] when every f_i is in [0,1],
    regardless of how many factors a metric declares or how the weights are scaled.

    Normalizing by the applied weight (not a fixed Σw) means a metric can omit a
    factor for a unit (e.g. a factor that's undefined for a 1-member cluster) and
    the score still reads on the same 0–1 dial. Weights are clamped to >= 0.
    """
    num = 0.0
    wsum = 0.0
    for name, f in factors.items():
        w = weights.get(name, 0.0)
        if w <= 0.0:
            continue
        num += w * _clamp01(f)
        wsum += w
    return 0.0 if wsum == 0.0 else num / wsum


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _saturate(x: float, half: float) -> float:
    """Map a non-negative unbounded quantity to [0,1) with a saturating curve that
    reaches 0.5 at x==half. x/(x+half). Used to normalize "more is better but with
    diminishing returns" factors (backbone strength) so a huge topic doesn't need
    proportionally huge structure to score well."""
    if x <= 0.0 or half <= 0.0:
        return 0.0
    return x / (x + half)


_CO_MENTION_REL = "co_mentions"
_PROVENANCE_RELS = ("consolidates", "distills_from")


# ── KAS ──────────────────────────────────────────────────────────────────────
#
# Factors (each normalized to [0,1]):
#   backbone   : saturating strength of load-bearing edges per member — "is there
#                real connective tissue?" (the core anchor signal)
#   provenance : share of internal edges that are consolidates/distills_from —
#                "is it grounded in sources, not just loosely 'related'?"
#   confidence : mean member confidence — "built from trustworthy memories?"
#   backbone_ratio : load-bearing edges / all internal edges — "NOT propped up by
#                synthetic co-mention bridges" (0 when a cluster is pure co-mention)
#
# Weights default to _DEFAULT_KAS_WEIGHTS, set from the live corpus (see the
# calibration note where they're defined). All tunable via KASWeights.


@dataclass(frozen=True)
class KASWeights:
    """Tunable weights for the four KAS factors, plus the backbone saturation
    half-point. Defaults are the published m3 reference calibration (set from a
    real corpus); every field is overridable so a deployment — or an adopter
    making KAS its own field metric — can recalibrate without editing code.

    The four weights need not sum to 1 (the kernel normalizes by applied weight);
    they express RELATIVE importance.

    CALIBRATION (2026-07-24, from a 3,337-memory / 39-cluster live corpus): weights
    are proportional to each factor's DISCRIMINATING POWER (per-cluster stdev), not
    guessed. A factor that barely varies across clusters adds only offset, not
    signal, so it earns little weight even if conceptually important. Measured
    stdev: provenance 0.166, backbone_ratio 0.153, backbone 0.042, confidence
    0.026. Hence provenance + backbone_ratio (the factors that actually separate
    coherent topics from over-merged ones) carry ~75% of the weight; backbone and
    confidence are low but kept — backbone is the base "is there any structure",
    and confidence is near-inert ONLY because this corpus lacks confidence spread
    (belief/consolidates rollups will populate it), so it's held for that future.
    A placeholder that over-weighted backbone gave KAS stdev 0.013 (flat, useless);
    this calibration gives 0.039 — 3x the ranking power."""
    backbone: float = 0.15
    provenance: float = 0.40
    confidence: float = 0.10
    backbone_ratio: float = 0.35
    # normalization control: backbone strength that maps to 0.5 (saturation).
    backbone_half: float = 2.0
    # confidence assumed for a member with no rating (below the corpus mid so an
    # unrated memory anchors slightly LESS than a typical one).
    null_confidence: float = 0.4

    def as_weights(self) -> "dict[str, float]":
        return {"backbone": self.backbone, "provenance": self.provenance,
                "confidence": self.confidence, "backbone_ratio": self.backbone_ratio}

    @classmethod
    def from_tuple(cls, t: tuple) -> "KASWeights":
        """(backbone, provenance, confidence, backbone_ratio) — the four factor
        weights, positionally. Saturation/null-confidence keep defaults."""
        names = ("backbone", "provenance", "confidence", "backbone_ratio")
        return cls(**{n: t[i] for i, n in enumerate(names) if i < len(t)})


_DEFAULT_KAS = KASWeights()


@dataclass
class ClusterAnchor:
    key: str
    title: str
    members: int
    kas: float                       # the linear score in [0,1]
    factors: dict = field(default_factory=dict)   # the raw normalized factors
    load_bearing_edges: int = 0
    comention_edges: int = 0
    adrift: bool = False
    drift_entities: list = field(default_factory=list)


def kas_factors(cluster, edges, kw: "Optional[KASWeights]" = None,
                edge_weights: "Optional[dict]" = None) -> "tuple[dict, int, int]":
    """Compute the four normalized KAS factors for a cluster. Returns
    (factors, load_bearing_edge_count, comention_edge_count)."""
    kw = kw or _DEFAULT_KAS
    if edge_weights is None:
        from .select import EDGE_WEIGHTS
        edge_weights = EDGE_WEIGHTS

    member_ids = {m.id for m in cluster.members}
    n = max(1, len(cluster.members))
    conf = {m.id: (m.confidence if m.confidence is not None else kw.null_confidence)
            for m in cluster.members}

    backbone_strength = 0.0
    load_bearing = 0
    comention = 0
    provenance_edges = 0
    for e in edges:
        if e.from_id not in member_ids or e.to_id not in member_ids:
            continue
        if e.rel == _CO_MENTION_REL:
            comention += 1
            continue
        load_bearing += 1
        if e.rel in _PROVENANCE_RELS:
            provenance_edges += 1
        backbone_strength += edge_weights.get(e.rel, 1.0)

    total_internal = load_bearing + comention
    mean_conf = (sum(conf.values()) / n) if conf else kw.null_confidence

    factors = {
        # saturating backbone strength per member.
        "backbone": _saturate(backbone_strength / n, kw.backbone_half),
        # share of real edges that are provenance-grade.
        "provenance": (provenance_edges / load_bearing) if load_bearing else 0.0,
        # mean member confidence (already 0–1).
        "confidence": _clamp01(mean_conf),
        # real vs synthetic edge share.
        "backbone_ratio": (load_bearing / total_internal) if total_internal else 0.0,
    }
    return factors, load_bearing, comention


def cluster_anchor(cluster, edges, kas: "Optional[KASWeights] | dict" = None,
                   edge_weights: "Optional[dict]" = None) -> ClusterAnchor:
    """KAS for one cluster as a weighted sum of its four normalized factors. A
    bare edge-weights dict is accepted in the `kas` slot for back-compat (treated
    as edge_weights, default KAS weights)."""
    if isinstance(kas, dict):
        edge_weights = edge_weights or kas
        kw = _DEFAULT_KAS
    else:
        kw = kas or _DEFAULT_KAS
    factors, load_bearing, comention = kas_factors(cluster, edges, kw, edge_weights)
    score = weighted_score(factors, kw.as_weights())
    return ClusterAnchor(
        key=cluster.key,
        title=cluster.members[0].display_title if cluster.members else cluster.key,
        members=len(cluster.members),
        kas=round(score, 4),
        factors={k: round(v, 4) for k, v in factors.items()},
        load_bearing_edges=load_bearing,
        comention_edges=comention,
        adrift=(load_bearing == 0 and len(cluster.members) >= 2),
    )


def _flag_low_anchor(anchors) -> list:
    """Self-calibrating: adrift always flagged; otherwise bottom-fifth of the run
    AND below half the run's median KAS, once >= 5 clusters give a distribution.
    Never cries wolf on a small or uniformly-good corpus."""
    multi = [a for a in anchors if a.members >= 2]
    keys = {a.key for a in multi if a.adrift}
    scored = sorted(a.kas for a in multi if not a.adrift)
    if len(scored) >= 5:
        median = scored[len(scored) // 2]
        p20 = scored[max(0, int(len(scored) * 0.20) - 1)]
        for a in multi:
            if not a.adrift and a.kas <= p20 and a.kas < 0.5 * median:
                keys.add(a.key)
    return sorted(keys)


# ── Coverage (corpus, single-factor: the n=1 case, and that's fine) ──────────


@dataclass(frozen=True)
class CoverageWeights:
    representation: float = 1.0     # one factor; the algebra's degenerate case
    importance_floor: float = 0.7


def coverage_score(clusters, cw: "Optional[CoverageWeights]" = None
                   ) -> "tuple[float, int, int]":
    """Fraction of high-importance memories in real topics vs. orphaned. Returns
    (score, covered, orphaned). Genuinely one factor — expressed in the same
    weighted-sum kernel for uniformity, not padded with invented factors."""
    cw = cw or CoverageWeights()
    covered = orphaned = 0
    for c in clusters:
        for m in c.members:
            if (m.importance or 0.0) < cw.importance_floor:
                continue
            if getattr(c, "is_orphan", False):
                orphaned += 1
            else:
                covered += 1
    total = covered + orphaned
    representation = 1.0 if total == 0 else covered / total
    score = weighted_score({"representation": representation},
                           {"representation": cw.representation})
    return round(score, 4), covered, orphaned


# ── Staleness (per cluster) ──────────────────────────────────────────────────
#
# Factors: stale_fraction (share of members superseded/aged). Single dominant
# factor today; kept in the kernel so a second factor (e.g. age-of-newest-source)
# can be added later without changing shape.


@dataclass(frozen=True)
class StalenessWeights:
    stale_fraction: float = 1.0
    threshold: float = 0.5          # flag clusters scoring above this


def cluster_staleness(cluster, sw: "Optional[StalenessWeights]" = None) -> float:
    sw = sw or StalenessWeights()
    if not cluster.members:
        return 0.0
    stale = sum(1 for m in cluster.members if m.valid_to)
    frac = stale / len(cluster.members)
    return round(weighted_score({"stale_fraction": frac},
                                {"stale_fraction": sw.stale_fraction}), 4)


def stale_topics(clusters, sw: "Optional[StalenessWeights]" = None) -> list:
    sw = sw or StalenessWeights()
    out = []
    for c in clusters:
        if getattr(c, "is_orphan", False) or not c.members:
            continue
        s = cluster_staleness(c, sw)
        if s >= sw.threshold:
            out.append((c.key, s))
    out.sort(key=lambda t: -t[1])
    return out


# ── Redundancy (cluster pairs) ───────────────────────────────────────────────
#
# A pairwise metric: each PAIR gets a weighted-sum score over similarity factors.
# Factor today: member Jaccard. A synthesis-embedding-similarity factor can be
# added with a weight later — same kernel, no reshape.


@dataclass(frozen=True)
class RedundancyWeights:
    member_overlap: float = 1.0
    threshold: float = 0.6          # flag pairs scoring above this


def redundant_pairs(clusters, rw: "Optional[RedundancyWeights]" = None) -> list:
    rw = rw or RedundancyWeights()
    sets = [(c.key, {m.id for m in c.members}) for c in clusters
            if not getattr(c, "is_orphan", False) and len(c.members) >= 2]
    out = []
    for i in range(len(sets)):
        ka, sa = sets[i]
        for j in range(i + 1, len(sets)):
            kb, sb = sets[j]
            inter = len(sa & sb)
            if not inter:
                continue
            jac = inter / len(sa | sb)
            score = weighted_score({"member_overlap": jac},
                                   {"member_overlap": rw.member_overlap})
            if score >= rw.threshold:
                a, b = sorted((ka, kb))
                out.append((a, b, round(score, 4)))
    out.sort(key=lambda t: -t[2])
    return out


# ── the full report ──────────────────────────────────────────────────────────


@dataclass
class WikiHealth:
    anchors: list = field(default_factory=list)
    low_anchor: list = field(default_factory=list)
    coverage: float = 1.0
    covered: int = 0
    orphaned_high_value: int = 0
    stale: list = field(default_factory=list)
    redundant_pairs: list = field(default_factory=list)


def annotate_drift_entities(anchors, clusters, comention_entities) -> None:
    """For each ADRIFT cluster, name the co-mention entities still bridging its
    members — points at the next entity filter. Optional; None keeps it query-free."""
    if not comention_entities:
        return
    by_key = {c.key: {m.id for m in c.members} for c in clusters}
    for a in anchors:
        if not a.adrift:
            continue
        ids = by_key.get(a.key, set())
        a.drift_entities = sorted(
            name for name, members in comention_entities.items()
            if len(members & ids) >= 2)[:8]


def build_health(clusters, edges, kas: "Optional[KASWeights] | dict" = None,
                 comention_entities=None, *, edge_weights: "Optional[dict]" = None,
                 coverage_w: "Optional[CoverageWeights]" = None,
                 staleness_w: "Optional[StalenessWeights]" = None,
                 redundancy_w: "Optional[RedundancyWeights]" = None) -> WikiHealth:
    """Assemble the Knowledge Anchor Report from the four linear metrics. Pure,
    deterministic, no queries. Each metric's weights are independently tunable; a
    bare edge-weights dict in `kas` is accepted for back-compat."""
    if isinstance(kas, dict):
        edge_weights = edge_weights or kas
        kw = _DEFAULT_KAS
    else:
        kw = kas or _DEFAULT_KAS
    anchors = [cluster_anchor(c, edges, kw, edge_weights)
               for c in clusters if not getattr(c, "is_orphan", False)]
    annotate_drift_entities(anchors, clusters, comention_entities)
    frac, covered, orphaned = coverage_score(clusters, coverage_w)
    return WikiHealth(
        anchors=anchors,
        low_anchor=_flag_low_anchor(anchors),
        coverage=frac,
        covered=covered,
        orphaned_high_value=orphaned,
        stale=stale_topics(clusters, staleness_w),
        redundant_pairs=redundant_pairs(clusters, redundancy_w),
    )
