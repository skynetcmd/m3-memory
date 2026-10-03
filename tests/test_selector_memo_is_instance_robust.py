"""A reset on a held selector instance must reach the memo the process reads.

`resolve_backend_name()` memoizes in a module global. A fixture that does
`from memory.backends import selector as _selector; _selector._reset_for_tests()`
can be holding an EARLIER instance of that module -- the test namespace purge
discards it while the fixture keeps its reference. The reset then clears a cache
the code under test never reads, so `M3_DB_BACKEND=postgres` resolves as
`sqlite` and every PG-marked test runs against SQLite while reporting as a PG
failure. Observed as the same 23 failures on the Windows PostgreSQL lane,
discriminated by the skip count (94 with, 91 without).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

SRC = Path(__file__).resolve().parents[1] / "bin" / "memory" / "backends" / "selector.py"


def _second_instance():
    """A genuinely separate selector instance, with its own globals and memo."""
    spec = importlib.util.spec_from_file_location("memory.backends.selector", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_memo():
    from memory.backends import selector

    selector._reset_for_tests()
    yield
    selector._reset_for_tests()


def test_the_two_instances_really_are_separate():
    """Or the tests below prove nothing."""
    from memory.backends import selector as held

    live = _second_instance()
    assert live.__dict__ is not held.__dict__


def test_a_reset_on_the_held_instance_clears_the_live_memo(monkeypatch):
    from memory.backends import selector as held

    live = _second_instance()
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "memory.backends.selector", live)
        m.setenv("M3_DB_BACKEND", "sqlite")
        assert held.resolve_backend_name() == "sqlite"

        # The fixture pattern: flip the env, reset via the HELD reference.
        m.setenv("M3_DB_BACKEND", "postgres")
        held._reset_for_tests()
        assert held.resolve_backend_name() == "postgres", (
            "the held reset did not reach the memo the process resolves through"
        )
        assert live._resolved_name == "postgres"


def test_resolution_through_a_held_instance_agrees_with_the_live_one(monkeypatch):
    from memory.backends import selector as held

    live = _second_instance()
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "memory.backends.selector", live)
        m.setenv("M3_DB_BACKEND", "postgres")
        assert held.resolve_backend_name() == live.resolve_backend_name() == "postgres"


def test_a_bad_value_still_raises_rather_than_defaulting(monkeypatch):
    """The delegation must not swallow the typo guard."""
    from memory.backends import selector as held

    with monkeypatch.context() as m:
        m.setenv("M3_DB_BACKEND", "postgre")
        held._reset_for_tests()
        with pytest.raises(ValueError, match="not recognized"):
            held.resolve_backend_name()
