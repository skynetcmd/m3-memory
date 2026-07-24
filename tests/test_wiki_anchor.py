"""Knowledge Anchor Report — KAS + coverage / staleness / redundancy.

KAS's whole point: a cluster held together only by co-mention scores ~0 (adrift),
while one held by real load-bearing edges scores well — the OPPOSITE of what
plain link-density would say, and the over-merge signature this project fixed.
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


# ── KAS: backbone vs co-mention (the headline) ───────────────────────────────

def test_co_mention_only_cluster_is_adrift():
    """A cluster held together ONLY by co-mention has zero backbone → adrift,
    KAS ~0 — even though it has plenty of edges. This is the over-merge signal
    plain density would MISS (it would score high on edge count)."""
    c = _cluster(_mem("a"), _mem("b"), _mem("c"))
    edges = [Edge("a", "b", "co_mentions"), Edge("b", "c", "co_mentions")]
    an = A.cluster_anchor(c, edges, EDGE_WEIGHTS)
    assert an.adrift is True
    assert an.kas == 0.0
    assert an.load_bearing_edges == 0
    assert an.comention_edges == 2


def test_real_backbone_scores_high():
    c = _cluster(_mem("a"), _mem("b"))
    edges = [Edge("a", "b", "consolidates")]  # weight 3.0, conf 0.8
    an = A.cluster_anchor(c, edges, EDGE_WEIGHTS)
    assert an.adrift is False
    assert an.kas > 0
    # 3.0 * 0.8 / 2 members = 1.2
    assert abs(an.kas - 1.2) < 0.001


def test_confidence_weights_the_backbone():
    """Same edge, lower-confidence endpoints → weaker anchor."""
    hi = A.cluster_anchor(_cluster(_mem("a", 0.9), _mem("b", 0.9)),
                          [Edge("a", "b", "related")], EDGE_WEIGHTS)
    lo = A.cluster_anchor(_cluster(_mem("a", 0.3), _mem("b", 0.3)),
                          [Edge("a", "b", "related")], EDGE_WEIGHTS)
    assert hi.kas > lo.kas


def test_only_internal_edges_count():
    """An edge to a member outside the cluster does not contribute."""
    c = _cluster(_mem("a"), _mem("b"))
    edges = [Edge("a", "outside", "consolidates")]  # b<-a? no: a->outside
    an = A.cluster_anchor(c, edges, EDGE_WEIGHTS)
    assert an.load_bearing_edges == 0
    assert an.adrift is True


def test_adrift_always_flagged_strong_never():
    anchors = [
        A.cluster_anchor(_cluster(_mem("a"), _mem("b")),
                         [Edge("a", "b", "co_mentions")], EDGE_WEIGHTS),   # adrift
        A.cluster_anchor(_cluster(_mem("c"), _mem("d")),
                         [Edge("c", "d", "consolidates")], EDGE_WEIGHTS),  # strong
    ]
    flagged = A._flag_low_anchor(anchors)
    assert anchors[0].key in flagged      # adrift always flagged
    assert anchors[1].key not in flagged  # a strong cluster is never "bottom slice"


def test_percentile_flags_the_genuinely_weak_in_a_real_distribution():
    """With a real distribution (≥5), a cluster far below the median KAS is
    flagged; strong ones are not — even though all have real backbones."""
    def clu(i, rel):
        a, b = f"{i}a", f"{i}b"
        c = _cluster(_mem(a), _mem(b))
        return A.cluster_anchor(c, [Edge(a, b, rel)], EDGE_WEIGHTS)
    # five strong (consolidates=3.0) + one weak (follows=0.5)
    anchors = [clu(i, "consolidates") for i in range(5)] + [clu(9, "follows")]
    flagged = A._flag_low_anchor(anchors)
    assert anchors[-1].key in flagged            # the weak one
    assert all(a.key not in flagged for a in anchors[:5])  # the strong ones


# ── KAS is tunable (the config knobs) ────────────────────────────────────────

def test_default_kas_matches_explicit_default_weights():
    """Passing nothing == passing the default KASWeights == passing EDGE_WEIGHTS."""
    c = _cluster(_mem("a"), _mem("b"))
    e = [Edge("a", "b", "consolidates")]
    assert A.cluster_anchor(c, e).kas == A.cluster_anchor(c, e, A.KASWeights()).kas
    assert A.cluster_anchor(c, e).kas == A.cluster_anchor(c, e, EDGE_WEIGHTS).kas


def test_confidence_influence_zero_ignores_confidence():
    """With confidence_influence=0, low- and high-confidence clusters score the
    same — the confidence factor is switched off."""
    kw = A.KASWeights(confidence_influence=0.0)
    hi = A.cluster_anchor(_cluster(_mem("a", 0.9), _mem("b", 0.9)),
                          [Edge("a", "b", "related")], kw)
    lo = A.cluster_anchor(_cluster(_mem("a", 0.2), _mem("b", 0.2)),
                          [Edge("a", "b", "related")], kw)
    assert hi.kas == lo.kas


def test_comention_credit_gives_partial_anchor():
    """comention_credit>0 lets a co-mention-only cluster score above zero (still
    'adrift' by the load-bearing test, but no longer KAS 0)."""
    c = _cluster(_mem("a"), _mem("b"))
    e = [Edge("a", "b", "co_mentions")]
    assert A.cluster_anchor(c, e, A.KASWeights()).kas == 0.0
    credited = A.cluster_anchor(c, e, A.KASWeights(comention_credit=0.5))
    assert credited.kas > 0.0
    assert credited.adrift is True  # load-bearing count is still 0


def test_custom_edge_weights_re_rank():
    """A caller can make 'related' count as much as 'consolidates'."""
    c = _cluster(_mem("a"), _mem("b"))
    boosted = A.KASWeights(edge_weights={"related": 3.0})
    assert (A.cluster_anchor(c, [Edge("a", "b", "related")], boosted).kas
            == A.cluster_anchor(c, [Edge("a", "b", "consolidates")]).kas)


def test_normalize_by_possible_edges():
    kw = A.KASWeights(normalize_by="possible_edges")
    # 3 members, 1 consolidates edge (weight 3.0, conf 0.8): 3*0.8 / (3*2/2=3) = 0.8
    an = A.cluster_anchor(_cluster(_mem("a"), _mem("b"), _mem("c")),
                          [Edge("a", "b", "consolidates")], kw)
    assert abs(an.kas - 0.8) < 0.001


def test_from_tuple_sugar():
    kw = A.KASWeights.from_tuple((0.0, 0.5))
    assert kw.confidence_influence == 0.0 and kw.comention_credit == 0.5


# ── coverage ─────────────────────────────────────────────────────────────────

def test_coverage_counts_stranded_high_value():
    clusters = [
        _cluster(_mem("a", imp=0.9), _mem("b", imp=0.9)),            # covered
        _cluster(_mem("c", imp=0.9), orphan=True),                   # stranded
        _cluster(_mem("d", imp=0.1)),                                # low-value, ignored
    ]
    frac, covered, orphaned = A.coverage(clusters)
    assert covered == 2 and orphaned == 1
    assert abs(frac - 2 / 3) < 0.001


# ── staleness ────────────────────────────────────────────────────────────────

def test_stale_topic_flagged():
    fresh = _cluster(_mem("a"), _mem("b"))
    stale = _cluster(_mem("c", vto="2026-01-01"), _mem("d", vto="2026-01-02"))
    out = A.stale_topics([fresh, stale])
    keys = {k for k, _ in out}
    assert stale.key in keys and fresh.key not in keys


# ── redundancy ───────────────────────────────────────────────────────────────

def test_redundant_pair_detected():
    a = _cluster(_mem("1"), _mem("2"), _mem("3"))
    b = _cluster(_mem("1"), _mem("2"), _mem("4"))   # 2/4 shared → 0.5? below 0.6
    c = _cluster(_mem("1"), _mem("2"), _mem("3"), _mem("5"))  # vs a: 3/4 = 0.75
    pairs = A.redundant_pairs([a, b, c])
    keys = {(x, y) for x, y, _ in pairs}
    assert (min(a.key, c.key), max(a.key, c.key)) in keys


# ── drift diagnostic ─────────────────────────────────────────────────────────

def test_drift_entities_named_for_adrift_cluster():
    c = _cluster(_mem("a"), _mem("b"))
    anchors = [A.cluster_anchor(c, [Edge("a", "b", "co_mentions")], EDGE_WEIGHTS)]
    A.annotate_drift_entities(anchors, [c],
                              {"pyproject.toml": {"a", "b"}, "unrelated": {"z"}})
    assert anchors[0].drift_entities == ["pyproject.toml"]


# ── full report ──────────────────────────────────────────────────────────────

def test_build_health_is_pure_and_complete():
    clusters = [
        _cluster(_mem("a"), _mem("b")),
        _cluster(_mem("x"), _mem("y")),
    ]
    edges = [Edge("a", "b", "consolidates"), Edge("x", "y", "co_mentions")]
    h = A.build_health(clusters, edges, EDGE_WEIGHTS)
    assert len(h.anchors) == 2
    # the co-mention-only cluster is adrift → flagged; the consolidates one is not
    assert clusters[1].key in h.low_anchor
    assert clusters[0].key not in h.low_anchor
    assert h.coverage == 1.0  # all high-value memories are in real topics
