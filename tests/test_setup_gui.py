"""Tests for the optional graphical setup front-end (m3_memory.setup_gui).

These cover the pure logic — flag mapping and the GUI/terminal selector — without
opening a window, so they run headlessly in CI. The GUI itself is a thin shell
around `m3 setup --non-interactive <flags>`; the flag map is the contract that
must stay correct, and the selector's no-loop guard is the safety-critical part.
"""
from __future__ import annotations

import argparse
import sys

import pytest

from m3_memory.setup_gui import _TOOLTIPS, _build_flags, _doctor_line_status
from m3_memory.setup_wizard import _should_use_gui


def _ns(**kw):
    base = dict(non_interactive=False, terminal=False, gui=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_empty_values_are_minimal():
    # With NO values set (nothing observed), _build_flags emits only the base
    # flag — the mapping itself adds nothing speculative.
    assert _build_flags({}) == ["--non-interactive"]


def test_gui_default_state_flags():
    # The GUI pre-selects several recommended defaults (cognitive loop, wipe
    # __pycache__, force-kill mcp, decoupled roots). Simulate that default state
    # and confirm the emitted command matches — this is the contract between the
    # checkbox defaults and what `m3 setup` actually receives.
    default_state = {
        "capture_mode": "both",          # default; omitted (== wizard default)
        "no_native_wheel": False,        # keep native wheel; omitted
        "cognitive_loop": True,          # default ON -> flag
        "clean_cache": True,             # default ON -> flag
        "force_kill_mcp": True,          # default ON -> flag
        "decouple_roots": True,          # default ON -> flag (+ pre-filled roots)
        # Synthetic, non-home-shaped test paths (not real user dirs — avoids
        # tripping the pre-push PII scan on ~/home patterns).
        "config_root": "TESTROOT/config",
        "engine_root": "TESTROOT/engine",
    }
    flags = _build_flags(default_state)
    assert "--cognitive-loop" in flags
    assert "--clean-cache" in flags
    assert "--force-kill-mcp" in flags
    assert "--decouple-roots" in flags
    assert "--config-root" in flags and "TESTROOT/config" in flags
    assert "--engine-root" in flags and "TESTROOT/engine" in flags
    # 'both' is the wizard default, so capture-mode must NOT be emitted.
    assert "--capture-mode" not in flags


def test_capture_both_is_omitted_but_others_emit():
    assert _build_flags({"capture_mode": "both"}) == ["--non-interactive"]
    assert _build_flags({"capture_mode": "stop"}) == [
        "--non-interactive", "--capture-mode", "stop"]


def test_agents_subset_is_comma_joined():
    flags = _build_flags({"agent_claude": True, "agent_gemini": True,
                          "agent_opencode": False})
    assert flags == ["--non-interactive", "--agents", "claude,gemini"]


def test_fips_strict_excludes_mode_and_gates_wolfssl():
    # strict implies mode (the child enforces) — don't emit both; wolfssl rides.
    assert _build_flags({"fips_strict": True, "install_wolfssl": True}) == [
        "--non-interactive", "--fips-strict", "--install-wolfssl"]
    # wolfssl without any FIPS must NOT be emitted (meaningless there).
    assert _build_flags({"install_wolfssl": True}) == ["--non-interactive"]


def test_decouple_roots_with_paths():
    flags = _build_flags({"decouple_roots": True,
                          "config_root": "D:/cfg", "engine_root": "D:/eng"})
    assert flags == ["--non-interactive", "--decouple-roots",
                     "--config-root", "D:/cfg", "--engine-root", "D:/eng"]


def test_endpoint_is_stripped():
    assert _build_flags({"endpoint": "  http://x:1234  "}) == [
        "--non-interactive", "--endpoint", "http://x:1234"]


def test_selector_never_loops_in_non_interactive():
    # SAFETY-CRITICAL: the GUI spawns `m3 setup --non-interactive`; that child
    # must never re-enter the GUI, or we get an infinite GUI->child->GUI loop.
    assert _should_use_gui(_ns(non_interactive=True)) is False
    assert _should_use_gui(_ns(gui=True, non_interactive=True)) is False


def test_selector_respects_terminal_flag():
    assert _should_use_gui(_ns(terminal=True)) is False


# Note: the GUI no longer content-scans doctor stderr. It shows stdout (the
# --brief verdicts) verbatim, archives full stderr to a temp file, and surfaces
# that path only on a non-zero exit code. The engine-side --brief distillation
# is what makes stdout clean (bin/doctor/*_probe.py, bin/memory_doctor.py).


def test_doctor_status_classifier_maps_tags():
    f = _doctor_line_status
    assert f("✅ embedding-cascade: healthy") == ("ok", "embedding-cascade: healthy")
    assert f("[OK] m3 HEALTHY") == ("ok", "m3 HEALTHY")
    assert f("⚠️  governor: NAG (5)") == ("warn", "governor: NAG (5)")
    assert f("[WARN] something") == ("warn", "something")
    assert f("❌ exit 5") == ("fail", "exit 5")
    assert f("[FAIL] embed-server down") == ("fail", "embed-server down")


def test_doctor_status_classifier_preserves_indent():
    # Leading indentation is kept so nested lines stay aligned after the bullet.
    assert _doctor_line_status("  ✅ oxidation: current") == ("ok", "  oxidation: current")


def test_doctor_status_classifier_ignores_plain_lines():
    assert _doctor_line_status("embed-server: not installed") is None
    assert _doctor_line_status("agent MCP configs:") is None
    assert _doctor_line_status("") is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS embedder wording")
def test_macos_embedder_tooltips_name_metal_and_xcode():
    # On macOS the embedder tooltips must name the REAL back-end/prereqs so the
    # user isn't guessing (DESIGN_PHILOSOPHIES §3, §5). _apply_platform_tooltips
    # runs at import, so the module-level dict already reflects this platform.
    assert "Metal" in _TOOLTIPS["no_native_wheel"]
    assert "Xcode" in _TOOLTIPS["allow_native_source_build"]
    assert "cmake" in _TOOLTIPS["allow_native_source_build"]
