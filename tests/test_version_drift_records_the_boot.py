"""The drift check must record the boot, including when the prior PID is dead.

`_pid_is_alive()` listed `subprocess.TimeoutExpired` in its `except` tuple while
importing `subprocess` only inside the Windows branch. On Unix the first dead
PID made evaluating that tuple raise `UnboundLocalError`, which propagated out
of `check_and_record()` before it could save state. The caller logs that at
DEBUG, so the symptom was silent: a boot record frozen since 2026-07-06 and a
"version drift detected ... kill PID <long-dead>" warning on every single boot.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import version_drift  # noqa: E402


def test_a_dead_pid_reports_not_alive_rather_than_raising():
    # 2**22 is above the default pid_max on both Linux and macOS, so it cannot
    # be a live process; the point is that this returns rather than raises.
    assert version_drift._pid_is_alive(2 ** 22) is False


def test_our_own_pid_is_alive():
    """Or the probe could return False unconditionally and still pass above."""
    import os

    assert version_drift._pid_is_alive(os.getpid()) is True


def test_a_nonpositive_pid_is_not_alive():
    assert version_drift._pid_is_alive(0) is False
    assert version_drift._pid_is_alive(-1) is False


def test_check_and_record_persists_the_boot(tmp_path, monkeypatch):
    """The whole point of the check: the next boot must see this one."""
    monkeypatch.setattr(version_drift, "_state_dir", lambda: tmp_path)

    # A prior boot by a PID that is now dead -- the condition that used to throw.
    (tmp_path / version_drift.STATE_FILENAME).write_text(
        '{"last_boot_at": "2026-07-06T17:03:23Z", "last_boot_pid": 4194304,'
        ' "last_boot_version": "2026.7.5.0"}'
    )

    findings = version_drift.check_and_record()
    assert findings["prior_version"] == "2026.7.5.0"
    assert findings["prior_pid_alive"] is False

    import json

    saved = json.loads((tmp_path / version_drift.STATE_FILENAME).read_text())
    assert saved["last_boot_version"] == findings["current_version"], (
        "the boot was not recorded, so the drift warning will repeat forever"
    )
    import os as _os

    assert saved["last_boot_pid"] == _os.getpid()
