"""pip is bootstrapped into a venv that lacks it before m3 installs anything.

Hazard: pipx 1.17+ builds app venvs without pip, so `python -m pip` fails and
the native core, its embed-server binary and the dashboard extras never install.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from m3_memory import _pip  # noqa: E402
from m3_memory import rust_core_install as rci  # noqa: E402


class _R:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(_pip, "_READY", {})


def test_pip_present_is_used_directly(monkeypatch):
    calls = []
    monkeypatch.setattr(_pip.subprocess, "run", lambda cmd, **k: calls.append(cmd) or _R(0))
    assert _pip.pip_command("/venv/python") == ["/venv/python", "-m", "pip"]
    assert not any("ensurepip" in c for c in calls)


def test_missing_pip_is_bootstrapped_with_ensurepip(monkeypatch):
    state = {"pip": False}
    calls = []

    def fake(cmd, **k):
        calls.append(cmd)
        if "ensurepip" in cmd:
            state["pip"] = True
            return _R(0)
        return _R(0 if state["pip"] else 1)

    monkeypatch.setattr(_pip.subprocess, "run", fake)
    assert _pip.pip_command("/venv/python") == ["/venv/python", "-m", "pip"]
    assert ["/venv/python", "-m", "ensurepip", "--default-pip"] in calls


def test_unbootstrappable_pip_raises_with_the_fix(monkeypatch):
    monkeypatch.setattr(_pip.subprocess, "run", lambda cmd, **k: _R(1, err="no ensurepip"))
    with pytest.raises(_pip.PipUnavailable, match="ensurepip --default-pip"):
        _pip.pip_command("/venv/python")


def test_rust_core_install_reports_missing_pip_not_a_missing_wheel(monkeypatch, capsys):
    def boom():
        raise _pip.PipUnavailable("pip is not available in this environment (x)")

    monkeypatch.setattr(_pip, "pip_command", boom)
    monkeypatch.setattr(rci, "install_log", lambda *a, **k: None)
    monkeypatch.setattr(rci, "install_from_github_release",
                        lambda *a, **k: pytest.fail("no install attempt without pip"))
    assert rci.install_rust_core(os_tok="macos", force=True) == 1
    err = capsys.readouterr().err
    assert "pip is not available" in err
    assert "no prebuilt wheel" not in err.lower()
