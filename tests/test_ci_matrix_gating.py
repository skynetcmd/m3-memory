"""The 3.15 CI lane is non-blocking; the supported lanes still gate.

Python 3.15 is an early-warning probe: `torch` publishes no cp315 wheel, so
`pip install` fails before a test runs. Left blocking, that lane made `main`
permanently red — the §3 false alarm, where a build that always fails is a build
nobody reads and a real regression has nothing to contrast against.

`continue-on-error` keyed on the matrix version fixes that without deleting the
probe. But the expression is a STRING in YAML: a typo (`python_version`, `3.15.0`,
a stray quote) would silently evaluate falsey — leaving 3.15 blocking again — or,
worse, evaluate truthy for every lane and make the entire matrix advisory. Either
way CI would still look configured. These tests pin both directions.
"""
from __future__ import annotations

import os

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml needed to parse the workflow")

_CI = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "ci.yml")

# The versions pyproject.toml classifies as supported. These MUST gate.
_SUPPORTED = ("3.12", "3.13", "3.14")
# The probe. Runs and reports, but must not gate.
_CANARY = "3.15"


def _workflow() -> dict:
    with open(_CI, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _test_job() -> dict:
    return _workflow()["jobs"]["test"]


def test_canary_lane_is_non_blocking():
    coe = str(_test_job().get("continue-on-error", ""))
    assert coe, "the test job has no continue-on-error — the 3.15 lane gates again"
    assert "matrix.python-version" in coe, (
        f"continue-on-error must key on the matrix version, got {coe!r}. A "
        "hardcoded true/false would apply to EVERY lane."
    )
    assert _CANARY in coe, f"continue-on-error does not mention {_CANARY}: {coe!r}"


def test_continue_on_error_is_not_unconditionally_true():
    """The failure mode that still looks configured: every lane advisory."""
    coe = str(_test_job().get("continue-on-error", "")).strip().lower()
    assert coe not in ("true", "${{ true }}"), (
        "continue-on-error is unconditionally true — the whole matrix stopped "
        "gating, so no Python version can fail the build."
    )


def test_supported_versions_are_not_excused():
    """3.12/3.13/3.14 must not appear in the continue-on-error expression."""
    coe = str(_test_job().get("continue-on-error", ""))
    for v in _SUPPORTED:
        assert v not in coe, (
            f"Python {v} appears in continue-on-error, so a version "
            "pyproject.toml CLAIMS would no longer gate the build."
        )


def test_the_canary_still_runs():
    """Non-blocking must not become deleted — the probe is the whole point.

    The matrix is emitted as a JSON string by the matrix-setup job, so assert on
    that text rather than a parsed structure.
    """
    steps = _workflow()["jobs"]["matrix-setup"]["steps"]
    blob = " ".join(str(s.get("run", "")) for s in steps)
    assert _CANARY in blob, (
        f"Python {_CANARY} is gone from the matrix. Non-blocking keeps the "
        "early-warning probe; deleting it hides the signal we want (the day "
        "torch ships a cp315 wheel)."
    )


def test_fail_fast_stays_off():
    """One lane failing must not cancel the others, or the canary would take
    real lanes down with it before they report."""
    assert _test_job()["strategy"].get("fail-fast") is False
