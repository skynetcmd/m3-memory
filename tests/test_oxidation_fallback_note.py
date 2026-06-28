"""Tests for the Project Oxidation pure-Python fallback messaging + tier probe.

Contract (project memory 2026-06-27):
  - The reassurance must say m3 is fully functional as a pure-Python solution
    and that only Project Oxidation's hot-path optimizations are missing.
  - The RELATIVE multiplier (~10-85x) is authoritative and MAY be asserted.
  - The ABSOLUTE latencies (~10-50 ms with; ~0.3-2.5 s without) are
    ILLUSTRATIVE ONLY: they MUST carry a "varies by host" qualifier and MUST
    NEVER be asserted as a specific fact. A test may assert the HEDGE is
    present; it must not pin the number as truth.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m3_memory import rust_core_install as rci  # noqa: E402

# ── the canonical relative figure ──────────────────────────────────────────────

def test_speedup_constant_is_10_85x():
    """The one authoritative figure tests may assert."""
    assert rci.OXIDATION_SPEEDUP_X == "~10-85x"


def test_note_states_pure_python_reassurance():
    note = rci.oxidation_fallback_note()
    low = note.lower()
    assert "pure-python" in low
    assert "fully functional" in low
    assert "project oxidation" in low
    # The whole point: usable, just not maximal speed.
    assert "very usable" in low
    assert "not maximized" in low


def test_note_states_relative_multiplier():
    """The relative figure is asserted; it is the authoritative number."""
    note = rci.oxidation_fallback_note()
    assert "~10-85x" in note


def test_note_states_both_ways_with_and_without():
    """Both framings present: the with-Oxidation ms and the without seconds."""
    note = rci.oxidation_fallback_note()
    # with-Oxidation typical latency
    assert "10-50 ms" in note
    # without-Oxidation, expressed in seconds (midpoint-anchored)
    assert "0.3-2.5 s" in note


def test_absolute_latency_carries_illustrative_qualifier():
    """The absolute numbers are NOT measured — they must be hedged. We assert
    the HEDGE exists, never that the number is fact."""
    note = rci.oxidation_fallback_note().lower()
    assert "illustrative" in note
    assert "varies by host" in note


def test_note_indent_is_applied():
    note = rci.oxidation_fallback_note(indent="    ")
    # Every non-empty line is indented; blank lines stay blank (no trailing ws).
    for line in note.splitlines():
        if line:
            assert line.startswith("    ")
        else:
            assert line == ""


# ── tier probe (I3) ────────────────────────────────────────────────────────────

def test_active_embedder_tier_no_wheel(monkeypatch):
    """With m3_core_rs absent, the probe reports the pure-Python fallback and
    names the ~10-85x figure — never claims native is active."""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "m3_core_rs":
            raise ImportError("no native wheel")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    tier = rci.active_embedder_tier()
    assert tier["native"] is False
    assert tier["backend"] is None
    assert "~10-85x" in tier["summary"]
    assert "pure-Python" in tier["summary"]


def test_active_embedder_tier_with_wheel(monkeypatch):
    """A stub m3_core_rs exposing EmbeddedEmbedder is reported as native/active."""
    import types
    stub = types.ModuleType("m3_core_rs")
    stub.EmbeddedEmbedder = object          # presence is what matters
    stub.__version__ = "3.6.27"
    stub.embed_backend_label = lambda: "cpu"
    monkeypatch.setitem(sys.modules, "m3_core_rs", stub)

    tier = rci.active_embedder_tier()
    assert tier["native"] is True
    assert tier["version"] == "3.6.27"
    assert tier["backend"] == "cpu"
    assert "Project Oxidation active" in tier["summary"]
    assert "cpu" in tier["summary"]


def test_active_embedder_tier_wheel_without_embedded_feature(monkeypatch):
    """A wheel built WITHOUT the embedded feature (no EmbeddedEmbedder) is not
    treated as the active hot path."""
    import types
    stub = types.ModuleType("m3_core_rs")          # no EmbeddedEmbedder attr
    stub.__version__ = "3.6.27"
    monkeypatch.setitem(sys.modules, "m3_core_rs", stub)

    tier = rci.active_embedder_tier()
    assert tier["native"] is False
    assert "without the embedded feature" in tier["summary"].lower()


# ── privilege-probe footguns: must be safe & non-crashing everywhere ────────────

def test_can_sudo_false_when_no_sudo_binary(monkeypatch):
    """_can_sudo must NOT crash when `sudo` is absent (Windows, minimal Unix) —
    it returns False instead of raising FileNotFoundError."""
    monkeypatch.setattr(rci.shutil, "which", lambda _: None)

    def _boom(*a, **k):  # would be the FileNotFoundError path
        raise OSError("No such file or directory: 'sudo'")
    monkeypatch.setattr(rci.subprocess, "run", _boom)
    assert rci._can_sudo() is False


def test_in_privileged_group_false_on_windows(monkeypatch):
    """_in_privileged_group is sudo/wheel-specific — always False on Windows,
    and never tries to import the Unix-only `grp` module there."""
    monkeypatch.setattr(rci.sys, "platform", "win32")
    assert rci._in_privileged_group() is False
