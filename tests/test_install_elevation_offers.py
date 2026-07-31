"""Every privileged install step must OFFER inline UAC, not print a banner.

Windows has no inline sudo, so the installer elevates via PowerShell
``Start-Process -Verb RunAs`` — one consent dialog beats a context-switch into a
separate admin shell. The machinery shipped in v2026.7.18.1, but one call site
(the dashboard boot task) never used it: it captured the child's output,
discarded it, ignored the return code, and swallowed exceptions with a bare
``pass``. A denied registration was therefore invisible, and the user learned at
the next reboot that the dashboard never came back.

These tests pin the CONTRACT, not the wiring:
  1. a denied task registration offers elevation,
  2. the child's diagnostics reach the user,
  3. a missed UAC dialog is re-offered rather than silently downgraded,
  4. headless runs never block on a dialog nobody can approve,
  5. non-Windows hosts are unaffected (systemd --user / LaunchAgents need no
     privilege, so they must never see a UAC prompt).
"""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from m3_memory import installer, setup_wizard

DENIED = "[FAIL] Failed to create task AgentOS_Dashboard: ERROR: Access is denied."


def _proc(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# --- the dashboard boot task ------------------------------------------------


def test_denied_dashboard_task_offers_elevation(capsys):
    """The regression: a denied registration must reach the elevation offer."""
    with patch.object(installer, "_install_schedules_script", return_value="sched.py"), \
         patch.object(installer, "_python_exe", return_value="py"), \
         patch.object(installer.subprocess, "run", return_value=_proc(1, DENIED)), \
         patch.object(setup_wizard, "_offer_elevated_schedule_repair",
                      return_value=True) as offer:
        installer._register_dashboard_task(non_interactive=False)

    assert offer.called, "denied boot task did not offer inline UAC"
    assert "Access is denied" in capsys.readouterr().out, \
        "child diagnostics were swallowed instead of shown"


def test_successful_registration_does_not_prompt():
    """A task that registers fine must never pop a UAC dialog."""
    with patch.object(installer, "_install_schedules_script", return_value="sched.py"), \
         patch.object(installer, "_python_exe", return_value="py"), \
         patch.object(installer.subprocess, "run", return_value=_proc(0, "[OK] created")), \
         patch.object(setup_wizard, "_offer_elevated_schedule_repair") as offer:
        installer._register_dashboard_task()

    assert not offer.called, "prompted for elevation on a successful registration"


def test_non_privilege_failure_does_not_prompt(capsys):
    """A failure that is not about privilege gets reported, not elevated."""
    with patch.object(installer, "_install_schedules_script", return_value="sched.py"), \
         patch.object(installer, "_python_exe", return_value="py"), \
         patch.object(installer.subprocess, "run",
                      return_value=_proc(1, "[FAIL] schtasks not found")), \
         patch.object(setup_wizard, "_offer_elevated_schedule_repair") as offer:
        installer._register_dashboard_task()

    assert not offer.called, "elevation offered for a non-privilege failure"
    assert "schtasks not found" in capsys.readouterr().out


def test_declined_elevation_names_the_recovery_command(capsys):
    """Declining must leave the user a command, not a dead end."""
    with patch.object(installer, "_install_schedules_script", return_value="sched.py"), \
         patch.object(installer, "_python_exe", return_value="py"), \
         patch.object(installer.subprocess, "run", return_value=_proc(1, DENIED)), \
         patch.object(setup_wizard, "_offer_elevated_schedule_repair",
                      return_value=False):
        installer._register_dashboard_task(non_interactive=False)

    out = capsys.readouterr().out
    assert "m3 schedules repair" in out
    assert "will not start after" in out.lower()


def test_exception_is_reported_not_swallowed(capsys):
    """The original bare `except: pass` is what made this invisible."""
    with patch.object(installer, "_install_schedules_script", return_value="sched.py"), \
         patch.object(installer, "_python_exe", return_value="py"), \
         patch.object(installer.subprocess, "run", side_effect=OSError("boom")):
        installer._register_dashboard_task()

    assert "boom" in capsys.readouterr().out, "exception silently swallowed"


def test_headless_run_never_blocks_on_a_dialog():
    """No one can approve UAC on a headless box — don't hang waiting."""
    with patch.object(setup_wizard.sys, "platform", "win32"), \
         patch.object(setup_wizard, "_ask_yes_no") as ask:
        assert setup_wizard._offer_elevated_schedule_repair(
            "sched.py", non_interactive=True) is False
    assert not ask.called, "headless run prompted for consent"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only path")
def test_posix_never_offers_uac():
    """Linux/macOS register user-level units — no privilege, so no prompt."""
    assert setup_wizard._offer_elevated_schedule_repair(
        "sched.py", non_interactive=False) is False


# --- the retry loop ---------------------------------------------------------


def test_missed_uac_dialog_is_retried():
    """A UAC dialog is easy to miss; one miss must not permanently downgrade."""
    attempts = {"n": 0}

    def _flaky() -> bool:
        attempts["n"] += 1
        return attempts["n"] >= 3  # succeeds on the third consent

    with patch.object(setup_wizard, "_prompt", return_value="retry"):
        assert setup_wizard._retry_elevated(_flaky, what="test") is True
    assert attempts["n"] == 3


def test_user_can_stop_retrying():
    """Retry, but never trap: an explicit decline stops the loop."""
    calls = {"n": 0}

    def _always_fail() -> bool:
        calls["n"] += 1
        return False

    with patch.object(setup_wizard, "_prompt", return_value="quit"):
        assert setup_wizard._retry_elevated(_always_fail, what="test") is False
    assert calls["n"] == 1, "kept retrying after the user declined"


def test_retry_gives_up_rather_than_looping_forever():
    with patch.object(setup_wizard, "_prompt", return_value="retry"):
        assert setup_wizard._retry_elevated(
            lambda: False, what="test", max_attempts=3) is False


def test_eof_stops_retrying():
    """No stdin (piped/CI) must end the loop, not spin on it."""
    with patch.object(setup_wizard, "_prompt", return_value=None):
        assert setup_wizard._retry_elevated(lambda: False, what="test") is False


def test_stuck_writer_elevation_retries():
    """_kill_stuck_writers used a bare one-shot elevate() — a missed dialog
    abandoned the writer and setup carried on over a live DB lock."""
    writer = MagicMock(pid=4321, role="dashboard")
    attempts = {"n": 0}

    def _flaky_elevate(_pid) -> bool:
        attempts["n"] += 1
        return attempts["n"] >= 2

    with patch.object(setup_wizard.sys, "platform", "win32"), \
         patch.object(setup_wizard, "_kill_process_windows", return_value=False), \
         patch.object(setup_wizard, "_runas_kill_windows", _flaky_elevate), \
         patch.object(setup_wizard, "_prompt", return_value="retry"):
        assert setup_wizard._kill_stuck_writers([writer], allow_sudo=True) is True
    assert attempts["n"] == 2, "a missed UAC dialog was not re-offered"
