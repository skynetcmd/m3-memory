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
    # The real classification, not a copy of it.
    import importlib.util
    spec = importlib.util.spec_from_file_location("_m3_halt_real", _ROOT / "bin" / "m3_halt.py")
    real = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "_m3_halt_real", real)  # dataclasses need it
    spec.loader.exec_module(real)
    mod.holds_store = real.holds_store
    monkeypatch.setitem(sys.modules, "m3_halt", mod)
    # Never stop the developer's real systemd/launchd services.
    sched = types.ModuleType("install_schedules")
    sched.stopped = []
    sched.stop_unix_services = lambda: list(sched.stopped)
    monkeypatch.setitem(sys.modules, "install_schedules", sched)
    mod.sched = sched
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


@pytest.mark.skipif(sys.platform == "win32", reason="service managers are macOS/Linux")
def test_supervised_services_are_stopped_through_the_manager(fake_halt, capsys):
    fake_halt.sched.stopped = ["m3-cognitive-loop.service"]
    fake_halt.results = []
    assert _run() == 0
    out = capsys.readouterr().out
    assert "stopped service m3-cognitive-loop.service" in out
    assert "m3 setup" in out


def test_stop_unix_services_stops_units_and_the_watchdog_timer(monkeypatch):
    """systemd restarts a killed Restart=always unit; only `stop` holds it."""
    sys.path.insert(0, str(_ROOT / "bin"))
    import install_schedules as sched

    calls = []

    class _R:
        returncode = 0
        stderr = ""

        def __init__(self, out=""):
            self.stdout = out

    inactive = {"m3-dashboard.service"}

    def fake_run(cmd, **k):
        calls.append(cmd)
        if cmd[2] == "is-active":
            return _R("inactive" if cmd[3] in inactive else "active")
        return _R()

    monkeypatch.setattr(sched, "_platform_key", lambda: "linux")
    monkeypatch.setattr(sched, "_service_exists",
                        lambda n: n != "m3-notification-waiter.service")
    monkeypatch.setattr(sched, "_run", fake_run)
    stopped = sched.stop_unix_services()
    assert ["systemctl", "--user", "stop", "m3-cognitive-loop.service"] in calls
    assert ["systemctl", "--user", "stop", "m3-loop-watchdog.timer"] in calls
    assert "m3-notification-waiter.service" not in stopped
    # An already-stopped unit is neither stopped again nor reported.
    assert "m3-dashboard.service" not in stopped
    assert ["systemctl", "--user", "stop", "m3-dashboard.service"] not in calls


def test_a_surviving_embed_server_is_left_running_not_a_failure(fake_halt, capsys):
    """The embed server holds no database, so an elevated one that cannot be
    stopped does not block an upgrade; say it was left, never that it stopped."""
    fake_halt.results = [
        {"pid": 1, "role": "cognitive-loop", "killed": True, "error": None},
        {"pid": 2, "role": "embed-server(elevated?)", "killed": False,
         "error": "ERROR: could not be terminated.\nReason: Access is denied."},
    ]
    assert _run() == 0
    out = capsys.readouterr().out
    assert "stopped embed-server" not in out
    assert "left running: embed-server(elevated?) (pid 2)" in out
    assert "Reason: Access is denied." in out          # one line, not two
    assert "stopped 1/1 writer(s); 1 left running (no database)" in out
