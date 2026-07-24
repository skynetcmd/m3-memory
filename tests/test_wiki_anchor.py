"""Knowledge Anchor Report — a linear scoring framework (kernel + 4 metrics).

Every metric is score = Σ w_i·f_i over normalized [0,1] factors. Tests cover the
kernel, KAS's four factors and their weighting, the self-calibrating flag, and the
coverage / staleness / redundancy metrics on the same kernel.
"""
import os
import sys

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from wiki import anchor as A  # noqa: E402
from wiki.cluster import Cluster  # noqa: E402
from wiki.select import EDGE_WEIGHTS, Edge, Mem  # noqa: E402


def _mem(id, conf=0.8, imp=0.9, vto=None):
    return Mem(id=id, type="note", title=id, content=id, importance=imp,
               confidence=conf, valid_from=None, valid_to=vto, pinned=0,
               created_at=None, updated_at=None)


def _cluster(*mems, orphan=False):
    ms = list(mems)
    c = Cluster(key=min(m.id for m in ms), members=ms)
    c.is_orphan = orphan
    return c


# ── the kernel ───────────────────────────────────────────────────────────────

def test_weighted_score_is_normalized_0_1():
    assert A.weighted_score({"a": 1.0, "b": 0.0}, {"a": 1, "b": 1}) == 0.5
    assert A.weighted_score({"a": 1.0}, {"a": 5}) == 1.0
    assert A.weighted_score({"a": 0.0}, {"a": 5}) == 0.0


def test_weighted_score_clamps_factors_and_ignores_zero_weight():
    assert A.weighted_score({"a": 2.0}, {"a": 1}) == 1.0     # clamp >1
    assert A.weighted_score({"a": -1.0}, {"a": 1}) == 0.0    # clamp <0
    # a zero-weight factor doesn't dilute the score
    assert A.weighted_score({"a": 1.0, "b": 0.0}, {"a": 1, "b": 0}) == 1.0


def test_weighted_score_empty_is_zero():
    assert A.weighted_score({}, {}) == 0.0
    assert A.weighted_score({"a": 1.0}, {}) == 0.0


def test_saturate_half_point():
    assert abs(A._saturate(2.0, 2.0) - 0.5) < 1e-9   # x==half → 0.5
    assert A._saturate(0.0, 2.0) == 0.0


# ── KAS factors ──────────────────────────────────────────────────────────────

def test_co_mention_only_cluster_is_adrift_and_low():
    """Pure co-mention: no load-bearing edges → adrift, backbone_ratio 0,
    provenance 0, backbone 0 — a low KAS driven by the factors, not a multiply."""
    c = _cluster(_mem("a"), _mem("b"), _mem("c"))
    edges = [Edge("a", "b", "co_mentions"), Edge("b", "c", "co_mentions")]
    an = A.cluster_anchor(c, edges)
    assert an.adrift is True
    assert an.factors["backbone_ratio"] == 0.0
    assert an.factors["provenance"] == 0.0
    assert an.load_bearing_edges == 0 and an.comention_edges == 2


def test_provenance_factor_rewards_consolidates():
    """A consolidates-backed cluster scores higher provenance than a merely
    'related' one — the factor that most discriminates on the real corpus."""
    prov = A.cluster_anchor(_cluster(_mem("a"), _mem("b")),
                            [Edge("a", "b", "consolidates")])
    rel = A.cluster_anchor(_cluster(_mem("a"), _mem("b")),
                           [Edge("a", "b", "related")])
    assert prov.factors["provenance"] == 1.0
    assert rel.factors["provenance"] == 0.0
    assert prov.kas > rel.kas


def test_backbone_ratio_penalizes_comention_dilution():
    """Same real edge, but one cluster is also fused by co-mention → lower
    backbone_ratio → lower KAS."""
    clean = A.cluster_anchor(_cluster(_mem("a"), _mem("b")),
                             [Edge("a", "b", "consolidates")])
    diluted = A.cluster_anchor(
        _cluster(_mem("a"), _mem("b"), _mem("c")),
        [Edge("a", "b", "consolidates"),
         Edge("a", "c", "co_mentions"), Edge("b", "c", "co_mentions")])
    assert clean.factors["backbone_ratio"] > diluted.factors["backbone_ratio"]


def test_kas_is_in_unit_range():
    an = A.cluster_anchor(_cluster(_mem("a"), _mem("b")),
                          [Edge("a", "b", "consolidates")])
    assert 0.0 <= an.kas <= 1.0


def test_kas_factors_only_internal_edges():
    c = _cluster(_mem("a"), _mem("b"))
    an = A.cluster_anchor(c, [Edge("a", "outside", "consolidates")])
    assert an.load_bearing_edges == 0 and an.adrift is True


# ── KAS weights are tunable ──────────────────────────────────────────────────

def test_zero_weight_factor_drops_out():
    """Setting a factor's weight to 0 removes it from the score entirely."""
    c = _cluster(_mem("a"), _mem("b"))
    e = [Edge("a", "b", "consolidates")]
    full = A.cluster_anchor(c, e, A.KASWeights())
    no_conf = A.cluster_anchor(c, e, A.KASWeights(confidence=0.0))
    # dropping the (low) confidence factor changes the score
    assert full.kas != no_conf.kas


def test_from_tuple_sets_the_four_weights():
    kw = A.KASWeights.from_tuple((0.1, 0.5, 0.1, 0.3))
    assert (kw.backbone, kw.provenance, kw.confidence, kw.backbone_ratio) == (0.1, 0.5, 0.1, 0.3)


def test_default_weights_favor_discriminating_factors():
    """Calibration invariant: provenance + backbone_ratio carry the majority,
    reflecting their measured discriminating power on the real corpus."""
    kw = A.KASWeights()
    discriminating = kw.provenance + kw.backbone_ratio
    weak = kw.backbone + kw.confidence
    assert discriminating > weak


# ── self-calibrating flag ────────────────────────────────────────────────────

def test_adrift_always_flagged_strong_never():
    anchors = [
        A.cluster_anchor(_cluster(_mem("a"), _mem("b")), [Edge("a", "b", "co_mentions")]),
        A.cluster_anchor(_cluster(_mem("c"), _mem("d")), [Edge("c", "d", "consolidates")]),
    ]
    flagged = A._flag_low_anchor(anchors)
    assert anchors[0].key in flagged and anchors[1].key not in flagged


def test_percentile_flags_weak_in_a_real_distribution():
    def clu(i, rel):
        a, b = f"{i}a", f"{i}b"
        return A.cluster_anchor(_cluster(_mem(a), _mem(b)), [Edge(a, b, rel)])
    anchors = [clu(i, "consolidates") for i in range(5)] + [clu(9, "follows")]
    flagged = A._flag_low_anchor(anchors)
    assert anchors[-1].key in flagged
    assert all(a.key not in flagged for a in anchors[:5])


# ── coverage / staleness / redundancy (same kernel) ──────────────────────────

def test_coverage_counts_stranded_high_value():
    clusters = [_cluster(_mem("a"), _mem("b")),
                _cluster(_mem("c"), orphan=True),
                _cluster(_mem("d", imp=0.1))]
    frac, covered, orphaned = A.coverage_score(clusters)
    assert covered == 2 and orphaned == 1 and abs(frac - 2 / 3) < 1e-3


def test_staleness_flags_aged_topic():
    fresh = _cluster(_mem("a"), _mem("b"))
    stale = _cluster(_mem("c", vto="2026-01-01"), _mem("d", vto="2026-01-02"))
    keys = {k for k, _ in A.stale_topics([fresh, stale])}
    assert stale.key in keys and fresh.key not in keys


def test_redundancy_detects_overlap():
    a = _cluster(_mem("1"), _mem("2"), _mem("3"))
    c = _cluster(_mem("1"), _mem("2"), _mem("3"), _mem("5"))  # vs a: 3/4=0.75
    pairs = A.redundant_pairs([a, c])
    assert (min(a.key, c.key), max(a.key, c.key)) in {(x, y) for x, y, _ in pairs}


# ── drift diagnostic + full report ───────────────────────────────────────────

def test_drift_entities_named():
    c = _cluster(_mem("a"), _mem("b"))
    anchors = [A.cluster_anchor(c, [Edge("a", "b", "co_mentions")])]
    A.annotate_drift_entities(anchors, [c], {"pyproject.toml": {"a", "b"}})
    assert anchors[0].drift_entities == ["pyproject.toml"]


def test_build_health_complete():
    clusters = [_cluster(_mem("a"), _mem("b")), _cluster(_mem("x"), _mem("y"))]
    edges = [Edge("a", "b", "consolidates"), Edge("x", "y", "co_mentions")]
    h = A.build_health(clusters, edges)
    assert len(h.anchors) == 2
    assert clusters[1].key in h.low_anchor and clusters[0].key not in h.low_anchor
    assert 0.0 <= h.coverage <= 1.0
