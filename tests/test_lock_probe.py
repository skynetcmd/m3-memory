"""Tests for the single-instance lock doctor probe (bin/doctor/lock_probe.py)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import m3_halt  # noqa: E402
from doctor import lock_probe  # noqa: E402


@pytest.fixture
def engine(tmp_path, monkeypatch):
    root = tmp_path / "engine"
    monkeypatch.setenv("M3_ENGINE_ROOT", str(root))
    return str(root)


def test_probe_ok_when_no_events(engine, capsys):
    rc = lock_probe.run(brief=False)
    assert rc == 0
    assert "no lock events" in capsys.readouterr().out.lower()


def test_probe_flags_degraded(engine, capsys):
    m3_halt._log_lock_event("config_error", "embed-server", engine, error="unwritable")
    rc = lock_probe.run(brief=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "DEGRADED" in out and "embed-server" in out


def test_probe_flags_flapping(engine, capsys):
    for _ in range(6):
        m3_halt._log_lock_event("held_by_peer", "dashboard", engine, owner_pid=99)
    rc = lock_probe.run(brief=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "FLAPPING" in out and "dashboard" in out


def test_probe_brief_summarizes(engine, capsys):
    m3_halt._log_lock_event("config_error", "mcp-proxy", engine, error="x")
    lock_probe.run(brief=True)
    out = capsys.readouterr().out
    assert out.startswith("locks:") and "degraded" in out


def test_probe_never_bumps_exit_code(engine):
    # Report-only: always 0, even with problems present.
    m3_halt._log_lock_event("lock_error", "cognitive-loop", engine, error="boom")
    assert lock_probe.run(brief=False) == 0
    assert lock_probe.run(brief=True) == 0
