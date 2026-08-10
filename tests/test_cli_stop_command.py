r"""`m3 stop` — quiesce m3's DB-writers before an upgrade.

Why this command exists: `pipx upgrade m3-memory` is unsafe while m3 is running
on Windows. pip UNINSTALLS before it installs, Windows holds an open .exe
against deletion, so the upgrade deletes the package and then fails on
`[WinError 32] ... Scripts\m3.exe` — leaving NO m3 installed (`m3` then dies
with ModuleNotFoundError) plus ~-prefixed junk dirs from the half-undone
uninstall. Observed on a real install 2026-08-10.

The contract this pins:
  - delegates to m3_halt.kill_stale_daemons (precise-PID; never a name sweep)
  - "nothing running" is exit 0, not an error
  - a PARTIAL stop is exit 1 — it must not read as success, because the
    caller's next step is an upgrade that will then fail (§3)
"""
from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from m3_memory import cli  # noqa: E402


@pytest.fixture
def fake_halt(monkeypatch):
    """Install a stub m3_halt so the test never touches real processes."""
    mod = types.ModuleType("m3_halt")
    mod.calls = []

    def kill_stale_daemons(engine_root=None, *, timeout=8.0):
        mod.calls.append(timeout)
        return mod.results

    mod.kill_stale_daemons = kill_stale_daemons
    mod.results = []
    monkeypatch.setitem(sys.modules, "m3_halt", mod)
    monkeypatch.setattr(cli, "_resolve_bin_script",
                        lambda name: Path(_ROOT / "bin" / "m3_halt.py"))
    return mod


def _run(timeout=8.0):
    return cli._cmd_stop(argparse.Namespace(timeout=timeout))


def test_nothing_running_is_success(fake_halt, capsys):
    fake_halt.results = []
    assert _run() == 0
    assert "nothing to stop" in capsys.readouterr().out


def test_all_stopped_is_success(fake_halt, capsys):
    fake_halt.results = [
        {"pid": 1, "role": "cognitive-loop", "killed": True, "error": None},
        {"pid": 2, "role": "embed-server", "killed": True, "error": None},
    ]
    assert _run() == 0
    out = capsys.readouterr().out
    assert "stopped cognitive-loop (pid 1)" in out
    assert "stopped 2/2 writer(s)" in out


def test_partial_stop_is_a_failure(fake_halt, capsys):
    """A survivor means the upgrade will still hit a file lock — say so, loudly.

    Reporting 0 here would hand the user a green light into the exact failure
    this command exists to prevent.
    """
    fake_halt.results = [
        {"pid": 1, "role": "cognitive-loop", "killed": True, "error": None},
        {"pid": 2, "role": "mcp", "killed": False, "error": "AccessDenied"},
    ]
    assert _run() == 1, "a partial stop must not report success"
    cap = capsys.readouterr()
    assert "could NOT stop mcp (pid 2)" in cap.err
    assert "AccessDenied" in cap.err
    assert "elevated" in cap.err


def test_timeout_is_forwarded(fake_halt):
    fake_halt.results = []
    _run(timeout=2.5)
    assert fake_halt.calls == [2.5]


def test_missing_payload_reports_and_fails(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_resolve_bin_script", lambda name: None)
    assert cli._cmd_stop(argparse.Namespace(timeout=8.0)) == 1
    assert "m3_halt.py not found" in capsys.readouterr().err
