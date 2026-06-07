"""Tests for the doctor oxidation probe (bin/doctor/oxidation_probe.py).

The probe is report-only: it must ALWAYS return 0 (a pure-Python or stale-wheel
deployment is supported, not a failure) and must distinguish three states —
not-installed, installed-and-current, installed-but-stale — in its output.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_BIN = str(Path(__file__).resolve().parent.parent / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from doctor import oxidation_probe  # noqa: E402

from memory import config  # noqa: E402


def _set_rs(monkeypatch, value):
    monkeypatch.setattr(config, "m3_core_rs", value, raising=False)
    monkeypatch.setattr(config, "_OXIDATION_DISABLED", False, raising=False)


def test_returns_zero_when_not_installed(monkeypatch, capsys):
    _set_rs(monkeypatch, None)
    assert oxidation_probe.run() == 0
    out = capsys.readouterr().out
    assert "not installed" in out


def test_returns_zero_when_disabled(monkeypatch, capsys):
    monkeypatch.setattr(config, "m3_core_rs", object(), raising=False)
    monkeypatch.setattr(config, "_OXIDATION_DISABLED", True, raising=False)
    assert oxidation_probe.run() == 0
    assert "disabled" in capsys.readouterr().out


def test_reports_current_when_all_present(monkeypatch, capsys):
    # A fake extension exposing every expected function.
    fake = SimpleNamespace(__version__="9.9.9")
    for name, _why in oxidation_probe._EXPECTED:
        setattr(fake, name, lambda *a, **k: None)
    _set_rs(monkeypatch, fake)

    assert oxidation_probe.run() == 0
    out = capsys.readouterr().out
    assert "current" in out
    assert "STALE" not in out


def test_reports_stale_when_functions_missing(monkeypatch, capsys):
    # Expose all but the two FTS functions — the real stale-wheel case.
    fake = SimpleNamespace(__version__="1.0.0")
    for name, _why in oxidation_probe._EXPECTED:
        if name in ("sanitize_fts", "compile_fts_query"):
            continue
        setattr(fake, name, lambda *a, **k: None)
    _set_rs(monkeypatch, fake)

    assert oxidation_probe.run() == 0  # report-only: never fails the run
    out = capsys.readouterr().out
    assert "STALE" in out
    assert "sanitize_fts" in out
    assert "compile_fts_query" in out


def test_never_raises_on_bad_config(monkeypatch, capsys):
    # Even if m3_core_rs is a hostile object, the probe must not crash the doctor.
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    _set_rs(monkeypatch, Boom())
    # hasattr swallows the AttributeError path; RuntimeError from __getattr__
    # would propagate through hasattr as False, so the probe still completes.
    assert oxidation_probe.run() == 0
