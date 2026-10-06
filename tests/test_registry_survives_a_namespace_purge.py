"""A held registry reference still resolves when it is not the live instance.

`@register_backend` resolves `register_backend` through
`sys.modules["memory.backends.registry"]`, so registration lands in whichever
registry module is live. Code holding an EARLIER instance — anything that
captured a registry function before that module was replaced — saw an empty
`_REGISTRY` and raised "no dialect registered" for a backend that is shipped,
allow-listed and importable.

Deliberately does NOT purge the `memory.*` namespace. m3 memory 25df967b
records that `memory.*` sys.modules manipulation leaks across tests (13
unrelated failures in 2026-07, 141 on the Windows PostgreSQL lane in 2026-10,
all as `'module' object is not callable` from the shadowed `dialect` symbol).
The package object is never replaced here; only the single
`memory.backends.registry` key is swapped, inside a monkeypatch context.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _second_instance() -> ModuleType:
    """A genuinely separate `memory.backends.registry` instance.

    Copying a module's `__dict__` is NOT enough: the copied functions keep
    `__globals__` pointing at the original module, so `@register_backend` would
    still write into the original `_REGISTRY` and the test could not fail.
    Loading the file afresh gives the new module its own globals and its own
    registry, which is the condition this is about.
    """
    import importlib.util

    src = Path(__file__).resolve().parents[1] / "bin" / "memory" / "backends" / "registry.py"
    spec = importlib.util.spec_from_file_location("memory.backends.registry", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_a_held_registry_adopts_a_live_registration(backend, monkeypatch):
    from memory.backends import registry as held

    held._ensure_registered(backend)
    assert backend in held._REGISTRY, "precondition: the backend is registered"

    with monkeypatch.context() as m:
        # The held module is no longer the live one; the live one has the entry.
        live = _second_instance()
        m.setattr(held, "_REGISTRY", {}, raising=False)
        m.setitem(sys.modules, "memory.backends.registry", live)

        assert held._REGISTRY == {}, "precondition: the held registry is empty"
        assert live._REGISTRY is not held._REGISTRY, "not a separate instance"
        # Not object identity: re-running @register_backend builds an equal
        # entry. What matters is that the HELD registry ends up able to answer.
        dialect = held.dialect_singleton_for(backend)
        assert dialect.backend == backend, dialect
        assert held._REGISTRY.get(backend) is not None, "nothing was adopted"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_backend_factory_for_shares_the_adoption(backend, monkeypatch):
    """`backend_factory_for` uses the same `_ensure_registered`."""
    from memory.backends import registry as held

    held._ensure_registered(backend)
    assert backend in held._REGISTRY, "precondition: the backend is registered"

    with monkeypatch.context() as m:
        live = _second_instance()
        m.setattr(held, "_REGISTRY", {}, raising=False)
        m.setitem(sys.modules, "memory.backends.registry", live)
        assert callable(held.backend_factory_for(backend))


def test_the_dialect_accessor_is_untouched_by_all_of_this():
    """The package object is never replaced, so the accessor stays callable."""
    import memory.backends as B

    assert callable(B.dialect), type(B.dialect).__name__
