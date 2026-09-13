"""`_start_longlived_tasks` must start services on all three OSes.

It was hardcoded to `schtasks /Run`, so `m3 setup` on Linux reached "Verifying
background services are running", called this, and raised FileNotFoundError
into a bare `except` -- the cognitive loop stayed down behind a warning.
Reported from a real Linux install 2026-09-12.

THE TRAP these tests exist to hold shut: _SELF_HEAL_TASKS is keyed by the
WINDOWS task name, so the obvious fix -- swap `schtasks` for
`_restart_command(name)` -- produces

    systemctl --user start AgentOS_CognitiveLoop

and the exit code of `systemctl --user start` does not reliably report a
missing unit -- it varies by systemd version. Both measured 2026-09-12, same
command, same missing unit::

    WSL Ubuntu 24.04 (systemd 255)  ->  exit 0   <- reports SUCCESS
    Debian 13        (systemd 257)  ->  exit 5

On the older release that prints "Started" over a service never touched:
strictly worse than the FileNotFoundError, because the crash was at least loud.
The name must be translated through _ROLE_TO_SERVICE.

Verified on a real Debian 13 / systemd 257 guest (claude-dev), not only on
mocks: 0 schtasks calls, 0 Windows names leaked, and a genuinely absent
`m3-cognitive-loop.service` reported loudly rather than as "Started".
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import install_schedules as isch  # noqa: E402


@pytest.fixture
def spy(monkeypatch):
    """Capture the commands that would run, and never actually run one."""
    calls = []

    class _Res:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return _Res()

    monkeypatch.setattr(isch.subprocess, "run", fake_run)
    monkeypatch.setattr(isch, "_confirm_service_live", lambda *a, **k: None)
    monkeypatch.setattr(isch, "_safe_print", lambda *a, **k: None)
    # _service_exists probes systemd; the unit is "present" unless a test says
    # otherwise, so these tests measure NAME RESOLUTION, not the probe.
    monkeypatch.setattr(isch, "_service_exists", lambda name: True)
    return calls


@pytest.mark.parametrize(
    "platform,launcher,expected_unit",
    [
        ("win", "schtasks", "AgentOS_CognitiveLoop"),
        ("darwin", "launchctl", "com.m3memory.cognitiveloop"),
        ("linux", "systemctl", "m3-cognitive-loop.service"),
    ],
)
def test_uses_the_right_launcher_and_the_right_name(
    platform, launcher, expected_unit, spy, monkeypatch
):
    monkeypatch.setattr(isch, "_platform_key", lambda: platform)
    isch._start_longlived_tasks([{"name": "AgentOS_CognitiveLoop"}])

    assert spy, f"no command issued on {platform} -- the service stays DOWN"
    cmd = spy[0]
    assert cmd[0] == launcher, f"on {platform} expected {launcher}, got {cmd}"
    assert expected_unit in cmd, (
        f"on {platform} the command carries {cmd[-1]!r}; it must be translated "
        f"through _ROLE_TO_SERVICE to {expected_unit!r}. Passing the Windows "
        f"task name to systemd exits 0 and starts NOTHING"
    )


def test_windows_task_name_never_reaches_a_unix_launcher(spy, monkeypatch):
    """The regression stated directly: no Unix command may carry 'AgentOS_'."""
    for platform in ("darwin", "linux"):
        spy.clear()
        monkeypatch.setattr(isch, "_platform_key", lambda p=platform: p)
        for task in isch._SELF_HEAL_TASKS:
            isch._start_longlived_tasks([{"name": task}])
        for cmd in spy:
            assert not any("AgentOS_" in part for part in cmd), (
                f"on {platform} a Windows task name leaked into {cmd}"
            )


def test_a_platform_without_the_service_is_skipped_not_started(spy, monkeypatch):
    """embed-server self-manages on Linux (_ROLE_TO_SERVICE linux=None). It must
    be skipped -- starting `None` or falling back to the task name would run a
    command against a unit that does not exist."""
    monkeypatch.setattr(isch, "_platform_key", lambda: "linux")
    isch._start_longlived_tasks([{"name": "AgentOS_EmbedServer"}])
    assert spy == [], f"expected no command for a self-managed service, got {spy}"


def test_an_unmappable_task_is_reported_not_silently_skipped(monkeypatch):
    """'we were asked to start something and cannot name it' must be loud --
    distinct from the legitimate skip above, which is not an error."""
    said = []
    monkeypatch.setattr(isch, "_safe_print", lambda m, *a, **k: said.append(str(m)))
    monkeypatch.setattr(isch, "_platform_key", lambda: "linux")
    monkeypatch.setattr(isch, "_SELF_HEAL_TASKS", {"AgentOS_Unmapped": "PT5M"})

    isch._start_longlived_tasks([{"name": "AgentOS_Unmapped"}])

    assert any("_ROLE_TO_SERVICE" in m for m in said), (
        f"an unmappable task was not reported loudly; output was {said}"
    )


def test_service_exists_has_one_owner():
    """§10a: the systemd exit-0 discrimination was inlined in
    restart_reaped_services. Two copies drift."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(isch.__file__).read_text(encoding="utf-8"))
    defs = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_service_exists"]
    assert len(defs) == 1, f"_service_exists must have ONE definition; got {defs}"

    src = pathlib.Path(isch.__file__).read_text(encoding="utf-8")
    assert src.count('"LoadState"') <= 1, (
        "the LoadState probe appears more than once -- it belongs only in "
        "_service_exists"
    )
