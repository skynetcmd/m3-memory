"""Tests for the doctor oxidation probe (bin/doctor/oxidation_probe.py).

The probe is report-only: it must ALWAYS return 0 (a pure-Python or stale-wheel
deployment is supported, not a failure) and must distinguish three states —
not-installed, installed-and-current, installed-but-stale — in its output.

Capture note: these tests use contextlib.redirect_stdout rather than pytest's
capsys. In the full suite, earlier tests (and the llama.cpp native lib) perform
fd-level stdout manipulation that can leave capsys seeing nothing — capturing
the probe's print() directly into a StringIO is immune to that ordering effect.
The probe reads `config` via `from memory import config` *inside* run(), so we
patch attributes on that exact module object (imported the same way here) to
guarantee the patch and the probe see the same `config`.
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

_BIN = str(Path(__file__).resolve().parent.parent / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from doctor import oxidation_probe  # noqa: E402

from memory import config  # noqa: E402


def _set_rs(monkeypatch, value, disabled=False):
    monkeypatch.setattr(config, "m3_core_rs", value, raising=False)
    monkeypatch.setattr(config, "_OXIDATION_DISABLED", disabled, raising=False)


def _run_capture() -> tuple[int, str]:
    """Run the probe, returning (exit_code, stdout). Ordering-proof capture."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = oxidation_probe.run()
    return rc, buf.getvalue()


def test_returns_zero_when_not_installed(monkeypatch):
    _set_rs(monkeypatch, None)
    rc, out = _run_capture()
    assert rc == 0
    assert "not installed" in out


def test_returns_zero_when_disabled(monkeypatch):
    _set_rs(monkeypatch, object(), disabled=True)
    rc, out = _run_capture()
    assert rc == 0
    assert "disabled" in out


def test_reports_current_when_all_present(monkeypatch):
    # A fake extension exposing every expected function.
    fake = SimpleNamespace(__version__="9.9.9")
    for name, _why in oxidation_probe._EXPECTED:
        setattr(fake, name, lambda *a, **k: None)
    _set_rs(monkeypatch, fake)

    rc, out = _run_capture()
    assert rc == 0
    assert "current" in out
    assert "STALE" not in out


def test_reports_stale_when_functions_missing(monkeypatch):
    # Expose all but the two FTS functions — the real stale-wheel case.
    fake = SimpleNamespace(__version__="1.0.0")
    for name, _why in oxidation_probe._EXPECTED:
        if name in ("sanitize_fts", "compile_fts_query"):
            continue
        setattr(fake, name, lambda *a, **k: None)
    _set_rs(monkeypatch, fake)

    rc, out = _run_capture()
    assert rc == 0  # report-only: never fails the run
    assert "STALE" in out
    assert "sanitize_fts" in out
    assert "compile_fts_query" in out


def test_reports_version_stale_when_functions_present_but_old(monkeypatch):
    """The new capability: all expected functions present, but the wheel version
    is BEHIND the target — still flagged STALE (a current function set doesn't
    prove a current build)."""
    from m3_memory.rust_core_install import M3_CORE_RS_VERSION  # noqa: F401
    fake = SimpleNamespace(__version__="0.0.1")  # far below any real target
    for name, _why in oxidation_probe._EXPECTED:
        setattr(fake, name, lambda *a, **k: None)
    _set_rs(monkeypatch, fake)

    rc, out = _run_capture()
    assert rc == 0
    assert "STALE" in out
    assert "behind the target" in out or "< expected" in out


def test_unknown_version_is_not_falsely_stale(monkeypatch):
    """A wheel that exposes every function but reports no version must NOT be
    declared version-stale (we can't prove it's old) — only noted."""
    fake = SimpleNamespace()  # no __version__
    for name, _why in oxidation_probe._EXPECTED:
        setattr(fake, name, lambda *a, **k: None)
    _set_rs(monkeypatch, fake)

    rc, out = _run_capture()
    assert rc == 0
    # All functions present + unknown version → reported current (not STALE),
    # but the "does not report a version" note appears.
    assert "does not report a version" in out
    assert "STALE" not in out


def test_never_raises_on_bad_config(monkeypatch):
    # Even if m3_core_rs is a hostile object, the probe must not crash the doctor.
    class Boom:
        def __getattr__(self, name):
            raise RuntimeError("boom")

    _set_rs(monkeypatch, Boom())
    rc, _out = _run_capture()
    assert rc == 0
