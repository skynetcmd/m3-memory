"""Tests for the doctor governor probe and the `m3 governor` CLI entrypoint.

Detection is mocked so the tests are deterministic across OSes and never touch
the real scheduler.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import governor_cli  # noqa: E402
import governor_migration as gm  # noqa: E402
from doctor import governor_probe  # noqa: E402


def test_probe_ok_when_no_tasks(monkeypatch, capsys):
    monkeypatch.setattr(
        gm, "detect_scheduled_tasks",
        lambda: {"eligible": [], "not_migratable_present": []},
    )
    rc = governor_probe.run()
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out
    assert "NAG" not in out


def test_probe_nags_with_fix_command(monkeypatch, capsys):
    monkeypatch.setattr(
        gm, "detect_scheduled_tasks",
        lambda: {"eligible": ["AgentOS_HourlySync"], "not_migratable_present": []},
    )
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")
    rc = governor_probe.run()
    out = capsys.readouterr().out
    assert rc == 0  # report-only, never fails the doctor run
    assert "NAG" in out
    assert "m3 governor migrate" in out          # the fix command
    assert "AgentOS_HourlySync" in out
    assert 'schtasks /Delete /TN "AgentOS_HourlySync" /F' in out  # privileged cmd


def test_cli_status_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        gm, "detect_scheduled_tasks",
        lambda: {"eligible": ["AgentOS_Maintenance"], "not_migratable_present": []},
    )
    monkeypatch.setattr(gm, "_os_name", lambda: "Linux")
    rc = governor_cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NAG" in out
    assert "memory_maintenance.py" in out  # Linux marker in the privileged cmd


def test_cli_migrate_all_removed_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        gm, "detect_scheduled_tasks",
        lambda: {"eligible": ["AgentOS_Maintenance"], "not_migratable_present": []},
    )
    monkeypatch.setattr(
        gm, "try_remove_scheduled_tasks",
        lambda names: (list(names), []),  # all removed
    )
    rc = governor_cli.main(["migrate", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "removed AgentOS_Maintenance" in out


def test_cli_migrate_all_failed_returns_one(monkeypatch, capsys):
    monkeypatch.setattr(
        gm, "detect_scheduled_tasks",
        lambda: {"eligible": ["AgentOS_Maintenance"], "not_migratable_present": []},
    )
    monkeypatch.setattr(
        gm, "try_remove_scheduled_tasks",
        lambda names: ([], list(names)),  # all failed (no privilege)
    )
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")
    rc = governor_cli.main(["migrate", "--yes"])
    out = capsys.readouterr().out
    assert rc == 1  # pure permission failure -> nonzero for scripts
    assert "PRIVILEGED" in out
    assert "schtasks /Delete" in out


def test_cli_migrate_nothing_to_do(monkeypatch, capsys):
    monkeypatch.setattr(
        gm, "detect_scheduled_tasks",
        lambda: {"eligible": [], "not_migratable_present": []},
    )
    rc = governor_cli.main(["migrate", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Nothing to migrate" in out
