import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory_core import _trim_by_elbow


def _ranked(scores):
    return [(s, {"id": f"x{i}", "content": f"c{i}"}) for i, s in enumerate(scores)]


def test_sensitivity_default_preserves_behavior():
    # Clean cliff: diffs [0.02, 0.02, 0.47, 0.01]. avg ≈ 0.13.
    # sensitivity=1.5 threshold ≈ 0.195. First diff > 0.195 is the 0.47 at i=2 → trim[:3].
    ranked = _ranked([1.00, 0.98, 0.96, 0.49, 0.48])
    assert len(_trim_by_elbow(ranked)) == 3
    assert len(_trim_by_elbow(ranked, sensitivity=1.5)) == 3


def test_looser_sensitivity_keeps_more_or_trims_earlier():
    # Gentler slope: diffs [0.05, 0.10, 0.10, 0.15], avg=0.10.
    # sensitivity=1.5 → threshold 0.15, no diff exceeds → keep all 5.
    # sensitivity=1.0 → threshold 0.10, 0.15 at i=3 exceeds → trim[:4].
    # sensitivity=0.9 → threshold 0.09, 0.10 at i=1 exceeds → trim[:2].
    ranked = _ranked([1.00, 0.95, 0.85, 0.75, 0.60])
    assert len(_trim_by_elbow(ranked, sensitivity=1.5)) == 5
    assert len(_trim_by_elbow(ranked, sensitivity=1.0)) == 4
    assert len(_trim_by_elbow(ranked, sensitivity=0.9)) == 2


def test_too_few_to_trim():
    assert _trim_by_elbow(_ranked([0.9, 0.8])) == _ranked([0.9, 0.8])
    assert _trim_by_elbow([]) == []


def test_no_elbow_returns_all():
    # Uniform diffs → no diff > avg*1.5.
    ranked = _ranked([1.0, 0.9, 0.8, 0.7, 0.6])
    assert len(_trim_by_elbow(ranked)) == 5
