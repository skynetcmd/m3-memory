"""Parity tests for get_governor_pacing — native (Rust) vs Python fallback (M4).

The Adaptive Governor pacing ladder has a Rust source-of-truth
(`m3_core_rs.Governor`, crate `m3-governor`) and a pure-Python fallback in
`bin/m3_sdk.py`. The oxidation is only safe if the two return byte-identical
dicts for every input. These tests assert that across the full truth table,
forcing the Python path with `M3_CORE_RS_DISABLE=1` and comparing against the
native path (when a Governor-capable wheel is installed).
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

# (load, elapsed_since_interaction) probe points covering every branch +
# boundaries: critical, throttled, halted-recent, taper window edges, idle.
_PROBES = [
    (95.0, 0.0),
    (99.9, 999.0),
    (85.0, 0.0),
    (94.9, 999.0),
    (10.0, 0.0),
    (10.0, 29.9),
    (10.0, 30.0),
    (10.0, 59.9),
    (0.0, 60.0),
    (0.0, 100.0),
]


def _load_sdk(disable_rust: bool):
    """Import a fresh m3_sdk with M3_CORE_RS_DISABLE set as requested."""
    os.environ["M3_CORE_RS_DISABLE"] = "1" if disable_rust else "0"
    sys.modules.pop("m3_sdk", None)
    return importlib.import_module("m3_sdk")


def _pacing(sdk, load: float, elapsed: float) -> dict:
    """Drive get_governor_pacing at a fixed (load, elapsed) by pinning the
    last-interaction clock so `elapsed` is deterministic."""
    import time
    sdk._LAST_USER_INTERACTION = time.time() - elapsed
    telemetry = {"cpu_total": load, "ram_total": 0.0, "gpu_total": 0.0}
    return sdk.get_governor_pacing(telemetry)


def test_python_fallback_truth_table():
    """The Python path returns the documented dict for each branch."""
    sdk = _load_sdk(disable_rust=True)
    # Defaults: INITIAL_LIMIT=85, LIMIT_THRESHOLD=95 (env unset).
    out = _pacing(sdk, 95.0, 0.0)
    assert out == {"background": "HALTED", "interactive_delay": 30.0}
    out = _pacing(sdk, 85.0, 0.0)
    assert out == {"background": "THROTTLED", "background_delay": 10.0, "interactive_delay": 0.0}
    out = _pacing(sdk, 10.0, 0.0)
    assert out == {"background": "HALTED", "interactive_delay": 0.0}
    out = _pacing(sdk, 10.0, 45.0)
    assert out == {"background": "TAPERED", "background_delay": 5.0, "interactive_delay": 0.0}
    out = _pacing(sdk, 0.0, 120.0)
    assert out == {"background": "CONTINUOUS", "background_delay": 0.1, "interactive_delay": 0.0}


def _has_native_governor() -> bool:
    try:
        import m3_core_rs
        return hasattr(m3_core_rs, "Governor")
    except Exception:
        return False


@pytest.mark.skipif(
    not _has_native_governor(),
    reason="native m3_core_rs.Governor not installed in this interpreter; "
           "parity is covered by the Rust crate's own tests/parity.rs",
)
def test_native_matches_python_fallback():
    """Native and Python paths must return identical dicts for every probe."""
    py_sdk = _load_sdk(disable_rust=True)
    py_results = [_pacing(py_sdk, load, elapsed) for load, elapsed in _PROBES]

    native_sdk = _load_sdk(disable_rust=False)
    native_results = [_pacing(native_sdk, load, elapsed) for load, elapsed in _PROBES]

    for (load, elapsed), py, native in zip(_PROBES, py_results, native_results):
        assert py == native, f"divergence at load={load}, elapsed={elapsed}: py={py} native={native}"


def teardown_module(module):
    os.environ.pop("M3_CORE_RS_DISABLE", None)
    sys.modules.pop("m3_sdk", None)
