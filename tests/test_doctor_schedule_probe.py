"""Tests for the doctor dangling-scheduled-task probe (bin/doctor/schedule_probe.py).

The schtasks query and on-disk path checks are mocked so the tests are
deterministic across OSes and never touch the real host scheduler or filesystem.
The probe is report-only: every path must return rc 0 and never raise.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from doctor import schedule_probe  # noqa: E402

_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _task_xml(interpreter: str, script: str, extra_args: str = '"--flag"') -> str:
    """Minimal but faithful AgentOS-style task XML (matches the real namespace,
    <Exec><Command>/<Arguments> shape, and quoted-arg convention)."""
    args = f'"{script}" {extra_args}'.strip()
    return (
        f'<?xml version="1.0" encoding="UTF-16"?>'
        f'<Task version="1.2" xmlns="{_NS}">'
        f'<Actions Context="Author"><Exec>'
        f'<Command>{interpreter}</Command>'
        f'<Arguments>{args}</Arguments>'
        f'</Exec></Actions></Task>'
    )


# ---------------------------------------------------------------------------
# _parse_action_paths
# ---------------------------------------------------------------------------

def test_parse_action_paths_extracts_interpreter_and_script():
    xml = _task_xml(r"C:\venv\pythonw.exe", r"C:\repo\bin\loop.py")
    interpreter, script = schedule_probe._parse_action_paths(xml)
    assert interpreter == r"C:\venv\pythonw.exe"
    assert script == r"C:\repo\bin\loop.py"  # surrounding quotes stripped


def test_parse_action_paths_strips_quoted_interpreter():
    # schtasks registers some tasks (e.g. AgentOS_SecretRotator) with a QUOTED
    # <Command>. The quotes must be stripped or os.path.exists() false-flags it.
    xml = (
        f'<Task version="1.2" xmlns="{_NS}"><Actions><Exec>'
        f'<Command>"C:\\venv\\pythonw.exe"</Command>'
        f'<Arguments>"C:\\repo\\bin\\rotate.py"</Arguments>'
        f'</Exec></Actions></Task>'
    )
    interpreter, script = schedule_probe._parse_action_paths(xml)
    assert interpreter == r"C:\venv\pythonw.exe"
    assert script == r"C:\repo\bin\rotate.py"


def test_parse_action_paths_no_exec_returns_none():
    xml = f'<?xml version="1.0"?><Task xmlns="{_NS}"><Actions/></Task>'
    assert schedule_probe._parse_action_paths(xml) == (None, None)


def test_parse_action_paths_no_arguments_gives_none_script():
    xml = (
        f'<Task version="1.2" xmlns="{_NS}"><Actions><Exec>'
        f'<Command>C:\\venv\\pythonw.exe</Command></Exec></Actions></Task>'
    )
    interpreter, script = schedule_probe._parse_action_paths(xml)
    assert interpreter == r"C:\venv\pythonw.exe"
    assert script is None


# ---------------------------------------------------------------------------
# find_dangling
# ---------------------------------------------------------------------------

def _stub_names(monkeypatch, names):
    monkeypatch.setattr(schedule_probe, "_expected_task_names", lambda root: names)


def test_find_dangling_healthy_when_all_paths_exist(monkeypatch):
    _stub_names(monkeypatch, ["AgentOS_CognitiveLoop"])
    monkeypatch.setattr(
        schedule_probe, "_query_task_xml",
        lambda name: _task_xml(r"C:\venv\pythonw.exe", r"C:\repo\bin\loop.py"),
    )
    monkeypatch.setattr(schedule_probe.os.path, "exists", lambda p: True)
    assert schedule_probe.find_dangling("root") == []


def test_find_dangling_flags_missing_interpreter(monkeypatch):
    _stub_names(monkeypatch, ["AgentOS_CognitiveLoop"])
    interp = r"C:\deleted-pipx\pythonw.exe"
    script = r"C:\repo\bin\loop.py"
    monkeypatch.setattr(schedule_probe, "_query_task_xml",
                        lambda name: _task_xml(interp, script))
    # Only the script exists; the interpreter was wiped with the venv.
    monkeypatch.setattr(schedule_probe.os.path, "exists", lambda p: p == script)

    result = schedule_probe.find_dangling("root")
    assert len(result) == 1
    d = result[0]
    assert d["name"] == "AgentOS_CognitiveLoop"
    assert d["missing"] == ["interpreter"]
    assert d["interpreter"] == interp


def test_find_dangling_flags_missing_script(monkeypatch):
    _stub_names(monkeypatch, ["AgentOS_HourlySync"])
    interp = r"C:\venv\pythonw.exe"
    script = r"C:\moved-repo\bin\sync_all.py"
    monkeypatch.setattr(schedule_probe, "_query_task_xml",
                        lambda name: _task_xml(interp, script))
    monkeypatch.setattr(schedule_probe.os.path, "exists", lambda p: p == interp)

    result = schedule_probe.find_dangling("root")
    assert result[0]["missing"] == ["script"]


def test_find_dangling_flags_both_missing(monkeypatch):
    _stub_names(monkeypatch, ["AgentOS_CognitiveLoop"])
    monkeypatch.setattr(schedule_probe, "_query_task_xml",
                        lambda name: _task_xml(r"C:\gone\pythonw.exe", r"C:\gone\loop.py"))
    monkeypatch.setattr(schedule_probe.os.path, "exists", lambda p: False)
    assert schedule_probe.find_dangling("root")[0]["missing"] == ["interpreter", "script"]


def test_find_dangling_skips_not_installed_tasks(monkeypatch):
    # A task that isn't installed (query returns None) is NOT dangling.
    _stub_names(monkeypatch, ["AgentOS_CognitiveLoop", "AgentOS_HourlySync"])
    monkeypatch.setattr(
        schedule_probe, "_query_task_xml",
        lambda name: None if name == "AgentOS_HourlySync"
        else _task_xml(r"C:\venv\pythonw.exe", r"C:\repo\bin\loop.py"),
    )
    monkeypatch.setattr(schedule_probe.os.path, "exists", lambda p: True)
    # HourlySync absent -> skipped; CognitiveLoop healthy -> nothing dangling.
    assert schedule_probe.find_dangling("root") == []


def test_find_dangling_query_error_is_recorded_not_raised(monkeypatch):
    _stub_names(monkeypatch, ["AgentOS_CognitiveLoop"])

    def _boom(name):
        raise RuntimeError("schtasks blew up")

    monkeypatch.setattr(schedule_probe, "_query_task_xml", _boom)
    result = schedule_probe.find_dangling("root")
    assert result[0]["missing"] == ["query-error"]
    assert "schtasks blew up" in result[0]["error"]


# ---------------------------------------------------------------------------
# run()  — always rc 0, report-only
# ---------------------------------------------------------------------------

def test_run_non_windows_is_na(monkeypatch, capsys):
    monkeypatch.setattr(schedule_probe.sys, "platform", "linux")
    assert schedule_probe.run() == 0
    assert "n/a" in capsys.readouterr().out.lower()


def test_run_ok_when_nothing_dangling(monkeypatch, capsys):
    monkeypatch.setattr(schedule_probe.sys, "platform", "win32")
    monkeypatch.setattr(schedule_probe, "find_dangling", lambda root: [])
    assert schedule_probe.run() == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "NAG" not in out


def test_run_nags_with_fix_command(monkeypatch, capsys):
    monkeypatch.setattr(schedule_probe.sys, "platform", "win32")
    monkeypatch.setattr(
        schedule_probe, "find_dangling",
        lambda root: [{"name": "AgentOS_CognitiveLoop",
                       "interpreter": r"C:\gone\pythonw.exe",
                       "script": r"C:\repo\bin\loop.py",
                       "missing": ["interpreter"]}],
    )
    rc = schedule_probe.run()
    out = capsys.readouterr().out
    assert rc == 0  # report-only, never fails the doctor run
    assert "NAG" in out
    assert "AgentOS_CognitiveLoop" in out
    assert "m3 setup" in out               # the fix command


def test_run_brief_dangling_one_liner(monkeypatch, capsys):
    monkeypatch.setattr(schedule_probe.sys, "platform", "win32")
    monkeypatch.setattr(
        schedule_probe, "find_dangling",
        lambda root: [{"name": "AgentOS_CognitiveLoop", "interpreter": None,
                       "script": None, "missing": ["interpreter"]}],
    )
    assert schedule_probe.run(brief=True) == 0
    out = capsys.readouterr().out
    assert "1 dangling" in out
    assert "m3 setup" in out


def test_run_never_raises_when_find_dangling_throws(monkeypatch, capsys):
    monkeypatch.setattr(schedule_probe.sys, "platform", "win32")

    def _boom(root):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(schedule_probe, "find_dangling", _boom)
    assert schedule_probe.run() == 0  # swallowed, reported
    assert "could not run schedule probe" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# integration: names come from install_schedules (single-sourced)
# ---------------------------------------------------------------------------

def test_expected_task_names_sourced_from_install_schedules():
    names = schedule_probe._expected_task_names(os.path.expanduser("~/.m3"))
    # Sanity: the continuous governor loop and a periodic task must be present,
    # proving we read the real spec rather than a hardcoded list.
    assert "AgentOS_CognitiveLoop" in names
    assert any(n.startswith("AgentOS_") for n in names)
