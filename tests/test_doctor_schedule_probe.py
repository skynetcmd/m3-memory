"""Tests for the doctor dangling-scheduled-job probe (bin/doctor/schedule_probe.py).

All OS scheduler access (schtasks XML, launchd plist, systemd unit, crontab) is
mocked so the tests are deterministic across OSes and never touch the real host
scheduler or filesystem (DESIGN §3: a test that only passes because a live
service is reachable is not hermetic). The probe is report-only: every path must
return rc 0 and never raise.

Cross-platform coverage (DESIGN §1) — the dangling failure exists on all three
OSes, so each backend is exercised: Windows (schtasks), macOS (launchd plist +
crontab), Linux (systemd unit + crontab).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from doctor import schedule_probe  # noqa: E402

_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


# ===========================================================================
# Windows backend — schtasks XML
# ===========================================================================

def _task_xml(interpreter: str, script: str, extra_args: str = '"--flag"') -> str:
    args = f'"{script}" {extra_args}'.strip()
    return (
        f'<?xml version="1.0" encoding="UTF-16"?>'
        f'<Task version="1.2" xmlns="{_NS}">'
        f'<Actions Context="Author"><Exec>'
        f'<Command>{interpreter}</Command><Arguments>{args}</Arguments>'
        f'</Exec></Actions></Task>'
    )


def test_win_parse_extracts_interpreter_and_script():
    interp, script = schedule_probe._parse_exec_paths(
        _task_xml(r"C:\venv\pythonw.exe", r"C:\repo\bin\loop.py"))
    assert interp == r"C:\venv\pythonw.exe"
    assert script == r"C:\repo\bin\loop.py"


def test_win_parse_strips_quoted_command():
    # AgentOS_SecretRotator registers a QUOTED <Command>; quotes must be stripped
    # or os.path.exists() false-flags it (the live-run bug this test guards).
    xml = (
        f'<Task version="1.2" xmlns="{_NS}"><Actions><Exec>'
        f'<Command>"C:\\venv\\pythonw.exe"</Command>'
        f'<Arguments>"C:\\repo\\bin\\rotate.py"</Arguments>'
        f'</Exec></Actions></Task>'
    )
    interp, script = schedule_probe._parse_exec_paths(xml)
    assert interp == r"C:\venv\pythonw.exe"
    assert script == r"C:\repo\bin\rotate.py"


def test_win_parse_no_exec_returns_none():
    assert schedule_probe._parse_exec_paths(
        f'<Task xmlns="{_NS}"><Actions/></Task>') == (None, None)


def _win_env(monkeypatch, names, xml_for, exists):
    monkeypatch.setattr(schedule_probe.sys, "platform", "win32")
    monkeypatch.setattr(schedule_probe, "_expected_task_names", lambda root: names)
    monkeypatch.setattr(schedule_probe, "_query_task_xml", xml_for)
    monkeypatch.setattr(schedule_probe.os.path, "exists", exists)


def test_win_healthy_when_all_paths_exist(monkeypatch):
    _win_env(monkeypatch, ["AgentOS_CognitiveLoop"],
             lambda n: _task_xml(r"C:\venv\pythonw.exe", r"C:\repo\bin\loop.py"),
             lambda p: True)
    assert schedule_probe.find_dangling("root") == []


def test_win_flags_missing_interpreter(monkeypatch):
    interp, script = r"C:\gone\pythonw.exe", r"C:\repo\bin\loop.py"
    _win_env(monkeypatch, ["AgentOS_CognitiveLoop"],
             lambda n: _task_xml(interp, script), lambda p: p == script)
    d = schedule_probe.find_dangling("root")
    assert d[0]["job"] == "AgentOS_CognitiveLoop"
    assert d[0]["missing"] == ["interpreter"]


def test_win_skips_not_installed(monkeypatch):
    _win_env(monkeypatch, ["AgentOS_CognitiveLoop", "AgentOS_HourlySync"],
             lambda n: None if n == "AgentOS_HourlySync"
             else _task_xml(r"C:\venv\pythonw.exe", r"C:\repo\bin\loop.py"),
             lambda p: True)
    assert schedule_probe.find_dangling("root") == []


def test_win_query_error_recorded_not_raised(monkeypatch):
    def _boom(n):
        raise RuntimeError("schtasks blew up")
    _win_env(monkeypatch, ["AgentOS_CognitiveLoop"], _boom, lambda p: True)
    d = schedule_probe.find_dangling("root")
    assert d[0]["missing"] == ["query-error"]
    assert "schtasks blew up" in d[0]["error"]


# ===========================================================================
# macOS backend — launchd plist
# ===========================================================================

def _write_plist(path, interpreter, script):
    import plistlib
    with open(path, "wb") as f:
        plistlib.dump({
            "Label": "com.m3memory.cognitiveloop",
            "ProgramArguments": [interpreter, script, "--interval", "300"],
        }, f)


def _darwin_env(monkeypatch, plist_path):
    monkeypatch.setattr(schedule_probe.sys, "platform", "darwin")
    monkeypatch.setattr(schedule_probe, "_LAUNCHD_PLIST", plist_path)
    monkeypatch.setattr(schedule_probe, "_read_crontab", lambda: None)  # isolate plist


def test_darwin_healthy(tmp_path, monkeypatch):
    interp = tmp_path / "python3"; interp.write_text("")
    script = tmp_path / "loop.py"; script.write_text("")
    plist = tmp_path / "agent.plist"
    _write_plist(plist, str(interp), str(script))
    _darwin_env(monkeypatch, str(plist))
    assert schedule_probe.find_dangling("root") == []


def test_darwin_flags_missing_interpreter(tmp_path, monkeypatch):
    script = tmp_path / "loop.py"; script.write_text("")
    plist = tmp_path / "agent.plist"
    _write_plist(plist, str(tmp_path / "deleted-venv" / "python3"), str(script))
    _darwin_env(monkeypatch, str(plist))
    d = schedule_probe.find_dangling("root")
    assert d[0]["job"] == "launchd:com.m3memory.cognitiveloop"
    assert d[0]["missing"] == ["interpreter"]


def test_darwin_no_plist_is_not_dangling(tmp_path, monkeypatch):
    _darwin_env(monkeypatch, str(tmp_path / "nonexistent.plist"))
    assert schedule_probe.find_dangling("root") == []


# ===========================================================================
# Linux backend — systemd unit
# ===========================================================================

_UNIT_TMPL = (
    "[Service]\nType=simple\n"
    "ExecStart={interp} {script} --interval 300 --log-file /x/y.log\n"
    "Restart=always\n"
)


def _linux_env(monkeypatch, unit_path):
    monkeypatch.setattr(schedule_probe.sys, "platform", "linux")
    monkeypatch.setattr(schedule_probe, "_SYSTEMD_UNIT", unit_path)
    monkeypatch.setattr(schedule_probe, "_read_crontab", lambda: None)  # isolate unit


def test_linux_parse_execstart_strips_prefix():
    interp, script = schedule_probe._parse_execstart(
        "[Service]\nExecStart=-/opt/venv/bin/python /opt/m3/bin/loop.py --interval 300\n")
    assert interp == "/opt/venv/bin/python"   # leading '-' prefix stripped
    assert script == "/opt/m3/bin/loop.py"


def test_linux_healthy(tmp_path, monkeypatch):
    interp = tmp_path / "python"; interp.write_text("")
    script = tmp_path / "loop.py"; script.write_text("")
    unit = tmp_path / "m3.service"
    unit.write_text(_UNIT_TMPL.format(interp=interp, script=script))
    _linux_env(monkeypatch, str(unit))
    assert schedule_probe.find_dangling("root") == []


def test_linux_flags_missing_script(tmp_path, monkeypatch):
    interp = tmp_path / "python"; interp.write_text("")
    unit = tmp_path / "m3.service"
    unit.write_text(_UNIT_TMPL.format(interp=interp, script=tmp_path / "moved" / "loop.py"))
    _linux_env(monkeypatch, str(unit))
    d = schedule_probe.find_dangling("root")
    assert d[0]["job"] == "systemd:m3-cognitive-loop.service"
    assert d[0]["missing"] == ["script"]


def test_linux_no_unit_is_not_dangling(tmp_path, monkeypatch):
    _linux_env(monkeypatch, str(tmp_path / "nonexistent.service"))
    assert schedule_probe.find_dangling("root") == []


# ===========================================================================
# Shared crontab backend (macOS + Linux)
# ===========================================================================

def test_crontab_flags_missing_m3_python(monkeypatch):
    cron = ("# m3\n"
            "*/30 * * * * /opt/m3/.venv/bin/python /opt/m3/bin/sweep.py --batch 256\n"
            "0 9 * * * /usr/bin/backup.sh\n")  # unrelated line — must be ignored
    monkeypatch.setattr(schedule_probe, "_read_crontab", lambda: cron)
    monkeypatch.setattr(schedule_probe.os.path, "exists", lambda p: False)
    d = schedule_probe._dangling_crontab()
    assert len(d) == 1                       # only the m3 line, not backup.sh
    assert d[0]["job"] == "crontab:line-2"


def test_crontab_ignores_unrelated_jobs(monkeypatch):
    monkeypatch.setattr(schedule_probe, "_read_crontab",
                        lambda: "0 9 * * * /usr/bin/rsync -a /a /b\n")
    monkeypatch.setattr(schedule_probe.os.path, "exists", lambda p: False)
    assert schedule_probe._dangling_crontab() == []


def test_crontab_healthy_when_paths_exist(monkeypatch):
    monkeypatch.setattr(schedule_probe, "_read_crontab",
                        lambda: "0 3 * * * /opt/m3/.venv/bin/python /opt/m3/bin/maint.py\n")
    monkeypatch.setattr(schedule_probe.os.path, "exists", lambda p: True)
    assert schedule_probe._dangling_crontab() == []


def test_crontab_none_when_no_crontab(monkeypatch):
    monkeypatch.setattr(schedule_probe, "_read_crontab", lambda: None)
    assert schedule_probe._dangling_crontab() == []


# ===========================================================================
# run() — always rc 0, report-only, on every OS
# ===========================================================================

def test_run_ok_when_nothing_dangling(monkeypatch, capsys):
    monkeypatch.setattr(schedule_probe, "find_dangling", lambda root: [])
    assert schedule_probe.run() == 0
    out = capsys.readouterr().out
    assert "OK" in out and "NAG" not in out


def test_run_nags_with_fix_command(monkeypatch, capsys):
    monkeypatch.setattr(
        schedule_probe, "find_dangling",
        lambda root: [{"job": "systemd:m3-cognitive-loop.service",
                       "interpreter": "/gone/python", "script": "/repo/bin/loop.py",
                       "missing": ["interpreter"]}],
    )
    assert schedule_probe.run() == 0  # report-only, never fails the doctor run
    out = capsys.readouterr().out
    assert "NAG" in out
    assert "systemd:m3-cognitive-loop.service" in out
    assert "m3 setup" in out


def test_run_brief_dangling_one_liner(monkeypatch, capsys):
    monkeypatch.setattr(
        schedule_probe, "find_dangling",
        lambda root: [{"job": "launchd:com.m3memory.cognitiveloop", "interpreter": None,
                       "script": None, "missing": ["interpreter"]}],
    )
    assert schedule_probe.run(brief=True) == 0
    out = capsys.readouterr().out
    assert "1 dangling" in out and "m3 setup" in out


def test_run_never_raises_when_find_dangling_throws(monkeypatch, capsys):
    def _boom(root):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(schedule_probe, "find_dangling", _boom)
    assert schedule_probe.run() == 0  # swallowed, reported
    assert "could not run schedule probe" in capsys.readouterr().out


def test_run_reports_backend_label(monkeypatch, capsys):
    monkeypatch.setattr(schedule_probe.sys, "platform", "linux")
    monkeypatch.setattr(schedule_probe, "find_dangling", lambda root: [])
    schedule_probe.run()
    assert "systemd" in capsys.readouterr().out


# ===========================================================================
# integration: Windows task names single-sourced from install_schedules
# ===========================================================================

def test_expected_task_names_sourced_from_install_schedules():
    names = schedule_probe._expected_task_names(os.path.expanduser("~/.m3"))
    assert "AgentOS_CognitiveLoop" in names
    assert any(n.startswith("AgentOS_") for n in names)
