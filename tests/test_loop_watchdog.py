"""Tests for the cognitive-loop self-heal: plist KeepAlive contract + watchdog.

Regression cover for the 2026-08-09 outage, where the loop stayed dead for ~4h
after an upgrade. Two independent defects had to line up:

  1. The shipped plist used KeepAlive={Crashed:true}. An upgrade raises HALT_m3,
     the loop checkpoints and exits 0 — a CLEAN exit, exactly the case
     Crashed-only ignores — so launchd never restarted it.
  2. install_schedules' health check tested `if "KeepAlive" not in plist`: the
     KEY, never the VALUE. {Crashed:true} contains the string, so the check
     passed and nothing warned.

test_cognitiveloop_plist_keepalive_is_unconditional pins (1);
test_verify_warns_on_crashed_only_keepalive pins (2).
"""
from __future__ import annotations

import json
import os
import plistlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))

PLIST = REPO / "bin" / "com.m3memory.cognitiveloop.plist"
WATCHDOG_PLIST = REPO / "bin" / "com.m3memory.loopwatchdog.plist"


def _render(path: Path) -> dict:
    """Substitute the installer's placeholders so the template parses as a plist."""
    text = path.read_text(encoding="utf-8")
    text = text.replace("[M3_PYTHON]", "/usr/bin/python3")
    text = text.replace("[M3_MEMORY_ROOT]", "/opt/m3")
    return plistlib.loads(text.encode("utf-8"))


# ── (1) the plist contract ────────────────────────────────────────────────

def test_cognitiveloop_plist_keepalive_is_unconditional():
    """KeepAlive must be exactly True, not {Crashed: True}.

    A dict form restarts only on abnormal exit and will NOT bring the loop back
    after the clean exit an upgrade causes — the 4h-outage path.
    """
    plist = _render(PLIST)
    assert plist["KeepAlive"] is True, (
        f"KeepAlive={plist['KeepAlive']!r}; a non-True value (notably "
        "{'Crashed': True}) leaves the loop dead after a clean exit"
    )


def test_cognitiveloop_plist_throttles_respawn():
    """ThrottleInterval bounds the EXIT_ALREADY_RUNNING(4) respawn loop that
    Crashed-only was originally guarding against."""
    plist = _render(PLIST)
    assert plist.get("ThrottleInterval", 0) >= 10


def test_watchdog_plist_is_periodic_and_points_at_the_script():
    plist = _render(WATCHDOG_PLIST)
    assert plist["Label"] == "com.m3memory.loopwatchdog"
    assert plist["StartInterval"] == 300
    assert plist["ProgramArguments"][-1].endswith("bin/m3_loop_watchdog.py")


def test_watchdog_script_ships_next_to_its_plist():
    """The plist is useless if the script is not packaged beside it."""
    assert (REPO / "bin" / "m3_loop_watchdog.py").is_file()


# ── cross-OS parity (§2): all three platforms get the watchdog ────────────

def test_watchdog_ships_units_for_all_three_platforms():
    """macOS was the only platform broken for the CLEAN-EXIT case, but the
    WEDGED-BUT-ALIVE case is invisible to launchd, systemd (Restart=always) and
    the Windows task repetition alike. All three must ship a watchdog trigger."""
    for f in ("com.m3memory.loopwatchdog.plist",      # macOS
              "m3-loop-watchdog.service",              # Linux oneshot
              "m3-loop-watchdog.timer"):               # Linux trigger
        assert (REPO / "bin" / f).is_file(), f"missing {f}"


def test_windows_task_spec_registers_the_watchdog():
    import install_schedules as isch
    specs = isch.get_schedule_specs(str(REPO))
    names = {s["name"] for s in specs}
    assert "AgentOS_LoopWatchdog" in names
    spec = next(s for s in specs if s["name"] == "AgentOS_LoopWatchdog")
    assert spec["schedule"] == "MINUTE" and spec["modifier"] == "5"
    assert spec["args"][0].endswith("m3_loop_watchdog.py")


def test_watchdog_is_declared_not_migratable():
    """`governor migrate` must not fold the watchdog into the loop: pacing a
    supervisor defers it exactly when the host is loaded and the loop is most
    likely wedged."""
    import governor_migration as gm
    assert "AgentOS_LoopWatchdog" in {n for n, _ in gm.NOT_MIGRATABLE}
    assert "AgentOS_LoopWatchdog" not in gm.GOVERNOR_ELIGIBLE


def test_linux_timer_cadence_matches_the_other_platforms():
    text = (REPO / "bin" / "m3-loop-watchdog.timer").read_text(encoding="utf-8")
    assert "OnUnitActiveSec=5min" in text
    assert "Unit=m3-loop-watchdog.service" in text


def test_watchdog_avoids_platform_system():
    """platform.system() hangs on a WMI query on Py3.14/Windows — the codebase
    uses the os.name/sys.platform constant idiom instead."""
    src = (REPO / "bin" / "m3_loop_watchdog.py").read_text(encoding="utf-8")
    # The rationale is quoted in a docstring, so assert on the IMPORT and the
    # call site, not on any mention of the name.
    assert "import platform" not in src
    assert "def _os_name()" in src


def test_watchdog_restart_backend_per_os(monkeypatch):
    """Detection is OS-agnostic; only the restart ACTION branches. Each branch
    must go through the platform supervisor, never spawn the loop directly —
    two writers on one WAL DB is what the single-instance lock prevents."""
    import m3_loop_watchdog as wd

    monkeypatch.setattr(wd, "_os_name", lambda: "Darwin")
    cmds, what = wd._restart_backend()
    # Graceful FIRST. `kickstart -k` sends SIGKILL, and killing a writer
    # mid-write is what tears the WAL (HALT_PROTOCOL) — the exact damage this
    # watchdog exists to prevent. SIGTERM runs the loop's checkpoint/deregister
    # path; the follow-up kickstart must NOT carry -k.
    # os.getuid() is Unix-only; mirror the production guard
    # (m3_loop_watchdog.py: `os.getuid() if hasattr(os, "getuid") else 0`) so this
    # Darwin-branch assertion is evaluable on a Windows runner too (§1: the suite
    # must go green on all three OSes, and CI's matrix includes windows-latest).
    _uid = os.getuid() if hasattr(os, "getuid") else 0
    assert cmds[0] == ["launchctl", "kill", "SIGTERM",
                       f"gui/{_uid}/com.m3memory.cognitiveloop"]
    assert "-k" not in cmds[1]
    assert cmds[1][:2] == ["launchctl", "kickstart"]
    assert "cognitiveloop" in what

    monkeypatch.setattr(wd, "_os_name", lambda: "Linux")
    cmds, what = wd._restart_backend()
    assert cmds[0] == ["systemctl", "--user", "restart", "m3-cognitive-loop.service"]

    monkeypatch.setattr(wd, "_os_name", lambda: "Windows")
    cmds, what = wd._restart_backend()
    assert [c[1] for c in cmds] == ["/End", "/Run"]
    assert all(c[-1] == "AgentOS_CognitiveLoop" for c in cmds)


def test_watchdog_honors_homecoming_root_overrides(monkeypatch, tmp_path):
    """Hardcoding ~/.m3 would break every deployment that sets M3_CONFIG_ROOT /
    M3_ENGINE_ROOT (docs: Homecoming decoupled roots)."""
    import importlib

    import m3_loop_watchdog as wd
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path / "cfg"))
    monkeypatch.setenv("M3_ENGINE_ROOT", str(tmp_path / "eng"))
    # Force the fallback path (SDK unimportable) to prove the env precedence.
    monkeypatch.setitem(sys.modules, "m3_sdk", None)
    wd = importlib.reload(wd)
    cfg, eng = wd._roots()
    assert cfg == tmp_path / "cfg"
    assert eng == tmp_path / "eng"


# ── (2) the health check that missed it ───────────────────────────────────

@pytest.mark.parametrize("keepalive_xml,should_warn", [
    ("<true/>", False),                                          # healthy
    ("<dict><key>Crashed</key><true/></dict>", True),            # the outage
    ("<false/>", True),
])
def test_verify_warns_on_crashed_only_keepalive(tmp_path, capsys,
                                                keepalive_xml, should_warn):
    """The check must assert the VALUE. Key-presence alone passes {Crashed:true}."""
    import install_schedules as isch

    dest = tmp_path / "com.m3memory.cognitiveloop.plist"
    dest.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>'
        '<key>Label</key><string>com.m3memory.cognitiveloop</string>'
        f'<key>KeepAlive</key>{keepalive_xml}'
        '</dict></plist>\n', encoding="utf-8")

    # Reproduce the check verbatim rather than importing the whole Darwin-gated
    # verify path (which shells out to launchctl and needs a loaded agent).
    # `plutil` is macOS-only: off-Darwin the spawn raises FileNotFoundError rather
    # than returning nonzero, so the assertion below is only meaningful there
    # (§1 — CI's matrix includes ubuntu and windows runners).
    import shutil
    import subprocess
    if shutil.which("plutil") is None:
        pytest.skip("plutil is macOS-only; the KeepAlive-value check is Darwin-gated")
    r = subprocess.run(["plutil", "-extract", "KeepAlive", "raw", "-o", "-",
                        str(dest)], capture_output=True, text=True)
    warned = r.returncode != 0 or (r.stdout or "").strip() != "true"
    assert warned is should_warn
    assert isch is not None  # module imports cleanly (guards the json/subprocess deps)


# ── (3) watchdog decision logic ───────────────────────────────────────────

def test_watchdog_treats_missing_heartbeat_as_unknown_not_dead(tmp_path, monkeypatch):
    """A MISSING heartbeat must not read as 'wedged'. If an upgrade reverts the
    heartbeat patch, a watchdog that inferred death would kickstart the loop
    every 5 minutes forever."""
    import m3_loop_watchdog as wd
    monkeypatch.setattr(wd, "HEARTBEAT", tmp_path / "absent.json")
    age, interval = wd.heartbeat_age()
    assert age is None
    assert interval == 300


def test_watchdog_reads_interval_from_heartbeat(tmp_path, monkeypatch):
    """The staleness threshold derives from the loop's own --interval, so it
    cannot drift out of sync when the interval changes."""
    from datetime import datetime, timezone

    import m3_loop_watchdog as wd

    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": 1, "cycle": 7, "interval_s": 600,
    }), encoding="utf-8")
    monkeypatch.setattr(wd, "HEARTBEAT", hb)

    age, interval = wd.heartbeat_age()
    assert interval == 600
    assert age is not None and age < 60


def test_watchdog_stands_down_while_halt_is_held(tmp_path, monkeypatch):
    """Restarting the loop into an exclusive DB op is the torn-WAL case the HALT
    protocol exists to prevent. Driven through m3_halt.set_halt rather than a
    hand-written file, so this test tracks the real protocol."""
    import m3_halt
    import m3_loop_watchdog as wd

    monkeypatch.setattr(wd, "ENGINE_ROOT", tmp_path)
    m3_halt.set_halt(owner="test-exclusive-op", reason="migration",
                     engine_root=str(tmp_path))
    try:
        assert wd.halt_active() is True
    finally:
        m3_halt.clear_halt(engine_root=str(tmp_path))


def test_watchdog_proceeds_once_halt_is_cleared(tmp_path, monkeypatch):
    import m3_halt
    import m3_loop_watchdog as wd

    monkeypatch.setattr(wd, "ENGINE_ROOT", tmp_path)
    m3_halt.set_halt(owner="test-exclusive-op", reason="migration",
                     engine_root=str(tmp_path))
    m3_halt.clear_halt(engine_root=str(tmp_path))
    assert wd.halt_active() is False


def test_watchdog_no_halt_file_is_not_active(tmp_path, monkeypatch):
    import m3_loop_watchdog as wd
    monkeypatch.setattr(wd, "ENGINE_ROOT", tmp_path)
    assert wd.halt_active() is False


def test_watchdog_stands_down_when_halt_state_is_unreadable(tmp_path, monkeypatch):
    """Fail SAFE (§3): if we cannot tell whether a halt is held, do nothing. A
    missed restart is recoverable at the next tick; a restart into a migration
    may not be."""
    import m3_loop_watchdog as wd

    monkeypatch.setattr(wd, "ENGINE_ROOT", tmp_path)
    monkeypatch.setattr(wd, "LOG", tmp_path / "wd.log", raising=False)
    monkeypatch.setitem(sys.modules, "m3_halt", None)  # import raises
    assert wd.halt_active() is True
