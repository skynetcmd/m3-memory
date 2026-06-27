"""Tests for governor_migration — detect + remove + privileged-command logic.

Subprocess calls (schtasks / crontab) are mocked so the tests are deterministic
and never touch the real host scheduler.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import governor_migration as gm  # noqa: E402


def test_eligible_and_not_migratable_are_disjoint():
    eligible = set(gm.GOVERNOR_ELIGIBLE)
    not_mig = {n for n, _ in gm.NOT_MIGRATABLE}
    assert eligible.isdisjoint(not_mig)
    # The two known non-migratable tasks must be classified as such.
    assert "AgentOS_SecretRotator" in not_mig
    assert "AgentOS_CognitiveLoop" in not_mig


def test_privileged_commands_windows(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")
    cmds = gm.privileged_removal_commands(["AgentOS_HourlySync", "AgentOS_Maintenance"])
    assert cmds == [
        'schtasks /Delete /TN "AgentOS_HourlySync" /F',
        'schtasks /Delete /TN "AgentOS_Maintenance" /F',
    ]


def test_privileged_commands_linux_uses_crontab(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Linux")
    cmds = gm.privileged_removal_commands(["AgentOS_HourlySync"])
    joined = "\n".join(cmds)
    assert "crontab -e" in joined
    # CRITICAL cross-OS correctness: the HourlySync cron line invokes the
    # `pg_sync.sh` wrapper (NOT sync_all.py, which only appears in the Windows
    # task action). Matching sync_all.py would miss the Unix cron entry.
    assert "pg_sync.sh" in joined
    assert "sync_all.py" not in joined
    assert "sudo" in joined  # system-crontab hint present


def test_privileged_commands_macos_uses_crontab(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Darwin")
    cmds = gm.privileged_removal_commands(["AgentOS_Maintenance"])
    joined = "\n".join(cmds)
    assert "crontab -e" in joined
    assert "memory_maintenance.py" in joined


def test_hourlysync_marker_is_pg_sync_sh():
    # Guard against regression: the Unix detection marker must match the actual
    # crontab.template line, which uses pg_sync.sh.
    assert gm._UNIX_CRON_MARKERS["AgentOS_HourlySync"] == "pg_sync.sh"
    # Cognitive loop is NOT a cron marker (it's a service).
    assert "AgentOS_CognitiveLoop" not in gm._UNIX_CRON_MARKERS


def test_cognitive_loop_detected_as_service(monkeypatch, tmp_path):
    # On Linux the cognitive loop is a systemd unit; detection must find it by
    # service-file presence, not crontab.
    monkeypatch.setattr(gm, "_os_name", lambda: "Linux")
    svc = tmp_path / "m3-cognitive-loop.service"
    svc.write_text("[Unit]\n")
    monkeypatch.setattr(
        gm, "_unix_service_paths",
        lambda: {"AgentOS_CognitiveLoop": str(svc)},
    )

    class _R:
        returncode = 0
        stdout = ""  # empty crontab

    monkeypatch.setattr(gm.subprocess, "run", lambda *a, **k: _R())
    out = gm.detect_scheduled_tasks()
    assert "AgentOS_CognitiveLoop" in out["not_migratable_present"]
    assert out["eligible"] == []


def test_privileged_commands_empty_for_no_tasks():
    assert gm.privileged_removal_commands([]) == []


def test_detect_windows(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")

    class _R:
        def __init__(self, rc, out):
            self.returncode = rc
            self.stdout = out

    def fake_run(cmd, **kw):
        # cmd = ["schtasks","/Query","/TN", name]
        name = cmd[-1]
        present = {"AgentOS_HourlySync", "AgentOS_SecretRotator"}
        return _R(0, name) if name in present else _R(1, "")

    monkeypatch.setattr(gm.subprocess, "run", fake_run)
    out = gm.detect_scheduled_tasks()
    assert out["eligible"] == ["AgentOS_HourlySync"]
    assert out["not_migratable_present"] == ["AgentOS_SecretRotator"]


def test_remove_windows_partial_failure(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")

    class _R:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = ""
            self.stderr = ""

    def fake_run(cmd, **kw):
        # Succeed for HourlySync, fail (privilege) for Maintenance.
        name = cmd[cmd.index("/TN") + 1]
        return _R(0 if name == "AgentOS_HourlySync" else 1)

    monkeypatch.setattr(gm.subprocess, "run", fake_run)
    removed, failed = gm.try_remove_scheduled_tasks(["AgentOS_HourlySync", "AgentOS_Maintenance"])
    assert removed == ["AgentOS_HourlySync"]
    assert failed == ["AgentOS_Maintenance"]


def test_remove_empty_is_noop():
    assert gm.try_remove_scheduled_tasks([]) == ([], [])


def test_detect_never_raises_without_scheduler(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Linux")

    def boom(*a, **k):
        raise FileNotFoundError("crontab")

    monkeypatch.setattr(gm.subprocess, "run", boom)
    out = gm.detect_scheduled_tasks()
    assert out == {"eligible": [], "not_migratable_present": []}


def test_not_migratable_lines_have_reasons():
    lines = gm.not_migratable_lines()
    assert len(lines) == len(gm.NOT_MIGRATABLE)
    assert all("—" in line for line in lines)  # name — reason format
