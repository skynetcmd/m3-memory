"""A held registry reference keeps working after the `memory.*` namespace is purged.

`tests/conftest.py::_restore_memory_modules` purges the WHOLE `memory.*`
namespace when a test replaced any module in it. Anything that captured a
`memory.backends.registry` function before that purge still holds the old module
afterwards — and `@register_backend` resolves `register_backend` through
`sys.modules`, so an import registers into the LIVE instance, not the held one.
The held `_REGISTRY` then stays empty and the reader raises "no dialect
registered" for a backend that is shipped, allow-listed and importable.

Order-dependent and platform-dependent by nature: which consumer re-imports
first after a purge decides which tests fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _purge_memory_namespace() -> None:
    """Exactly what the conftest safety net does on teardown."""
    for name in [n for n in list(sys.modules)
                 if n == "memory" or n == "memory_core" or n.startswith("memory.")]:
        del sys.modules[name]


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_a_held_registry_still_resolves_a_dialect_after_a_purge(backend):
    from memory.backends import registry as held

    # Prove the held module is genuinely the discarded one, or the test is
    # asserting nothing: without this the purge could be a no-op and the
    # assertion below would pass for the wrong reason.
    _purge_memory_namespace()
    import memory.backends.registry as live  # noqa: F401  (re-creates the module)

    assert held is not sys.modules["memory.backends.registry"], (
        "the purge did not discard the held module -- this test can no longer fail"
    )

    dialect = held.dialect_singleton_for(backend)
    assert dialect is not None
    assert held._REGISTRY.get(backend) is not None, (
        f"{backend!r} resolved but was not adopted into the held registry"
    )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_a_held_registry_still_builds_a_backend_factory_after_a_purge(backend):
    """`backend_factory_for` shares `_ensure_registered`, so it shares the hazard."""
    from memory.backends import registry as held

    _purge_memory_namespace()
    import memory.backends.registry as live  # noqa: F401

    assert held is not sys.modules["memory.backends.registry"]
    assert callable(held.backend_factory_for(backend))
