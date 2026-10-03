"""The conftest detector must fire on an incoherent `memory.*` namespace.

A test that leaves `memory` without a `backends` attribute, or `dialect` bound
to its submodule, breaks LATER tests with errors that point nowhere near the
cause. The detector exists so the blame lands on the test that did it.

Breakage here is undone in `finally`, NOT via monkeypatch: the autouse teardown
that runs the detector fires BEFORE monkeypatch undoes its changes, so a
monkeypatch-based break would trip the detector on this file's own tests.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from conftest import _assert_memory_namespace_is_coherent as _check  # noqa: E402


def test_a_healthy_namespace_passes():
    import memory.backends  # noqa: F401

    _check()


def test_an_absent_memory_is_fine():
    """That is the purge having done its job, not a fault."""
    import memory.backends  # noqa: F401

    saved = sys.modules.pop("memory")
    try:
        _check()
    finally:
        sys.modules["memory"] = saved


def test_a_missing_backends_attribute_is_caught():
    import memory.backends  # noqa: F401

    parent = sys.modules["memory"]
    saved = parent.backends
    delattr(parent, "backends")
    try:
        with pytest.raises(RuntimeError, match="WITHOUT a `backends` attribute"):
            _check()
    finally:
        parent.backends = saved


def test_the_message_names_where_to_look():
    """A diagnostic that does not say what to change costs a second round."""
    import memory.backends  # noqa: F401

    parent = sys.modules["memory"]
    saved = parent.backends
    delattr(parent, "backends")
    try:
        with pytest.raises(RuntimeError) as e:
            _check()
        assert ".claude/rules/test-sandbox.md" in str(e.value)
    finally:
        parent.backends = saved


def test_a_shadowed_dialect_is_caught():
    """Written through `__dict__` on purpose.

    `_BackendsModule.__setattr__` REFUSES this rebind, so a plain `setattr`
    cannot produce the state -- which is the package guard working. The detector
    is the second line of defence, for a shadow that arrives some other way.
    """
    import memory.backends  # noqa: F401

    mod = sys.modules["memory.backends"]
    saved = mod.dialect
    mod.__dict__["dialect"] = ModuleType("memory.backends.dialect")
    try:
        with pytest.raises(RuntimeError, match="not the accessor function"):
            _check()
    finally:
        mod.__dict__["dialect"] = saved


def test_the_package_guard_blocks_the_plain_setattr_path():
    """Belt and braces: the two defences cover different entry points."""
    import memory.backends  # noqa: F401

    mod = sys.modules["memory.backends"]
    original = mod.dialect
    mod.dialect = ModuleType("memory.backends.dialect")   # refused + logged
    assert mod.dialect is original
    assert callable(mod.dialect)
