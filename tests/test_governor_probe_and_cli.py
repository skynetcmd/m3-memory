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


def test_probe_brief_nag_mentions_elevation_on_windows(monkeypatch, capsys):
    """The brief NAG line (what most users see in `m3 doctor`) must state up front
    that migration needs an elevated shell on Windows — without it, the user hits
    'Access is denied' only after running migrate."""
    monkeypatch.setattr(
        gm, "detect_scheduled_tasks",
        lambda: {"eligible": ["AgentOS_HourlySync"], "not_migratable_present": []},
    )
    monkeypatch.setattr(governor_probe.sys, "platform", "win32")
    governor_probe.run(brief=True)
    out = capsys.readouterr().out
    assert "NAG" in out
    assert "ELEVATED" in out


def test_probe_brief_nag_no_elevation_note_on_unix(monkeypatch, capsys):
    """On non-Windows the brief line stays clean (no Windows-only elevation text)."""
    monkeypatch.setattr(
        gm, "detect_scheduled_tasks",
        lambda: {"eligible": ["AgentOS_HourlySync"], "not_migratable_present": []},
    )
    monkeypatch.setattr(governor_probe.sys, "platform", "linux")
    governor_probe.run(brief=True)
    out = capsys.readouterr().out
    assert "NAG" in out
    assert "ELEVATED shell" not in out


def test_migrate_warns_about_elevation_before_prompt_on_windows(monkeypatch, capsys):
    """`m3 governor migrate` must warn about the elevation requirement BEFORE the
    deletes are attempted (headless mode skips the prompt but still prints it)."""
    monkeypatch.setattr(
        gm, "detect_scheduled_tasks",
        lambda: {"eligible": ["AgentOS_Maintenance"], "not_migratable_present": []},
    )
    # simulate a non-elevated Windows session: every remove fails
    monkeypatch.setattr(
        gm, "try_remove_scheduled_tasks",
        lambda names: ([], list(names)),
    )
    monkeypatch.setattr(
        gm, "privileged_removal_commands",
        lambda names: [f'schtasks /Delete /TN "{n}" /F' for n in names],
    )
    monkeypatch.setattr(governor_cli.sys, "platform", "win32")
    rc = governor_cli.cmd_migrate(yes=True)
    out = capsys.readouterr().out
    assert "ELEVATED" in out            # up-front notice
    assert "needs an elevated shell" in out  # per-failure hint (not a vague guess)
    assert rc == 1                       # pure permission failure


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
