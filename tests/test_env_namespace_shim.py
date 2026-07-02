"""Tests for the M3_* env-var namespacing back-compat shim.

getenv_compat(new, old, default) lets m3-specific config vars move under the
M3_ namespace without breaking existing configs: the new name wins, the old
(deprecated) name still works with a one-time warning, and every deprecated
name actually read is recorded so `m3 doctor` can report migration debt.

Also guards the real user-facing win: a documented M3_-prefixed knob
(M3_TITLE_MATCH_BOOST) now takes effect, where before the code read only the
bare name and silently ignored the documented one.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import m3_core.paths as paths  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_shim_state():
    paths._DEPRECATED_ENV_SEEN.clear()
    paths._DEPRECATED_ENV_WARNED.clear()
    yield
    paths._DEPRECATED_ENV_SEEN.clear()
    paths._DEPRECATED_ENV_WARNED.clear()


def test_new_name_wins(monkeypatch):
    monkeypatch.setenv("M3_FOO", "new")
    monkeypatch.setenv("FOO", "old")
    assert paths.getenv_compat("M3_FOO", "FOO", "def") == "new"
    # new name in use is NOT deprecated
    assert "FOO" not in paths.deprecated_env_in_use()


def test_old_name_falls_back_and_is_recorded(monkeypatch):
    monkeypatch.delenv("M3_FOO", raising=False)
    monkeypatch.setenv("FOO", "old")
    assert paths.getenv_compat("M3_FOO", "FOO", "def") == "old"
    assert paths.deprecated_env_in_use() == {"FOO": "M3_FOO"}


def test_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("M3_FOO", raising=False)
    monkeypatch.delenv("FOO", raising=False)
    assert paths.getenv_compat("M3_FOO", "FOO", "def") == "def"
    assert paths.getenv_compat("M3_FOO", "FOO") is None
    assert not paths.deprecated_env_in_use()


def test_deprecation_warned_once(monkeypatch, caplog):
    import logging
    monkeypatch.delenv("M3_FOO", raising=False)
    monkeypatch.setenv("FOO", "old")
    with caplog.at_level(logging.WARNING, logger="M3_SDK"):
        paths.getenv_compat("M3_FOO", "FOO")
        paths.getenv_compat("M3_FOO", "FOO")
        paths.getenv_compat("M3_FOO", "FOO")
    warnings = [r for r in caplog.records if "FOO" in r.getMessage()]
    assert len(warnings) == 1, "deprecation must warn exactly once per name"


def test_documented_m3_knob_now_takes_effect(monkeypatch):
    """Regression for the real symptom: M3_TITLE_MATCH_BOOST is the documented
    name but the code used to read only bare TITLE_MATCH_BOOST, silently
    ignoring the documented one. The new name must now win."""
    monkeypatch.setenv("M3_TITLE_MATCH_BOOST", "0.99")
    monkeypatch.delenv("TITLE_MATCH_BOOST", raising=False)
    from memory import config as mc
    importlib.reload(mc)
    assert mc.TITLE_MATCH_BOOST == 0.99
    # old name still honored for back-compat
    monkeypatch.delenv("M3_TITLE_MATCH_BOOST", raising=False)
    monkeypatch.setenv("TITLE_MATCH_BOOST", "0.77")
    importlib.reload(mc)
    assert mc.TITLE_MATCH_BOOST == 0.77
