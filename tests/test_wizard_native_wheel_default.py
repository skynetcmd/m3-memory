"""F1: the Project Oxidation native wheel is attempted BY DEFAULT.

The wheel install is a SAFE attempt — the 3-tier cascade is non-fatal and m3
auto-falls-back to pure-Python if no wheel matches. So the wizard defaults it
ON, but NEVER auto-compiles from source (that stays opt-in). These tests pin
that default contract through the non-interactive plan builder.
"""
from __future__ import annotations

import argparse

from m3_memory import setup_wizard


def _ns(**over):
    """Build a Namespace matching `add_arguments` defaults, with overrides."""
    base = dict(
        non_interactive=True,
        agents=None,
        capture_mode=None,
        clean_cache=False,
        force_kill_mcp=False,
        install_gpu_embedder=False,   # legacy flag, default off
        no_native_wheel=False,        # new opt-out, default off
        allow_native_source_build=False,
        endpoint=None,
        cognitive_loop=False,
        decouple_roots=False,
        config_root=None,
        engine_root=None,
        fips_mode=False,
        no_governor_migration=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_native_wheel_on_by_default():
    """Plain non-interactive run → native wheel attempted, no source build."""
    plan = setup_wizard._gather_plan(setup_wizard.AgentTargets(), _ns())
    assert plan.install_gpu_embedder is True
    assert plan.allow_native_source_build is False


def test_no_native_wheel_opts_out():
    """--no-native-wheel disables the attempt (pure-Python only)."""
    plan = setup_wizard._gather_plan(
        setup_wizard.AgentTargets(), _ns(no_native_wheel=True)
    )
    assert plan.install_gpu_embedder is False


def test_legacy_install_gpu_flag_still_forces_on():
    """--install-gpu-embedder forces the wheel on even if --no-native-wheel is
    also (contradictorily) passed — back-compat: the explicit enable wins."""
    plan = setup_wizard._gather_plan(
        setup_wizard.AgentTargets(),
        _ns(no_native_wheel=True, install_gpu_embedder=True),
    )
    assert plan.install_gpu_embedder is True


def test_source_build_opt_in():
    """--allow-native-source-build flips the source-build last resort on."""
    plan = setup_wizard._gather_plan(
        setup_wizard.AgentTargets(), _ns(allow_native_source_build=True)
    )
    assert plan.allow_native_source_build is True


def test_dataclass_defaults_match_safe_posture():
    """The SetupPlan defaults themselves encode 'attempt prebuilt, no source'."""
    plan = setup_wizard.SetupPlan()
    assert plan.install_gpu_embedder is True
    assert plan.allow_native_source_build is False
