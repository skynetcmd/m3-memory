"""The native Rust core is installed on EVERY setup, and verified up to date.

m3 has shipped Rust-enabled by default for several versions. It was not true in
fact: `m3 setup` only installed the m3-core-rs wheel when the user answered YES
to an OPT-IN tier-1 prompt whose default was NO. Declining it meant no wheel --
and because embedder_admin._server_binary() resolves the shared `m3-embed-server`
binary from INSIDE that wheel, the very next step (`m3 embedder install`, the
"sovereign CPU embedder ... always installed" one) then failed with
"m3-embed-server binary not found" and was reported as "SKIPPED. This is fine."

So a default install advertised an always-on embedder and shipped neither it nor
the core, while the fix message pointed at `m3 embedder install-gpu` -- the step
the user had just been asked to skip.

The tier choice is WHICH EMBEDDER RUNS (tier 2 alone / tier 1 + tier 2 / tier 3
remote); the core is required by all three. These tests pin that.
"""
from __future__ import annotations

import subprocess
import unittest.mock as mock

import pytest

from m3_memory import setup_wizard as w


class _Plan:
    allow_native_source_build = False


def _drive(*, current, install_raises=None, plan=None):
    """Run _step_rust_core with the core-state probes stubbed.

    Returns (result, list_of_commands_run).
    """
    ran: list[list[str]] = []

    def _fake_run(cmd, *a, **k):
        ran.append(cmd)
        if install_raises is not None:
            raise install_raises

    with mock.patch("m3_memory.rust_core_install.is_rust_core_current",
                    return_value=current), \
         mock.patch("m3_memory.rust_core_install.active_embedder_tier",
                    return_value={"native": True, "backend": "cpu",
                                  "version": "3.9.7"}), \
         mock.patch("m3_memory.rust_core_install.M3_CORE_RS_VERSION", "3.9.7"), \
         mock.patch.object(w, "_run", _fake_run):
        out = w._step_rust_core(plan or _Plan())
    return out, ran


def test_stale_core_is_upgraded_on_setup():
    """The whole point: an UPGRADE with an old wheel pulls it forward.

    is_rust_core_current() compares installed __version__ against the pin, so a
    3.7.31 wheel under a 3.9.7 pin reinstalls rather than being skipped.
    """
    ok, ran = _drive(current=False)
    assert ok is True
    assert len(ran) == 1
    assert "install-gpu" in ran[0]


def test_current_core_is_not_reinstalled():
    """Idempotent: a host already at the pin must not re-download ~100s of MB."""
    ok, ran = _drive(current=True)
    assert ok is True
    assert ran == []


def test_never_compiles_from_source_by_default():
    """A default setup must not launch a multi-minute Rust+cmake build."""
    _ok, ran = _drive(current=False)
    assert "--no-source-fallback" in ran[0]


def test_source_build_opt_in_is_honoured():
    class _OptIn:
        allow_native_source_build = True

    _ok, ran = _drive(current=False, plan=_OptIn())
    assert "--no-source-fallback" not in ran[0]


def test_no_matching_wheel_degrades_and_never_aborts_setup():
    """§1 local-first: a host with no prebuilt wheel (unsupported arch, or a
    Python outside 3.11-3.14) falls back to pure-Python. Setup CONTINUES."""
    ok, ran = _drive(current=False,
                     install_raises=subprocess.CalledProcessError(1, "x"))
    assert ok is False          # reported, not raised
    assert len(ran) == 1


def test_probe_failure_never_aborts_setup():
    """An import/probe error must not take the wizard down with it."""
    with mock.patch("m3_memory.rust_core_install.is_rust_core_current",
                    side_effect=RuntimeError("probe exploded")), \
         mock.patch.object(w, "_run", lambda *a, **k: None):
        # Must return (False) rather than propagate.
        assert w._step_rust_core(_Plan()) in (False, True)


def test_core_step_runs_before_the_embedder_step():
    """Ordering is the bug: tier 2's server binary ships inside the wheel, so
    installing the embedder before the core is what produced
    'm3-embed-server binary not found' on a default install."""
    import inspect

    src = inspect.getsource(w)
    # Call sites, not definitions -- match the invocation forms.
    i_core = src.index("_step_rust_core(plan)")
    i_emb = src.index("_step_cpu_sovereign_embedder()\n", src.index("_step_rust_core(plan)") - 400)
    assert i_core < i_emb, "the native core must be installed before the embedder"


def test_core_step_is_unconditional():
    """It must NOT sit behind `if plan.install_gpu_embedder:` -- that flag is
    the tier-1 in-process choice, not a choice about having the core at all."""
    import inspect
    import re

    src = inspect.getsource(w)
    call = src.index("_step_rust_core(plan)")
    # Walk back to the start of that line and confirm it is not nested under
    # an `if plan.install_gpu_embedder` guard.
    line_start = src.rfind("\n", 0, call) + 1
    preceding = src[max(0, line_start - 300):line_start]
    last_if = preceding.rfind("if plan.install_gpu_embedder")
    assert last_if == -1 or "\n" in preceding[last_if:], (
        "_step_rust_core must not be gated on the tier-1 opt-in flag"
    )


@pytest.mark.parametrize("os_tok,backend", [
    ("windows", "cpu"), ("windows", "cuda"), ("windows", "vulkan"),
    ("linux", "cpu"), ("linux", "cuda"), ("linux", "vulkan"),
    ("macos", "metal"),
])
def test_every_supported_os_backend_pair_resolves(os_tok, backend):
    """3 OSes x their backends: the core step delegates to detect_backend(), so
    it must stay OS-agnostic. Guards against a future hardcoded platform."""
    from m3_memory.rust_core_install import _VALID, package_name

    assert (os_tok, backend) in _VALID
    assert package_name(os_tok, backend) == f"m3-core-rs-{os_tok}-{backend}"
