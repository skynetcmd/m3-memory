"""The jobs that must stay scheduled on macOS/Linux, rendered from one spec.

Hazards this pins:
  * macOS: the hourly warehouse sync ran from a hand-made plist with a DAILY
    calendar trigger, and m3 installed no floor of its own; verify said OK.
  * Linux: the managed cron block came from a second, hand-kept template that
    had drifted from the specs, and its HourlySync line ran pg_sync.sh, which
    calls a `.venv/bin/python` no pipx install has, so every run failed.
All hermetic: a temp HOME, launchctl/crontab stubbed.
"""
from __future__ import annotations

import os
import plistlib
import sys

import pytest

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import install_schedules as sched  # noqa: E402

_ROOT = os.path.normpath(os.path.join(_BIN, ".."))
_PY = "/venv/bin/python"


class _Res:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.fixture
def unix(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(os.path, "expanduser",
                        lambda p: p.replace("~", str(home), 1) if p.startswith("~") else p)
    monkeypatch.setattr(sched, "_venv_python", lambda root, **k: _PY)
    monkeypatch.setattr(sched, "_logs_dir", lambda: str(tmp_path / "logs"))
    state = {"cron": "", "loaded": set()}

    def fake_run(cmd, **_kw):
        if cmd[:2] == ["crontab", "-l"]:
            return _Res(0, state["cron"])
        if cmd[0] == "crontab":
            with open(cmd[1], encoding="utf-8") as f:
                state["cron"] = f.read()
            return _Res()
        if cmd[:2] == ["launchctl", "load"]:
            state["loaded"].add(os.path.basename(cmd[2])[:-len(".plist")])
            return _Res()
        if cmd[:2] == ["launchctl", "list"]:
            return _Res(0, "\n".join(f"-\t0\t{label}" for label in state["loaded"]))
        return _Res()

    monkeypatch.setattr(sched, "_run", fake_run)
    state["agents"] = home / "Library" / "LaunchAgents"
    return state


def _specs():
    return {s["name"]: s for s in sched.unix_periodic_specs(_ROOT)}


def test_the_floors_are_the_governor_floors_plus_the_rotator():
    assert set(_specs()) == {"AgentOS_HourlySync", "AgentOS_ChatlogEmbedSweep",
                             "AgentOS_SecretRotator"}


@pytest.mark.parametrize("spec, launchd, cron", [
    ({"name": "x", "schedule": "MINUTE", "modifier": "30", "time": "00:00"},
     {"StartInterval": 1800}, "*/30 * * * *"),
    ({"name": "x", "schedule": "HOURLY", "modifier": "1", "time": "00:00"},
     {"StartInterval": 3600}, "0 * * * *"),
    ({"name": "x", "schedule": "DAILY", "modifier": "", "time": "03:15"},
     {"StartCalendarInterval": {"Hour": 3, "Minute": 15}}, "15 3 * * *"),
    ({"name": "x", "schedule": "WEEKLY", "modifier": "FRI", "time": "16:00"},
     {"StartCalendarInterval": {"Weekday": 5, "Hour": 16, "Minute": 0}}, "0 16 * * 5"),
    ({"name": "x", "schedule": "MONTHLY", "modifier": "1", "time": "02:00"},
     {"StartCalendarInterval": {"Day": 1, "Hour": 2, "Minute": 0}}, "0 2 1 * *"),
])
def test_one_schedule_maps_the_same_way_on_every_unix(spec, launchd, cron):
    assert sched.launchd_trigger(spec) == launchd
    assert sched.cron_schedule(spec) == cron


def test_hourly_sync_keeps_its_existing_mac_label():
    assert sched.launchd_label("AgentOS_HourlySync") == "com.m3memory.sync_all"
    assert sched.launchd_label("AgentOS_ChatlogEmbedSweep") == "com.m3memory.chatlogembedsweep"


def test_cron_block_holds_exactly_the_floors_from_the_specs(unix):
    block = sched.render_cron_block(_ROOT)
    specs = _specs()
    import shlex
    sync = specs["AgentOS_HourlySync"]
    assert f"0 * * * * {shlex.join([_PY, *sync['args']])}" in block
    assert "pg_sync.sh" not in block and ".venv/bin/python" not in block
    for absent in ("memory_maintenance.py", "m3_enrich.py", "weekly_auditor.py"):
        assert absent not in block, "governed jobs run in the loop, not cron"
    assert "m3_loop_watchdog.py" not in block, "the watchdog is a timer unit on Unix"


def test_removing_the_block_keeps_the_users_own_lines(unix):
    unix["cron"] = "5 4 * * * my-own-job\n"
    sched.install_unix_crontab(_ROOT)
    assert "m3-managed" in unix["cron"] and "my-own-job" in unix["cron"]
    sched.install_unix_crontab(_ROOT, block=False)
    assert "m3-managed" not in unix["cron"]
    assert "my-own-job" in unix["cron"]


def test_linux_verify_passes_the_rendered_block_and_catches_the_old_wrapper(unix, capsys):
    sched.install_unix_crontab(_ROOT)
    assert sched.verify_linux_periodic(_ROOT) is True
    unix["cron"] = unix["cron"].replace(
        next(ln for ln in unix["cron"].splitlines() if "sync_all.py" in ln),
        "0 * * * * /p/m3_memory/bin/pg_sync.sh >> /l/sync_all.log 2>&1")
    assert sched.verify_linux_periodic(_ROOT) is False
    assert "AgentOS_HourlySync: cron line differs" in capsys.readouterr().out


def test_macos_install_writes_hourly_and_backs_up_a_hand_made_plist(unix):
    hand = unix["agents"] / "com.m3memory.sync_all.plist"
    with open(hand, "wb") as fh:
        plistlib.dump({"Label": "com.m3memory.sync_all",
                       "StartCalendarInterval": {"Hour": 3, "Minute": 0}}, fh)
    assert sched.install_macos_periodic(_ROOT) is True
    with open(hand, "rb") as fh:
        got = plistlib.load(fh)
    assert got["StartInterval"] == 3600 and "StartCalendarInterval" not in got
    assert got["ProgramArguments"][0] == _PY
    assert any(p.name.startswith("com.m3memory.sync_all.plist.bak-")
               for p in unix["agents"].iterdir())


def test_macos_install_leaves_a_current_plist_untouched(unix):
    sched.install_macos_periodic(_ROOT)
    before = {p.name: p.stat().st_mtime_ns for p in unix["agents"].iterdir()}
    sched.install_macos_periodic(_ROOT)
    assert {p.name: p.stat().st_mtime_ns for p in unix["agents"].iterdir()} == before


def test_macos_verify_catches_the_daily_trigger(unix, capsys):
    sched.install_macos_periodic(_ROOT)
    assert sched.verify_macos_periodic(_ROOT) is True
    hand = unix["agents"] / "com.m3memory.sync_all.plist"
    with open(hand, "rb") as fh:
        data = plistlib.load(fh)
    data.pop("StartInterval")
    data["StartCalendarInterval"] = {"Hour": 3, "Minute": 0}
    with open(hand, "wb") as fh:
        plistlib.dump(data, fh)
    assert sched.verify_macos_periodic(_ROOT) is False
    assert "AgentOS_HourlySync: StartInterval, StartCalendarInterval differ" in capsys.readouterr().out
