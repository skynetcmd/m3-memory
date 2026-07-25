"""Commit 4b — LIVE validation of the citation-drift judge PROMPT.

The skeleton's plumbing is drift-tested deterministically
(test_wiki_citation_drift_skeleton.py). This module validates the OTHER two
deferrals: the real model-backed prompt and its recall/precision THRESHOLD,
measured against the committed labelled fixture (tests/fixtures/citation_drift/).

requires_llm: auto-skips when no local chat model is reachable (the judge is
model-backed and non-deterministic — never on the hermetic lane). When it runs,
it asserts the judge catches real drift (recall) without crying wolf (precision)
at the floors recorded in wiki.citation_drift, so a prompt regression fails CI
wherever a model is available.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

from wiki.citation_drift import (  # noqa: E402
    DRIFT_PRECISION_FLOOR,
    DRIFT_RECALL_FLOOR,
    DriftConfig,
    ModelDriftJudge,
)

# NOTE: only the live judge test carries requires_llm — the fixture-shape and
# fail-open tests are hermetic and must run on every lane, so the marker is on the
# test, not the module.
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "citation_drift" / "pairs.json"


def _load_cases():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert cases, "fixture has no cases"
    return cases


def test_fixture_is_balanced_and_well_formed():
    """A recall/precision floor is only meaningful on a balanced, well-formed set
    — this runs WITHOUT a model (no requires_llm needed for the shape check)."""
    cases = _load_cases()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    drifted = [c for c in cases if c["drifted"] is True]
    faithful = [c for c in cases if c["drifted"] is False]
    assert len(drifted) >= 4 and len(faithful) >= 4, "need both classes represented"
    for c in cases:
        assert isinstance(c["drifted"], bool)
        assert c["synthesis"].strip()
        assert c["sources"] and all(s.strip() for s in c["sources"])


@pytest.fixture()
def judge(tmp_path):
    cfg = DriftConfig.from_env(cache_dir=str(tmp_path / "drift_cache"))
    return ModelDriftJudge(cfg)


@pytest.mark.requires_llm
def test_judge_meets_recall_and_precision_floor(judge):
    """Run the real judge over every labelled pair; assert it meets the recorded
    recall + precision floors. Abstentions (model confusion) count as neither a
    true nor false positive — fail-open, so they only reduce coverage, never
    manufacture a finding."""
    cases = _load_cases()
    tp = fp = fn = tn = abstain = 0
    misses: list[str] = []
    for c in cases:
        v = judge.judge(c["synthesis"], c["sources"])
        truth = c["drifted"]
        if v.drifted is None:
            abstain += 1
            continue
        if truth and v.drifted:
            tp += 1
        elif truth and not v.drifted:
            fn += 1
            misses.append(f"MISS drift {c['id']} ({c['kind']})")
        elif (not truth) and v.drifted:
            fp += 1
            misses.append(f"FALSE alarm {c['id']}: {v.reason}")
        else:
            tn += 1

    judged = tp + fp + fn + tn
    assert judged >= max(6, len(cases) // 2), (
        f"too many abstentions to validate ({abstain}/{len(cases)}); "
        f"model may be misconfigured. {judge.summary()}")
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    detail = (f"recall={recall:.2f} precision={precision:.2f} "
              f"(tp={tp} fp={fp} fn={fn} tn={tn} abstain={abstain}) | "
              + "; ".join(misses))
    assert recall >= DRIFT_RECALL_FLOOR, f"recall below floor: {detail}"
    assert precision >= DRIFT_PRECISION_FLOOR, f"precision below floor: {detail}"


def test_judge_fails_open_when_model_unreachable(tmp_path):
    """Point the judge at a dead endpoint: it must ABSTAIN (drifted=None), never
    raise and never report a drift. This runs even with a live model (it uses its
    own bad URL), but keep it here so the fail-open contract lives beside the judge."""
    cfg = DriftConfig(url="http://127.0.0.1:9/invalid", timeout_s=1.0,
                      cache_dir=str(tmp_path / "c"))
    j = ModelDriftJudge(cfg)
    v = j.judge("a claim", ["a source"])
    assert v.drifted is None, "unreachable model must abstain, not manufacture a drift"
