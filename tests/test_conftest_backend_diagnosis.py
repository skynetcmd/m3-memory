"""The `requires_pg`-on-the-wrong-backend diagnostic must actually say something.

On 2026-09-30 a Windows run produced 23 failures and 1 error across ten
`*_pg_live` files, every message of the form:

    E   sqlite3.OperationalError: near "%": syntax error
    E   sqlite3.OperationalError: no such table: memory_items

None of them mentioned a backend, a DSN, or `M3_DB_BACKEND`, and the same file
passed 3/3 when run alone — so the output pointed at the schema, the SQL and the
test, and never at the one thing that was wrong. `conftest._backend_diagnosis`
exists so that failure explains itself. These tests pin that it fires on the
right shape and stays quiet otherwise, because a diagnostic that cries wolf gets
ignored and one that never fires is dead code.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest as ct  # noqa: E402


class _Marker:
    def __init__(self, name): self.name = name


class _Item:
    """Minimal stand-in for a pytest Item: only `iter_markers` is consulted."""
    def __init__(self, *marker_names): self._m = [_Marker(n) for n in marker_names]
    def iter_markers(self): return list(self._m)


class _ExcInfo:
    def __init__(self, exc): self.value = exc


class _Call:
    def __init__(self, when="call", exc=None):
        self.when = when
        self.excinfo = _ExcInfo(exc) if exc is not None else None


def _sqlite_error():
    return sqlite3.OperationalError('near "%": syntax error')


# ── it fires on the real shape ───────────────────────────────────────────────

def test_a_requires_pg_failure_on_sqlite_is_explained():
    msg = ct._backend_diagnosis(_Item("requires_pg"), _Call(exc=_sqlite_error()))
    assert msg, "the diagnostic did not fire on the exact shape it exists for"
    # The three things the original output never said.
    assert "SQLite" in msg
    assert "M3_DB_BACKEND" in msg
    assert "_reset_for_tests" in msg, "must name the mechanism that fixes it"
    # And it must be actionable, not just descriptive.
    assert "run this file ALONE" in msg, "must say what to DO next"
    for label in ("observed:", "cause:", "inspect:"):
        assert label in msg, f"missing {label!r} — the error/log idiom"


def test_it_reports_the_resolved_backend(monkeypatch):
    """The decisive field: what the seam actually resolves to."""
    sel = sys.modules.get("memory.backends.selector")
    if sel is None:
        pytest.skip("selector not imported in this session; nothing to read")
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    msg = ct._backend_diagnosis(_Item("requires_pg"), _Call(exc=_sqlite_error()))
    assert "'sqlite'" in msg and "NOT postgres" in msg


def test_it_echoes_the_env_var_including_the_unset_case(monkeypatch):
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    monkeypatch.delenv("DB_BACKEND", raising=False)
    msg = ct._backend_diagnosis(_Item("requires_pg"), _Call(exc=_sqlite_error()))
    assert "default sqlite" in msg, (
        "an unset backend is the common cause and must be shown as such, not blank"
    )


# ── and stays quiet otherwise ────────────────────────────────────────────────

def test_silent_for_tests_that_are_not_requires_pg():
    assert ct._backend_diagnosis(_Item(), _Call(exc=_sqlite_error())) is None
    assert ct._backend_diagnosis(_Item("requires_native"), _Call(exc=_sqlite_error())) is None


def test_silent_when_the_test_passed():
    assert ct._backend_diagnosis(_Item("requires_pg"), _Call(exc=None)) is None


def test_silent_outside_the_call_phase():
    """Setup/teardown failures have their own causes; claiming a backend problem
    there would be noise."""
    assert ct._backend_diagnosis(
        _Item("requires_pg"), _Call(when="setup", exc=_sqlite_error())) is None


def test_a_non_sqlite_failure_is_not_blamed_on_the_backend():
    """A real PG-side assertion must not be mislabelled a wrong-backend run."""
    msg = ct._backend_diagnosis(_Item("requires_pg"), _Call(exc=AssertionError("boom")))
    assert msg is not None, "context is still useful"
    assert "while executing against SQLite" not in msg
    assert "run this file ALONE" not in msg, (
        "do not prescribe the wrong-backend remedy for an ordinary assertion"
    )


# ── the annotator must never break a test ────────────────────────────────────

def test_the_annotator_swallows_its_own_errors():
    """A diagnostic that can fail a test is worse than no diagnostic."""
    class _Boom:
        def get_result(self): raise RuntimeError("report unavailable")
    ct._annotate_backend_mismatch(_Item("requires_pg"), _Call(exc=_sqlite_error()), _Boom())


def test_the_annotator_attaches_a_named_section():
    class _Report:
        def __init__(self): self.sections = []
    class _Outcome:
        def __init__(self, r): self._r = r
        def get_result(self): return self._r
    rep = _Report()
    ct._annotate_backend_mismatch(_Item("requires_pg"), _Call(exc=_sqlite_error()), _Outcome(rep))
    assert len(rep.sections) == 1
    title, body = rep.sections[0]
    assert "backend" in title.lower()
    assert "M3_DB_BACKEND" in body
