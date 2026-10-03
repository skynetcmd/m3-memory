"""Scheduled-task and service logs resolve to the logs root, never the payload.

Every log path used to be derived from the install root, so on a pipx install
the logs lived inside site-packages/m3_memory/logs — invisible, and deleted by
the next reinstall.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

from m3_core.paths import get_m3_logs_root  # noqa: E402


def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def test_logs_root_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("M3_LOGS_ROOT", raising=False)
    monkeypatch.delenv("M3_MEMORY_ROOT", raising=False)
    assert _norm(get_m3_logs_root()) == _norm(Path.home() / ".m3" / "logs")

    monkeypatch.setenv("M3_MEMORY_ROOT", str(tmp_path / "master"))
    assert _norm(get_m3_logs_root()) == _norm(tmp_path / "master" / "logs")

    monkeypatch.setenv("M3_LOGS_ROOT", str(tmp_path / "explicit"))
    assert _norm(get_m3_logs_root()) == _norm(tmp_path / "explicit")


def test_schedule_specs_log_outside_the_install_root(monkeypatch, tmp_path):
    import install_schedules

    logs = tmp_path / "logs"
    payload = tmp_path / "payload"
    monkeypatch.setenv("M3_LOGS_ROOT", str(logs))
    specs = install_schedules.get_schedule_specs(str(payload))

    log_args = [s["args"][s["args"].index("--log-file") + 1]
                for s in specs if "--log-file" in s["args"]]
    assert log_args, "precondition: some spec passes --log-file"
    for p in log_args:
        assert _norm(Path(p).parent) == _norm(logs), p
        assert not _norm(p).startswith(_norm(payload)), p


def test_service_templates_use_the_logs_placeholder():
    templates = [p for p in _BIN.iterdir()
                 if p.suffix in (".plist", ".service", ".timer") or p.name == "crontab.template"]
    assert templates, "precondition: templates found"
    offenders = [p.name for p in templates
                 if "[M3_MEMORY_ROOT]/logs" in p.read_text(encoding="utf-8")]
    assert not offenders, f"templates still log under the payload: {offenders}"


def test_render_template_substitutes_the_logs_root(monkeypatch, tmp_path):
    import install_schedules

    monkeypatch.setenv("M3_LOGS_ROOT", str(tmp_path / "logs"))
    tpl = tmp_path / "t.service"
    tpl.write_text("ExecStart=[M3_PYTHON] x --log-file [M3_LOGS_ROOT]/a.log\n", encoding="utf-8")
    out = install_schedules._render_template(str(tpl), "/payload", "/py")
    assert "[M3_LOGS_ROOT]" not in out
    assert str(tmp_path / "logs") in out


def test_task_runtime_default_log_is_under_the_logs_root(monkeypatch, tmp_path):
    import _task_runtime

    monkeypatch.setenv("M3_LOGS_ROOT", str(tmp_path / "logs"))
    monkeypatch.delenv("M3_TASK_LOG_FILE", raising=False)
    resolved = _task_runtime._resolve_log_file(None)
    assert _norm(resolved.parent) == _norm(tmp_path / "logs"), resolved
