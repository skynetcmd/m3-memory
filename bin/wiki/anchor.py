"""Knowledge Anchor Report — deterministic health metrics for the compiled wiki.

The headline metric is the **Knowledge Anchor Score (KAS)**: is a topic held
together by REAL, load-bearing connections, or merely by incidental co-mention?
It improves on plain link-density (which treats a cluster fused by 20 weak
co-mention bridges as MORE cohesive than one held by 3 strong `consolidates`
edges — backwards, and blind to exactly the over-merge failure this project just
fixed). KAS weights by edge TYPE and by endpoint CONFIDENCE, and is normalized
per member (not by the near-impossible n² complete-graph baseline that forces a
magic threshold).

Three co-metrics round out the report, each the same shape — a pure function over
data already loaded (clusters, edges, member metadata), zero new queries,
deterministic so it lives inside the drift-tested surface:

  - KAS (per cluster)   : is each topic anchored, or adrift?
  - Coverage (corpus)   : did high-value memories reach real topics, or orphan?
  - Staleness (per cluster): are a topic's sources superseded / aged out?
  - Redundancy (pairs)  : are two topics really one (high member overlap)?

None call a model — a health metric that needs an LLM becomes the flaky thing it
is meant to catch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# The one synthetic edge type. Everything else is "load-bearing" — a real
# relationship (hand-authored or provenance), not an incidental co-occurrence.
_CO_MENTION_REL = "co_mentions"


@dataclass(frozen=True)
class KASWeights:
    """Tunable factors of the Knowledge Anchor Score. The defaults ARE the
    published m3 default — a defensible reference calibration — but every knob is
    configurable so a deployment (or an adopter making KAS its own field metric)
    can recalibrate to its corpus without editing code.

    KAS(cluster) = Σ over internal edges of
        (edge_type_weight × conf_factor × comention_factor)  /  normalizer

    Knobs:
      edge_weights        per-relationship-type weight (defaults to the wiki's
                          EDGE_WEIGHTS: consolidates 3.0 … related 1.0). Pass a
                          different dict to re-rank what counts as strong.
      confidence_influence  how much endpoint confidence scales an edge's
                          contribution. 1.0 = full linear (default); 0.0 = ignore
                          confidence entirely; between = dampened.
      null_confidence     confidence assumed for a member with no rating (0.5).
      comention_credit    fraction of a co-mention edge's weight that still counts
                          toward the anchor. 0.0 (default) = pure backbone, the
                          over-merge-catching behaviour; >0 gives synthetic bridges
                          partial credit.
      normalize_by        "members" (default; edges-per-member, size-fair) or
                          "possible_edges" (density-like, n·(n-1)/2 baseline).
    """
    edge_weights: Optional[dict] = None       # None → the wiki's EDGE_WEIGHTS
    confidence_influence: float = 1.0
    null_confidence: float = 0.5
    comention_credit: float = 0.0
    normalize_by: str = "members"

    def weight_for(self, rel: str) -> float:
        w = self.edge_weights if self.edge_weights is not None else _default_edge_weights()
        return w.get(rel, 1.0)

    def conf_factor(self, mean_conf: float) -> float:
        # linear blend between "confidence ignored" (1.0) and "full confidence".
        ci = self.confidence_influence
        return (1.0 - ci) + ci * mean_conf

    @classmethod
    def from_tuple(cls, t: tuple) -> "KASWeights":
        """Sugar: (confidence_influence, comention_credit[, null_confidence]).
        Edge weights and normalizer keep defaults; use the full constructor to set
        those. Positional order is documented here so the tuple is not a mystery."""
        ci = t[0] if len(t) > 0 else 1.0
        cc = t[1] if len(t) > 1 else 0.0
        nc = t[2] if len(t) > 2 else 0.5
        return cls(confidence_influence=ci, comention_credit=cc, null_confidence=nc)


def _default_edge_weights() -> dict:
    from .select import EDGE_WEIGHTS
    return EDGE_WEIGHTS


_DEFAULT_KAS = KASWeights()


@dataclass
class ClusterAnchor:
    """KAS + drift diagnostic for one topic."""
    key: str
    title: str
    members: int
    kas: float                       # knowledge anchor score (backbone / member)
    load_bearing_edges: int          # count of real (non-co-mention) internal edges
    comention_edges: int             # count of synthetic bridges
    adrift: bool                     # zero load-bearing backbone → floating
    drift_entities: list = field(default_factory=list)  # co-mention culprits, if adrift


@dataclass
class WikiHealth:
    """The full Knowledge Anchor Report."""
    anchors: list = field(default_factory=list)          # list[ClusterAnchor]
    low_anchor: list = field(default_factory=list)        # flagged topic keys
    coverage: float = 1.0                                  # 0..1
    covered: int = 0
    orphaned_high_value: int = 0
    stale: list = field(default_factory=list)             # (key, stale_fraction)
    redundant_pairs: list = field(default_factory=list)   # (key_a, key_b, overlap)


# ── KAS ──────────────────────────────────────────────────────────────────────

def cluster_anchor(cluster, edges, kas: "Optional[KASWeights]" = None) -> ClusterAnchor:
    """KAS for one cluster, per the (tunable) KASWeights. Backbone = sum over
    INTERNAL edges of (type_weight × confidence_factor × comention_factor),
    normalized. With default weights a cluster held only by co-mention scores ~0
    (adrift) — the over-merge signature; raise `comention_credit` to soften that."""
    kw = KASWeights(edge_weights=kas) if isinstance(kas, dict) else (kas or _DEFAULT_KAS)
    member_ids = {m.id for m in cluster.members}
    conf = {m.id: (m.confidence if m.confidence is not None else kw.null_confidence)
            for m in cluster.members}
    score = 0.0
    load_bearing = 0
    comention = 0
    for e in edges:
        if e.from_id not in member_ids or e.to_id not in member_ids:
            continue  # only edges INTERNAL to the cluster
        mean_conf = (conf.get(e.from_id, kw.null_confidence)
                     + conf.get(e.to_id, kw.null_confidence)) / 2.0
        contrib = kw.weight_for(e.rel) * kw.conf_factor(mean_conf)
        if e.rel == _CO_MENTION_REL:
            comention += 1
            score += contrib * kw.comention_credit  # 0 by default → no anchor
        else:
            load_bearing += 1
            score += contrib
    n = len(cluster.members)
    if kw.normalize_by == "possible_edges":
        denom = max(1, n * (n - 1) / 2)
    else:  # "members" (default)
        denom = max(1, n)
    kas_val = score / denom
    adrift = load_bearing == 0 and n >= 2
    return ClusterAnchor(
        key=cluster.key,
        title=cluster.members[0].display_title if cluster.members else cluster.key,
        members=len(cluster.members),
        kas=round(kas_val, 4),
        load_bearing_edges=load_bearing,
        comention_edges=comention,
        adrift=adrift,
    )


def _flag_low_anchor(anchors) -> list:
    """Self-calibrating flag. A cluster is low-anchor if:
      - it is ADRIFT (zero load-bearing backbone) — always flagged; or
      - its KAS sits in the bottom 20% of the run AND is a small ABSOLUTE fraction
        of the run's median, i.e. genuinely weak relative to a typical topic —
        not merely last place in a set of all-strong clusters.
    No magic constant imported from another corpus: the bar is derived from THIS
    run's own distribution, and the median-fraction guard prevents flagging a
    strong cluster just because the run is small or uniformly good.
    """
    multi = [a for a in anchors if a.members >= 2]
    keys = {a.key for a in multi if a.adrift}
    scored = sorted(a.kas for a in multi if not a.adrift)
    # Need a real distribution (≥5 non-adrift clusters) before percentile-flagging;
    # below that, only adrift clusters are flagged (no basis for "relatively weak").
    if len(scored) >= 5:
        median = scored[len(scored) // 2]
        idx = max(0, int(len(scored) * 0.20) - 1)
        p20 = scored[idx]
        for a in multi:
            if a.adrift:
                continue
            # bottom fifth AND well below the run's median (< half the typical topic)
            if a.kas <= p20 and a.kas < 0.5 * median:
                keys.add(a.key)
    return sorted(keys)


# ── Coverage ─────────────────────────────────────────────────────────────────

def coverage(clusters, *, importance_floor: float = 0.7) -> "tuple[float, int, int]":
    """Fraction of HIGH-IMPORTANCE memories that landed in a real (non-orphan)
    topic vs. fell to orphans. Answers 'is the wiki representative of what I know,
    or is my best knowledge stranded?' Returns (fraction, covered, orphaned)."""
    covered = 0
    orphaned = 0
    for c in clusters:
        for m in c.members:
            if (m.importance or 0.0) < importance_floor:
                continue
            if getattr(c, "is_orphan", False):
                orphaned += 1
            else:
                covered += 1
    total = covered + orphaned
    frac = 1.0 if total == 0 else covered / total
    return round(frac, 4), covered, orphaned


# ── Staleness ────────────────────────────────────────────────────────────────

def stale_topics(clusters, *, threshold: float = 0.5) -> list:
    """Topics whose SOURCE members are mostly superseded/aged (valid_to set).
    A page compiled from stale sources confidently presents outdated knowledge —
    the 'looks clean even when wrong' failure. Returns (key, stale_fraction) for
    clusters above `threshold`, worst first."""
    out = []
    for c in clusters:
        if getattr(c, "is_orphan", False) or not c.members:
            continue
        stale = sum(1 for m in c.members if m.valid_to)
        frac = stale / len(c.members)
        if frac >= threshold:
            out.append((c.key, round(frac, 4)))
    out.sort(key=lambda t: -t[1])
    return out


# ── Redundancy ───────────────────────────────────────────────────────────────

def redundant_pairs(clusters, *, overlap_threshold: float = 0.6) -> list:
    """Cluster PAIRS with high member overlap (Jaccard) — the clustering split
    what should be one topic, or two topics genuinely share a core. A
    between-cluster signal KAS (within-cluster) can't see. Returns
    (key_a, key_b, overlap) above threshold, most-overlapping first."""
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
            union = len(sa | sb)
            jac = inter / union
            if jac >= overlap_threshold:
                a, b = sorted((ka, kb))
                out.append((a, b, round(jac, 4)))
    out.sort(key=lambda t: -t[2])
    return out


# ── the full report ──────────────────────────────────────────────────────────

def annotate_drift_entities(anchors, clusters, comention_entities) -> None:
    """For each ADRIFT cluster, record which co-mention entity names still bridge
    its members — so the report points at the next entity filter to add, not just
    'this looks loose'. `comention_entities` maps entity_name -> set(member_ids);
    supplied by the renderer (which has the entity data). Optional: skipped when
    None, keeping build_health pure and query-free."""
    if not comention_entities:
        return
    by_key = {c.key: {m.id for m in c.members} for c in clusters}
    for a in anchors:
        if not a.adrift:
            continue
        ids = by_key.get(a.key, set())
        culprits = sorted(
            name for name, members in comention_entities.items()
            if len(members & ids) >= 2)
        a.drift_entities = culprits[:8]


def build_health(clusters, edges, kas: "Optional[KASWeights] | dict" = None,
                 comention_entities=None) -> WikiHealth:
    """Assemble the Knowledge Anchor Report. Pure; deterministic; no queries.

    `kas` is a KASWeights (the tunable factors); its default is the published m3
    default. For convenience a bare edge-weights dict is also accepted and wrapped
    (so existing callers passing EDGE_WEIGHTS keep working). `comention_entities`
    (optional) enriches adrift clusters with the entity names still bridging them —
    the renderer supplies it; None keeps this query-free."""
    if isinstance(kas, dict):
        kas = KASWeights(edge_weights=kas)
    kw = kas or _DEFAULT_KAS
    anchors = [cluster_anchor(c, edges, kw)
               for c in clusters if not getattr(c, "is_orphan", False)]
    annotate_drift_entities(anchors, clusters, comention_entities)
    frac, covered, orphaned = coverage(clusters)
    return WikiHealth(
        anchors=anchors,
        low_anchor=_flag_low_anchor(anchors),
        coverage=frac,
        covered=covered,
        orphaned_high_value=orphaned,
        stale=stale_topics(clusters),
        redundant_pairs=redundant_pairs(clusters),
    )
