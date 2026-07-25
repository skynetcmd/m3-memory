"""Cluster admission gate (wiki.admission) + its integration into compile.

Three layers, all model-free / store-free:
  1. the AdmissionGate tuple + admits() decision on synthetic anchors,
  2. config load/override/validation (mirrors the KASWeights + authority pattern),
  3. the gate wired into compile_clusters (_admit) and the CompileStats.admitted
     property the two-pass retry keys on.
"""
import json
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from wiki import admission as A  # noqa: E402
from wiki import compile as WC  # noqa: E402
from wiki.cluster import Cluster  # noqa: E402
from wiki.select import Edge, Mem  # noqa: E402


def _m(id, type="belief"):
    return Mem(id=id, type=type, title=id, content=f"c-{id}", importance=0.5,
               confidence=0.7, valid_from=None, valid_to=None, pinned=0,
               created_at=None, updated_at=None, metadata={})


def _clu(*mems):
    ms = list(mems)
    return Cluster(key=min(m.id for m in ms), members=ms)


def _E(a, b, rel):
    return Edge(a, b, rel)


# ── structural_signals: the state-independent scorer ─────────────────────────

def test_signals_ignore_compile_produced_consolidates():
    """The whole point: a `consolidates` edge (compile-produced) must NOT change
    the gate's view. Same members, same authored edges, +consolidates → identical
    provenance / backbone_ratio / kas."""
    c = _clu(_m("a"), _m("b"), _m("c"))
    authored = [_E("a", "b", "supersedes"), _E("b", "c", "related")]
    s1 = A.structural_signals(c, authored)
    s2 = A.structural_signals(c, authored + [_E("a", "b", "consolidates"),
                                            _E("a", "c", "consolidates")])
    assert (s1.provenance, s1.backbone_ratio, s1.kas) == \
           (s2.provenance, s2.backbone_ratio, s2.kas)


def test_signals_exclude_synthesis_members():
    """A consolidates edge drags the synthesis into the cluster; the gate must
    drop synthesis-typed members so they don't skew the structure."""
    with_syn = _clu(_m("a"), _m("b"), _m("syn", type="synthesis"))
    without = _clu(_m("a"), _m("b"))
    edges = [_E("a", "b", "supersedes")]
    s_with = A.structural_signals(with_syn, edges)
    s_without = A.structural_signals(without, edges)
    assert s_with.provenance == s_without.provenance
    assert s_with.backbone_ratio == s_without.backbone_ratio


def test_signals_comention_only_is_zero_structure():
    c = _clu(_m("a"), _m("b"), _m("c"))
    edges = [_E("a", "b", "co_mentions"), _E("b", "c", "co_mentions")]
    s = A.structural_signals(c, edges)
    assert s.backbone_ratio == 0.0 and s.provenance == 0.0
    assert s.load_bearing == 0 and s.comention == 2


def test_signals_authored_provenance_counted():
    c = _clu(_m("a"), _m("b"))
    s = A.structural_signals(c, [_E("a", "b", "supersedes")])
    assert s.provenance == 1.0 and s.backbone_ratio == 1.0


def test_admission_is_state_independent_across_compiles():
    """THE headline guarantee: the admit/demote decision is identical whether the
    store is cold (no consolidates) or warm (a prior compile wrote consolidates +
    added the synthesis to the cluster). Simulate both states and assert the gate
    decides the same."""
    g = A.AdmissionGate()
    # Cold: authored edges only.
    cold = _clu(_m("a"), _m("b"), _m("c"))
    cold_edges = [_E("a", "b", "supersedes"), _E("b", "c", "extends")]
    # Warm: same members PLUS the synthesis pulled in by consolidates edges.
    warm = _clu(_m("a"), _m("b"), _m("c"), _m("syn", type="synthesis"))
    warm_edges = cold_edges + [_E("syn", "a", "consolidates"),
                               _E("syn", "b", "consolidates"),
                               _E("syn", "c", "consolidates")]
    assert g.admits(cold, cold_edges) == g.admits(warm, warm_edges)
    # And the same for a grab-bag: demoted in both states.
    cold_bb = _clu(_m("x"), _m("y"))
    warm_bb = _clu(_m("x"), _m("y"), _m("s2", type="synthesis"))
    bb_edges = [_E("x", "y", "co_mentions")]
    warm_bb_edges = bb_edges + [_E("s2", "x", "consolidates"),
                                _E("s2", "y", "consolidates")]
    assert g.admits(cold_bb, bb_edges) is False
    assert g.admits(warm_bb, warm_bb_edges) is False


def test_ratio_one_topic_admitted_without_any_provenance():
    """Calibration guard: a well-connected topic linked ONLY by plain `related`
    edges (backbone_ratio 1.0, provenance 0.0, kas just under min_kas) MUST be
    admitted on the ratio path. Requiring provenance too would wrongly demote it —
    this is the exact case that broke the AND-gate on the real cold corpus."""
    c = _clu(_m("a"), _m("b"), _m("c"))
    edges = [_E("a", "b", "related"), _E("b", "c", "related"),
             _E("a", "c", "related")]
    s = A.structural_signals(c, edges)
    assert s.backbone_ratio == 1.0 and s.provenance == 0.0 and s.kas < 0.5
    assert A.AdmissionGate().admits(c, edges) is True  # ratio path carries it


# ── the gate tuple + admits() decision (via real clusters+edges) ─────────────

def test_default_gate_values():
    g = A.AdmissionGate()
    assert (g.min_provenance, g.min_backbone_ratio, g.min_kas) == (0.5, 0.6, 0.5)
    assert g.enabled is True


def test_from_tuple_sets_the_three_floors():
    g = A.AdmissionGate.from_tuple((0.4, 0.55, 0.6))
    assert (g.min_provenance, g.min_backbone_ratio, g.min_kas) == (0.4, 0.55, 0.6)
    assert g.enabled is True  # not positional; keeps default


def test_grab_bag_demoted():
    """Co-mention only → zero structure → demoted."""
    c = _clu(_m("a"), _m("b"), _m("c"))
    edges = [_E("a", "b", "co_mentions"), _E("b", "c", "co_mentions")]
    assert A.AdmissionGate().admits(c, edges) is False


def test_authored_provenance_cluster_admitted():
    """Dense authored lineage clears both floors → admitted, with NO consolidates
    edge anywhere (proving cold-corpus admission works)."""
    c = _clu(_m("a"), _m("b"), _m("c"))
    edges = [_E("a", "b", "supersedes"), _E("b", "c", "extends"),
             _E("a", "c", "refines")]
    assert A.AdmissionGate().admits(c, edges) is True


def test_thin_related_only_demoted():
    """Only weak `related` edges + co-mention: low provenance, diluted ratio →
    demoted (the thin-page case)."""
    c = _clu(_m("a"), _m("b"), _m("c"), _m("d"))
    edges = [_E("a", "b", "related"),
             _E("a", "c", "co_mentions"), _E("b", "d", "co_mentions"),
             _E("c", "d", "co_mentions")]
    assert A.AdmissionGate().admits(c, edges) is False


def test_singleton_always_admitted():
    """Orphan-ness is decided upstream; the gate never demotes a singleton."""
    assert A.AdmissionGate().admits(_clu(_m("a")), []) is True


def test_disabled_gate_admits_everything():
    c = _clu(_m("a"), _m("b"), _m("c"))
    edges = [_E("a", "b", "co_mentions")]
    assert A.AdmissionGate(enabled=False).admits(c, edges) is True


# ── partition ────────────────────────────────────────────────────────────────

def test_partition_splits_admitted_and_demoted():
    good = _clu(_m("a"), _m("b"))
    bad = _clu(_m("x"), _m("y"), _m("z"))
    edges = [_E("a", "b", "supersedes"),
             _E("x", "y", "co_mentions"), _E("y", "z", "co_mentions")]
    admitted, demoted = A.partition([good, bad], edges, A.AdmissionGate())
    assert admitted == [good] and demoted == [bad]


# ── config load / override / validation ──────────────────────────────────────

def _write_cfg(tmp_path, data):
    (tmp_path / A._CONFIG_BASENAME).write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_cache_and_env(monkeypatch):
    A._CACHE.clear()
    A._WARNED.clear()
    yield
    A._CACHE.clear()
    A._WARNED.clear()


def test_load_gate_defaults_when_no_config(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    assert A.load_gate() == A.AdmissionGate()


def test_load_gate_overrides_from_config(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    _write_cfg(tmp_path, {"min_kas": 0.6, "min_provenance": 0.7,
                          "min_backbone_ratio": 0.8})
    g = A.load_gate()
    assert (g.min_kas, g.min_provenance, g.min_backbone_ratio) == (0.6, 0.7, 0.8)
    assert g.enabled is True


def test_load_gate_partial_override_keeps_other_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    _write_cfg(tmp_path, {"min_kas": 0.65})
    g = A.load_gate()
    assert g.min_kas == 0.65
    assert g.min_provenance == 0.5 and g.min_backbone_ratio == 0.6


def test_load_gate_can_disable(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    _write_cfg(tmp_path, {"enabled": False})
    assert A.load_gate().enabled is False


def test_out_of_range_value_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    _write_cfg(tmp_path, {"min_kas": 1.5})  # > 1.0 → invalid
    assert A.load_gate() == A.AdmissionGate()  # bad config never breaks the build


def test_malformed_config_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    (tmp_path / A._CONFIG_BASENAME).write_text("{not json", encoding="utf-8")
    assert A.load_gate() == A.AdmissionGate()


def test_non_bool_enabled_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    _write_cfg(tmp_path, {"enabled": "yes"})
    assert A.load_gate() == A.AdmissionGate()


# ── integration: the gate inside compile_clusters (_admit) ───────────────────


def _mem(id, conf=0.7):
    return Mem(id=id, type="belief", title=id, content=f"c-{id}", importance=0.5,
               confidence=conf, valid_from=None, valid_to=None, pinned=0,
               created_at=None, updated_at=None, metadata={})


def _cluster(*ids):
    ms = [_mem(i) for i in ids]
    return Cluster(key=min(ids), members=ms)


async def _run(clusters, edges=None, gate=None):
    writes = []

    async def _write(**a):
        writes.append(a)
        return "Created: id"

    async def _supersede(**a):
        return "Superseded: id"

    async def _ensure_prompt(comp, stats, *, scope, user_id):
        return "prompt-id"

    class _C:
        prompt_version = "1"
        model = "stub"

        def prompt_text(self):
            return "P"

        def compile(self, cluster):
            return "PROSE"

    stats = await WC.compile_clusters(
        clusters, _C(), heads={}, edges=edges, gate=gate,
        _write=_write, _supersede=_supersede, _ensure_prompt=_ensure_prompt)
    return stats, writes


@pytest.mark.asyncio
async def test_no_edges_means_no_gating():
    """Without an edge graph the gate can't score — every cluster compiles."""
    stats, writes = await _run([_cluster("a", "b"), _cluster("c", "d")], edges=None)
    assert stats.created == 2 and stats.demoted == 0


@pytest.mark.asyncio
async def test_gate_demotes_grab_bag_before_model_call():
    """A pure co-mention cluster is demoted — no write, no model call — while an
    authored-provenance cluster compiles. Note the good cluster uses `supersedes`
    (authored), NOT `consolidates`: the gate ignores compile-produced edges, so a
    consolidates-only cluster would (correctly) be demoted too."""
    good = _cluster("a", "b")
    bad = _cluster("c", "d")
    edges = [Edge("a", "b", "supersedes"),
             Edge("c", "d", "co_mentions")]
    stats, writes = await _run([good, bad], edges=edges)
    assert stats.created == 1 and stats.demoted == 1
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_disabled_gate_compiles_all_even_with_edges():
    bad = _cluster("c", "d")
    edges = [Edge("c", "d", "co_mentions")]
    stats, _ = await _run([bad], edges=edges, gate=A.AdmissionGate(enabled=False))
    assert stats.created == 1 and stats.demoted == 0


# ── CompileStats.admitted / demoted (reporting) ──────────────────────────────

def test_admitted_counts_all_reached_clusters():
    s = WC.CompileStats(created=3, superseded=1, skipped_hash=2,
                        skipped_identical=1, failed=1, demoted=5)
    assert s.admitted == 8  # everything that reached the compiler, not demoted


def test_summary_reports_demotions():
    s = WC.CompileStats(created=29, demoted=9)
    assert "9 demoted" in s.summary()
