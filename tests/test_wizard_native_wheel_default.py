"""The tier-1 Project Oxidation native wheel is OPT-IN (shared tier-2 is default).

The shipped default embedder is the SHARED tier-2 server (:8082) that every m3
process uses; auto-installing the tier-1 native in-process wheel on top is
redundant (and per-process tier-1 is the N-CUDA-context / multi-GiB-RAM cost the
shared server exists to avoid). So the wizard defaults the native wheel OFF and
only installs it when explicitly requested via --install-gpu-embedder. These
tests pin that opt-in contract through the non-interactive plan builder.
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
        install_gpu_embedder=False,   # tier-1 opt-in flag, default off
        no_native_wheel=False,        # explicit opt-out, default off
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


def test_native_wheel_off_by_default():
    """Plain non-interactive run → tier-1 native wheel NOT installed (the shared
    tier-2 server is the default embedder)."""
    plan = setup_wizard._gather_plan(setup_wizard.AgentTargets(), _ns())
    assert plan.install_gpu_embedder is False
    assert plan.allow_native_source_build is False


def test_install_gpu_flag_opts_in():
    """--install-gpu-embedder opts INTO the tier-1 native wheel."""
    plan = setup_wizard._gather_plan(
        setup_wizard.AgentTargets(), _ns(install_gpu_embedder=True)
    )
    assert plan.install_gpu_embedder is True


def test_no_native_wheel_wins_over_opt_in():
    """--no-native-wheel forces the wheel OFF even if --install-gpu-embedder is
    also (contradictorily) passed — the explicit DISABLE wins, so a script that
    hard-disables the native path can't be surprised by a stray enable flag."""
    plan = setup_wizard._gather_plan(
        setup_wizard.AgentTargets(),
        _ns(no_native_wheel=True, install_gpu_embedder=True),
    )
    assert plan.install_gpu_embedder is False


def test_source_build_opt_in():
    """--allow-native-source-build flips the source-build last resort on."""
    plan = setup_wizard._gather_plan(
        setup_wizard.AgentTargets(), _ns(allow_native_source_build=True)
    )
    assert plan.allow_native_source_build is True


def test_dataclass_defaults_match_shared_first_posture():
    """The SetupPlan defaults themselves encode 'shared tier-2 default, tier-1
    opt-in': native OFF, shared ON, no source build."""
    plan = setup_wizard.SetupPlan()
    assert plan.install_gpu_embedder is False
    assert plan.use_shared_embedder is True
    assert plan.allow_native_source_build is False
