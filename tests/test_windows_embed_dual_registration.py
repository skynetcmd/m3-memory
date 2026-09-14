"""Windows must not register AgentOS_EmbedServer beside the Rust SCM service.

Both bind :8082 and are MUTUALLY EXCLUSIVE. The Unix installer already refuses
(EmbedServerConflict), but _rust_embed_service_loaded() returned None on
Windows and the guard only ran from install_unix_embed_server() — so Windows,
the one platform where the collision is actually live, had no guard at all.

Measured on the host that motivated this, 2026-09-13:
    Get-Service m3-embed-server           -> Running / Automatic
    Get-ScheduledTask AgentOS_EmbedServer -> Ready
Both armed, racing for one port.

Hermetic (§3): sc.exe is mocked at subprocess.run. No SCM, no Task Scheduler.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_BIN = _ROOT / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import install_schedules as sched  # noqa: E402

TASK = "AgentOS_EmbedServer"


class _SC:
    """Stand-in for `sc.exe query <name>`."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_REGISTERED = (
    "SERVICE_NAME: m3-embed-server\n"
    "        TYPE               : 10  WIN32_OWN_PROCESS\n"
    "        STATE              : 4  RUNNING\n"
)
_NOT_REGISTERED = (
    "[SC] EnumQueryServicesStatus:OpenService FAILED 1060:\n\n"
    "The specified service does not exist as an installed service.\n"
)


def _win(monkeypatch, result):
    monkeypatch.setattr(sched, "_os_name", lambda: "Windows")
    if isinstance(result, Exception):
        def _boom(*a, **k):
            raise result
        monkeypatch.setattr(sched.subprocess, "run", _boom)
    else:
        monkeypatch.setattr(sched.subprocess, "run", lambda *a, **k: result)


# ── detection ─────────────────────────────────────────────────────────────────
def test_detects_registered_rust_service(monkeypatch):
    _win(monkeypatch, _SC(0, _REGISTERED))
    assert sched._rust_embed_service_loaded() is True


def test_detects_absent_service_via_1060(monkeypatch):
    """1060 = ERROR_SERVICE_DOES_NOT_EXIST — the ONLY definite negative."""
    _win(monkeypatch, _SC(1060, "", _NOT_REGISTERED))
    assert sched._rust_embed_service_loaded() is False


def test_registered_but_stopped_still_counts(monkeypatch):
    """SCM owns the port the moment it starts the service. A stopped-but-
    registered service is still a supervisor — this is the divergence the
    Darwin docstring records (label loaded, port free)."""
    stopped = _REGISTERED.replace("4  RUNNING", "1  STOPPED")
    _win(monkeypatch, _SC(0, stopped))
    assert sched._rust_embed_service_loaded() is True


@pytest.mark.parametrize("result", [
    _SC(5, "", "Access is denied."),
    _SC(1, "", "The service control manager is unavailable"),
    OSError("sc.exe not found"),
])
def test_indeterminate_is_none_not_false(monkeypatch, result):
    """§3: returning False on an error path installs a second supervisor on
    EVERY error. Indeterminate must be None so the caller refuses."""
    _win(monkeypatch, result)
    assert sched._rust_embed_service_loaded() is None


# ── the filter that acts on it ────────────────────────────────────────────────
def _tasks():
    return [
        {"name": "AgentOS_CognitiveLoop"},
        {"name": TASK},
        {"name": "AgentOS_Dashboard"},
    ]


def test_drops_embed_task_when_rust_registered(monkeypatch, capsys):
    _win(monkeypatch, _SC(0, _REGISTERED))
    kept = sched._drop_embed_task_if_rust_owns_port(_tasks())
    names = [t["name"] for t in kept]
    assert TASK not in names, "registered a second :8082 supervisor"
    out = capsys.readouterr().out
    assert "m3-embed-server" in out and "8082" in out, "dropped it silently"


def test_keeps_every_other_task(monkeypatch):
    """Windows DROPS the one conflicting task rather than refusing the whole
    install — aborting would also cost the loop, dashboard and watchdog."""
    _win(monkeypatch, _SC(0, _REGISTERED))
    kept = [t["name"] for t in sched._drop_embed_task_if_rust_owns_port(_tasks())]
    assert "AgentOS_CognitiveLoop" in kept
    assert "AgentOS_Dashboard" in kept
    assert len(kept) == 2


def test_installs_embed_task_when_rust_absent(monkeypatch):
    """The guard must not be a blanket refusal: with no Rust service the
    Python task IS the keep-alive and must still be registered."""
    _win(monkeypatch, _SC(1060, "", _NOT_REGISTERED))
    kept = [t["name"] for t in sched._drop_embed_task_if_rust_owns_port(_tasks())]
    assert TASK in kept, "dropped the only available keep-alive"
    assert len(kept) == 3


def test_indeterminate_drops_the_task(monkeypatch, capsys):
    _win(monkeypatch, _SC(5, "", "Access is denied."))
    kept = [t["name"] for t in sched._drop_embed_task_if_rust_owns_port(_tasks())]
    assert TASK not in kept, "registered a POSSIBLE second supervisor"
    assert "sc.exe query" in capsys.readouterr().out, "no knob named to inspect"


def test_noop_when_embed_task_not_in_selection(monkeypatch):
    """A selector like --only dashboard must not trigger an sc.exe query."""
    calls = []
    monkeypatch.setattr(sched, "_os_name", lambda: "Windows")
    monkeypatch.setattr(sched.subprocess, "run",
                        lambda *a, **k: calls.append(a) or _SC(0, _REGISTERED))
    tasks = [{"name": "AgentOS_Dashboard"}]
    assert sched._drop_embed_task_if_rust_owns_port(tasks) == tasks
    assert calls == [], "queried SCM when no embed task was selected"


def test_filter_is_wired_into_the_installer():
    """A guard nobody calls is not a guard (§12c)."""
    src = (_BIN / "install_schedules.py").read_text(encoding="utf-8")
    idx = src.find("def install_windows_tasks(")
    assert idx != -1
    body = src[idx:idx + 4000]
    assert "_drop_embed_task_if_rust_owns_port" in body, \
        "install_windows_tasks does not call the guard"
