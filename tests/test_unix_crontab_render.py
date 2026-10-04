"""The managed cron block runs m3's real interpreter, and an upgrade refreshes it.

Hazard: the template named `<install root>/.venv/bin/python3`, which a pipx or
pip install never has, so every Python cron job pointed at a missing file; and
nothing an upgrade runs rewrote the block.
"""
from __future__ import annotations

import os
import sys

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import install_schedules as sched  # noqa: E402

_ROOT = os.path.normpath(os.path.join(_BIN, ".."))


class _Res:
    def __init__(self, rc=0, out=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def _capture_install(monkeypatch, tmp_path, current=""):
    written = {}

    def fake_run(cmd, **_kw):
        if cmd[:2] == ["crontab", "-l"]:
            return _Res(0, current)
        if cmd[0] == "crontab":
            with open(cmd[1], encoding="utf-8") as f:
                written["cron"] = f.read()
        return _Res()

    monkeypatch.setattr(sched, "_run", fake_run)
    monkeypatch.setattr(sched, "_venv_python", lambda root, **k: "/venv/bin/python")
    monkeypatch.setattr(sched, "_logs_dir", lambda: str(tmp_path / "logs"))
    monkeypatch.setenv("M3_ENGINE_ROOT", str(tmp_path / "engine"))
    return written


def test_cron_block_uses_the_resolved_interpreter(monkeypatch, tmp_path):
    written = _capture_install(monkeypatch, tmp_path)
    sched.install_unix_crontab(_ROOT)
    cron = written["cron"]
    assert ".venv/bin/python3" not in cron
    assert "[M3_" not in cron, "a placeholder was left unrendered"
    assert "/venv/bin/python " in cron


def test_refresh_rewrites_only_an_existing_managed_block(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(sched, "install_unix_crontab", lambda root: calls.append(root))

    monkeypatch.setattr(sched, "_run", lambda *a, **k: _Res(0, "0 * * * * user-job\n"))
    sched._refresh_managed_crontab(_ROOT)
    assert calls == []

    monkeypatch.setattr(sched, "_run", lambda *a, **k: _Res(
        0, "# >>> m3-managed (do not edit this block) >>>\n# <<< m3-managed <<<\n"))
    sched._refresh_managed_crontab(_ROOT)
    assert calls == [_ROOT]
