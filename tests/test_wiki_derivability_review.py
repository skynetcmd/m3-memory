"""GDPR derivability review queue (wiki.derivability_review).

The follow-on to wiki.erasure: turns restricted syntheses into an actionable
queue with a deterministic derivability signal (surviving provenance sources) and
a 30-day review SLA. Deterministic + report-only; the optional LLM judge is
exercised with a stub (no model on this path, mirrors citation_drift).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from wiki import derivability_review as DR  # noqa: E402
from wiki.cluster import Cluster  # noqa: E402
from wiki.select import Edge, Mem  # noqa: E402

_DERIV_FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "citation_drift"
                  / "derivability_pairs.json")

_NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


def _erased_at(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _syn(id, *, restricted=True, erased_days_ago=1.0, erasures=1, content="the claim"):
    review = {}
    if restricted:
        review = {
            "restricted": True,
            "erasure_records": [
                {"erased_members": 1, "erased_at": _erased_at(erased_days_ago),
                 "reason": "gdpr_forget (Art. 17)"}
                for _ in range(erasures)
            ],
        }
    meta = {"restricted": True, "review": review} if restricted else {}
    return Mem(id=id, type="synthesis", title=id, content=content, importance=0.5,
               confidence=0.5, valid_from=None, valid_to=None, pinned=0,
               created_at=None, updated_at=None, metadata=meta)


def _src(id, content="a surviving source"):
    return Mem(id=id, type="belief", title=id, content=content, importance=0.5,
               confidence=0.5, valid_from=None, valid_to=None, pinned=0,
               created_at=None, updated_at=None, metadata={})


class _StubJudge:
    def __init__(self, derivable=True, raises=False, abstain=False):
        self._d = None if abstain else derivable
        self._raises = raises
        self.calls = []

    def judge(self, synthesis_content, surviving_source_contents):
        self.calls.append((synthesis_content, tuple(surviving_source_contents)))
        if self._raises:
            raise RuntimeError("judge boom")
        return DR.DerivabilityVerdict(synthesis_id="?", derivable=self._d, reason="stub")


# ── deterministic queue ──────────────────────────────────────────────────────

def test_restricted_with_surviving_source_is_unrestrict_candidate():
    syn, src = _syn("s"), _src("x")
    clusters = [Cluster(key="s", members=[syn, src])]
    edges = [Edge("s", "x", "consolidates")]
    r = DR.build_queue(clusters, edges, now=_NOW)
    assert len(r.items) == 1
    it = r.items[0]
    assert it.surviving_sources == 1
    assert it.derivable_candidate is True
    assert r.unrestrict_candidates == [it]
    assert r.orphaned == []


def test_restricted_with_no_surviving_source_is_orphaned():
    # the only consolidates edge pointed at an erased member whose row is gone,
    # so there is no surviving-source edge left in the graph.
    syn = _syn("s")
    clusters = [Cluster(key="s", members=[syn])]
    r = DR.build_queue(clusters, [], now=_NOW)
    it = r.items[0]
    assert it.surviving_sources == 0
    assert it.derivable_candidate is False
    assert r.orphaned == [it]
    assert r.unrestrict_candidates == []


def test_edge_to_a_since_erased_member_does_not_count_as_surviving():
    # an edge whose to_id is NOT a live member (erased) must not count.
    syn, src = _syn("s"), _src("live")
    clusters = [Cluster(key="s", members=[syn, src])]
    edges = [Edge("s", "erased-ghost", "consolidates"),  # target row gone
             Edge("s", "live", "distills_from")]         # target survives
    r = DR.build_queue(clusters, edges, now=_NOW)
    assert r.items[0].surviving_sources == 1


def test_thirty_day_sla_overdue_flag():
    fresh = _syn("fresh", erased_days_ago=5)
    stale = _syn("stale", erased_days_ago=45)
    clusters = [Cluster(key="c", members=[fresh, stale, _src("x")])]
    edges = [Edge("fresh", "x", "consolidates"), Edge("stale", "x", "consolidates")]
    r = DR.build_queue(clusters, edges, now=_NOW)
    by_id = {i.synthesis_id: i for i in r.items}
    assert by_id["fresh"].overdue is False
    assert by_id["stale"].overdue is True
    assert [i.synthesis_id for i in r.overdue] == ["stale"]


def test_non_restricted_synthesis_is_not_queued():
    ok = _syn("ok", restricted=False)
    clusters = [Cluster(key="ok", members=[ok])]
    r = DR.build_queue(clusters, [], now=_NOW)
    assert r.items == []


def test_supersede_edge_is_not_a_provenance_source():
    syn, src = _syn("s"), _src("x")
    clusters = [Cluster(key="s", members=[syn, src])]
    edges = [Edge("s", "x", "supersedes")]  # not consolidates/distills_from
    r = DR.build_queue(clusters, edges, now=_NOW)
    assert r.items[0].surviving_sources == 0  # supersede doesn't derive


def test_unparseable_erased_at_gives_unknown_age_not_overdue():
    syn = _syn("s")
    syn.metadata["review"]["erasure_records"][0]["erased_at"] = "not-a-date"
    clusters = [Cluster(key="s", members=[syn, _src("x")])]
    edges = [Edge("s", "x", "consolidates")]
    r = DR.build_queue(clusters, edges, now=_NOW)
    assert r.items[0].age_days is None
    assert r.items[0].overdue is False  # unknown age is never auto-overdue


def test_queue_never_mutates_metadata():
    syn = _syn("s")
    before = dict(syn.metadata)
    clusters = [Cluster(key="s", members=[syn, _src("x")])]
    DR.build_queue(clusters, [Edge("s", "x", "consolidates")], now=_NOW)
    assert syn.metadata == before, "build_queue must be read-only"


# ── optional judge ───────────────────────────────────────────────────────────

def test_judge_sees_only_surviving_sources():
    syn, src = _syn("s", content="claim"), _src("x", "surviving text")
    clusters = [Cluster(key="s", members=[syn, src])]
    j = _StubJudge(derivable=True)
    r = DR.build_queue(clusters, [Edge("s", "x", "consolidates")], judge=j, now=_NOW)
    assert j.calls == [("claim", ("surviving text",))]
    assert r.items[0].judged_derivable is True
    assert r.judged == 1


def test_judge_not_run_on_orphaned_synthesis():
    """Zero surviving sources → nothing for a judge to derive from; it isn't called."""
    syn = _syn("s")
    clusters = [Cluster(key="s", members=[syn])]
    j = _StubJudge()
    r = DR.build_queue(clusters, [], judge=j, now=_NOW)
    assert j.calls == []
    assert r.judged == 0


def test_judge_says_not_derivable_removes_from_unrestrict_candidates():
    syn, src = _syn("s"), _src("x")
    clusters = [Cluster(key="s", members=[syn, src])]
    j = _StubJudge(derivable=False)
    r = DR.build_queue(clusters, [Edge("s", "x", "consolidates")], judge=j, now=_NOW)
    it = r.items[0]
    assert it.derivable_candidate is True          # deterministic floor still true
    assert it.judged_derivable is False            # but judge overrode
    assert r.unrestrict_candidates == []           # so not an unrestrict candidate


def test_judge_error_is_fail_open():
    syn, src = _syn("s"), _src("x")
    clusters = [Cluster(key="s", members=[syn, src])]
    r = DR.build_queue(clusters, [Edge("s", "x", "consolidates")],
                       judge=_StubJudge(raises=True), now=_NOW)
    assert r.abstained == 1
    assert r.items[0].judged_derivable is None
    # falls back to the deterministic floor
    assert r.unrestrict_candidates == [r.items[0]]


def test_judge_abstain_falls_back_to_floor():
    syn, src = _syn("s"), _src("x")
    clusters = [Cluster(key="s", members=[syn, src])]
    r = DR.build_queue(clusters, [Edge("s", "x", "consolidates")],
                       judge=_StubJudge(abstain=True), now=_NOW)
    assert r.abstained == 1
    assert r.items[0].judged_derivable is None
    assert r.unrestrict_candidates == [r.items[0]]


# ── render section ───────────────────────────────────────────────────────────

def test_render_none_report_is_empty():
    assert DR.render_section(None) == []


def test_render_empty_queue_states_nothing_pending():
    text = "\n".join(DR.render_section(DR.ReviewReport()))
    assert "GDPR derivability review (0)" in text
    assert "nothing awaiting" in text


def test_render_lists_overdue_first_and_states():
    fresh = _syn("fresh", erased_days_ago=2)
    stale = _syn("stale", erased_days_ago=60)
    orphan = _syn("orphan", erased_days_ago=3)
    clusters = [Cluster(key="c", members=[fresh, stale, orphan, _src("x")])]
    edges = [Edge("fresh", "x", "consolidates"), Edge("stale", "x", "consolidates")]
    r = DR.build_queue(clusters, edges, now=_NOW)
    lines = DR.render_section(r)
    text = "\n".join(lines)
    assert "GDPR derivability review (3)" in text
    assert "OVERDUE" in text and "orphaned" in text
    assert "Report-only" in text
    # overdue 'stale' appears before the non-overdue items in the list
    body = [ln for ln in lines if ln.startswith("- ")]
    assert "stale"[:8] in body[0], "overdue item should sort first"


# ── optional model-backed judge: hermetic + live ─────────────────────────────

def _load_deriv_cases():
    data = json.loads(_DERIV_FIXTURE.read_text(encoding="utf-8"))
    return data["cases"], data.get("recall_floor", 0.8), data.get("precision_floor", 0.8)


def test_derivability_fixture_is_balanced_and_well_formed():
    cases, _, _ = _load_deriv_cases()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    yes = [c for c in cases if c["derivable"] is True]
    no = [c for c in cases if c["derivable"] is False]
    assert len(yes) >= 3 and len(no) >= 3
    for c in cases:
        assert isinstance(c["derivable"], bool)
        assert c["synthesis"].strip()
        assert c["surviving"] and all(s.strip() for s in c["surviving"])


def test_model_judge_fails_open_when_model_unreachable(tmp_path):
    """A dead endpoint → abstain (derivable=None), never raise, never force a
    decision on privacy-erased content. Runs on the hermetic lane (own bad URL)."""
    from wiki.citation_drift import DriftConfig
    cfg = DriftConfig(url="http://127.0.0.1:9/invalid", timeout_s=1.0,
                      cache_dir=str(tmp_path / "c"))
    j = DR.ModelDerivabilityJudge(cfg)
    v = j.judge("a claim", ["a surviving source"])
    assert v.derivable is None


@pytest.mark.requires_llm
def test_model_judge_meets_floor_on_fixture(tmp_path):
    """Live: the derivability judge over labelled pairs. 'derivable' is the
    POSITIVE class. This gate is ASYMMETRIC: precision on 'derivable' is the
    SAFETY guard (a false positive = wrongly suggesting we unrestrict content that
    leaned on erased data), so its floor is HIGH; recall is only a convenience (a
    missed-derivable page just stays restricted for a human to review, and the
    deterministic surviving-source floor already flagged it), so its floor is
    LOOSE. Measured 2026-07-25 (local model): precision 1.00, recall 0.67 — the
    single miss was a defensible conservative read of a generalizing paraphrase."""
    from wiki.citation_drift import DriftConfig
    cases, recall_floor, precision_floor = _load_deriv_cases()
    j = DR.ModelDerivabilityJudge(DriftConfig.from_env(cache_dir=str(tmp_path)))
    tp = fp = fn = tn = ab = 0
    for c in cases:
        v = j.judge(c["synthesis"], c["surviving"])
        if v.derivable is None:
            ab += 1
            continue
        truth = c["derivable"]
        if truth and v.derivable:
            tp += 1
        elif truth and not v.derivable:
            fn += 1
        elif (not truth) and v.derivable:
            fp += 1
        else:
            tn += 1
    judged = tp + fp + fn + tn
    assert judged >= max(4, len(cases) // 2), f"too many abstentions ({ab}); {j.summary()}"
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    detail = f"recall={recall:.2f} precision={precision:.2f} (tp={tp} fp={fp} fn={fn} tn={tn} ab={ab})"
    assert recall >= recall_floor, f"recall below floor: {detail}"
    assert precision >= precision_floor, f"precision below floor: {detail}"
