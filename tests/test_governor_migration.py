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


# ── Windows legacy-action detection (hardening A) ──────────────────────────────
# A pre-bf110222 / hand-named task (e.g. `m3-memory-sync`) runs sync_all.py but
# is NOT named AgentOS_HourlySync, so the name-only query misses it. Detection
# must catch it by its ACTION and surface it under its REAL name for removal.

def _verbose_list(records: list[tuple[str, str]]) -> str:
    """Render a fake `schtasks /Query /FO LIST /V` blob from (TaskName, action)."""
    blocks = []
    for name, action in records:
        blocks.append(
            f"HostName:                             HOSTPC\n"
            f"TaskName:                             {name}\n"
            f"Task To Run:                          {action}\n"
        )
    return "\n".join(blocks)


def _win_run_factory(verbose_blob: str, name_present: set[str]):
    """Build a fake subprocess.run that answers BOTH query shapes:
    - /TN <name>            → present iff name in name_present
    - /FO LIST /V           → returns the verbose blob
    """
    class _R:
        def __init__(self, rc, out):
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    def fake_run(cmd, **kw):
        if "/V" in cmd:
            return _R(0, verbose_blob)
        name = cmd[-1]  # /TN <name>
        return _R(0, name) if name in name_present else _R(1, "")

    return fake_run


def test_windows_detects_legacy_sync_task_by_action(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")
    # No canonical task installed by name; a legacy task runs sync_all.py.
    blob = _verbose_list([
        (r"\m3-memory-sync",
         r'powershell.exe -Command "& ...\.venv\Scripts\python.exe ...\bin\sync_all.py"'),
        (r"\Microsoft\Windows\SomethingElse", r"C:\Windows\system32\noop.exe"),
    ])
    monkeypatch.setattr(gm.subprocess, "run", _win_run_factory(blob, set()))
    out = gm.detect_scheduled_tasks()
    # Surfaced under its REAL name so `schtasks /Delete /TN m3-memory-sync` works.
    assert "m3-memory-sync" in out["eligible"]
    assert out["not_migratable_present"] == []


def test_windows_action_scan_skips_canonical_names(monkeypatch):
    # A task NAMED AgentOS_HourlySync running sync_all.py must NOT be double-listed
    # by the action scan — it's already found by the name-based query.
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")
    blob = _verbose_list([
        (r"\AgentOS_HourlySync", r'"...\pythonw.exe" "...\bin\sync_all.py"'),
    ])
    monkeypatch.setattr(
        gm.subprocess, "run",
        _win_run_factory(blob, {"AgentOS_HourlySync"}),
    )
    out = gm.detect_scheduled_tasks()
    # Exactly one occurrence — found by name, not duplicated by action.
    assert out["eligible"] == ["AgentOS_HourlySync"]


def test_windows_action_marker_is_sync_all_not_pg_sync_sh():
    # CROSS-OS TRAP GUARD: the Windows action invokes sync_all.py directly; the
    # Unix wrapper pg_sync.sh never appears in a Windows action. The two marker
    # maps MUST stay independent or HourlySync legacy tasks go undetected.
    assert gm._WINDOWS_ACTION_MARKERS["AgentOS_HourlySync"] == "sync_all.py"
    assert gm._UNIX_CRON_MARKERS["AgentOS_HourlySync"] == "pg_sync.sh"


def test_windows_legacy_detection_never_raises_without_schtasks(monkeypatch):
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")

    def boom(*a, **k):
        raise FileNotFoundError("schtasks")

    monkeypatch.setattr(gm.subprocess, "run", boom)
    # Whole detect path must swallow it and return empty, not raise.
    assert gm.detect_windows_legacy_action_tasks() == set()
    assert gm.detect_scheduled_tasks() == {"eligible": [], "not_migratable_present": []}


def test_windows_legacy_does_not_reclassify_not_migratable(monkeypatch):
    # A hand-named task whose leaf collides with a NOT_MIGRATABLE name must not be
    # pulled into `eligible` (that would schedule a security task for removal).
    monkeypatch.setattr(gm, "_os_name", lambda: "Windows")
    blob = _verbose_list([
        (r"\AgentOS_SecretRotator", r'"...\pythonw.exe" "...\bin\secret_rotator.py"'),
    ])
    monkeypatch.setattr(
        gm.subprocess, "run",
        _win_run_factory(blob, {"AgentOS_SecretRotator"}),
    )
    out = gm.detect_scheduled_tasks()
    assert "AgentOS_SecretRotator" in out["not_migratable_present"]
    assert "AgentOS_SecretRotator" not in out["eligible"]


def test_leaf_task_name_strips_path():
    assert gm._leaf_task_name(r"\m3-memory-sync") == "m3-memory-sync"
    assert gm._leaf_task_name(r"\Microsoft\Windows\Foo\Bar") == "Bar"
    assert gm._leaf_task_name("PlainName") == "PlainName"
