"""`m3 update` offers the native-core upgrade when the wheel is behind the pin.

`m3 update` re-syncs the PAYLOAD. Nothing in that path ever touched the native
m3-core-rs wheel -- only install_os.py and `m3 embedder install-gpu` call
install_rust_core. So `pipx upgrade m3-memory` followed by `m3 update` left the
user on NEW Python code with an OLD Rust core, silently and with no prompt.

The skew is degraded rather than broken (memory/tokens.py cascades
Rust -> exact_fn -> estimator), which is exactly why it needs to be surfaced:
a silent partial upgrade that still "works" is the kind of thing users only
discover when they wonder why a fixed bug still bites.

These tests pin the decision table, not the wording.
"""
from __future__ import annotations

import io
import sys
import unittest.mock as mock

from m3_memory import cli


def _run(*, current, installed, interactive, assume_yes=False, skip=False,
         answer="", install_rc=0):
    """Drive _offer_rust_core_upgrade with the core-state probes stubbed.

    Returns (stderr_text, list_of_install_calls).
    """
    calls: list[dict] = []

    def _fake_install(**kwargs):
        calls.append(kwargs)
        return install_rc

    with mock.patch("m3_memory.rust_core_install.is_rust_core_current",
                    return_value=current), \
         mock.patch("m3_memory.rust_core_install.installed_rust_core_version",
                    return_value=installed), \
         mock.patch("m3_memory.rust_core_install.install_rust_core",
                    side_effect=_fake_install), \
         mock.patch("builtins.input", return_value=answer):
        buf = io.StringIO()
        real = sys.stderr
        sys.stderr = buf
        try:
            cli._offer_rust_core_upgrade(
                interactive=interactive, assume_yes=assume_yes, skip=skip,
            )
        finally:
            sys.stderr = real
    return buf.getvalue(), calls


def test_current_core_is_silent():
    """No output and no install when the core already satisfies the pin."""
    out, calls = _run(current=True, installed="9.9.9", interactive=True)
    assert out == ""
    assert calls == []


def test_stale_core_default_answer_installs():
    """Bare ENTER accepts -- the prompt is [Y/n], so empty means yes."""
    out, calls = _run(current=False, installed="3.7.31", interactive=True,
                      answer="")
    assert "behind this release" in out
    assert len(calls) == 1
    # Must not silently start a multi-minute Rust+cmake build, same rule
    # install_os.py follows.
    assert calls[0] == {"allow_source_fallback": False}


def test_stale_core_explicit_no_declines():
    out, calls = _run(current=False, installed="3.7.31", interactive=True,
                      answer="n")
    assert calls == []
    assert "install-gpu" in out  # tells them how to do it later


def test_non_interactive_never_blocks():
    """A scripted/CI `m3 update` must report and move on, never prompt."""
    out, calls = _run(current=False, installed="3.7.31", interactive=False)
    assert calls == []
    assert "install-gpu" in out


def test_yes_core_installs_without_a_tty():
    """--yes-core is the scripted opt-in; it must not need a TTY."""
    out, calls = _run(current=False, installed="3.7.31", interactive=False,
                      assume_yes=True)
    assert len(calls) == 1


def test_no_core_suppresses_everything():
    out, calls = _run(current=False, installed="3.7.31", interactive=True,
                      skip=True)
    assert out == ""
    assert calls == []


def test_missing_core_is_reported_not_crashed():
    """installed_rust_core_version() returns None when no wheel is present."""
    out, calls = _run(current=False, installed=None, interactive=True, answer="")
    assert "not installed" in out
    assert len(calls) == 1


def test_failed_install_is_reported_and_non_fatal():
    """A non-zero install must not raise -- embeddings still work on tier-2."""
    out, calls = _run(current=False, installed="3.7.31", interactive=True,
                      answer="", install_rc=3)
    assert len(calls) == 1
    assert "exit 3" in out
    assert "tier-2" in out


def test_probe_failure_is_swallowed():
    """Advisory only: an import/probe error must never fail `m3 update`."""
    with mock.patch("m3_memory.rust_core_install.is_rust_core_current",
                    side_effect=RuntimeError("probe exploded")):
        buf = io.StringIO()
        real = sys.stderr
        sys.stderr = buf
        try:
            cli._offer_rust_core_upgrade(interactive=True)  # must not raise
        finally:
            sys.stderr = real


def test_update_subcommand_exposes_both_flags():
    """The flags _cmd_update reads must actually exist on the parser."""
    import subprocess

    out = subprocess.run(
        [sys.executable, "-m", "m3_memory.cli", "update", "--help"],
        capture_output=True, text=True, check=False,
    ).stdout
    assert "--yes-core" in out
    assert "--no-core" in out
