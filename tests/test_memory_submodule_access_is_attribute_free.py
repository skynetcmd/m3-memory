"""Reaching a `memory.*` submodule must not go through the parent attribute.

`import memory.backends` is a NO-OP when `memory.backends` is already in
`sys.modules`, so it does not (re)bind the attribute on a `memory` package
object that was rebuilt without it. The following `memory.backends` access then
raises `AttributeError: module 'memory' has no attribute 'backends'` -- which is
also why a dotted-string `monkeypatch.setattr("memory.backends.X", ...)` target
fails. `importlib.import_module` reads `sys.modules` directly and is immune.

This bit `tests/test_reembed_space.py` three times: once latently, once after a
module-level import "fixed" it (collection-time imports do not survive the
conftest namespace purge), and once more after an in-test `import` statement.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


@pytest.fixture
def parent_without_the_attribute():
    """`memory` present but missing `backends`; `memory.backends` still cached.

    A FIXTURE, not a monkeypatch call: conftest's autouse namespace-coherence
    check tears down AFTER monkeypatch undoes its changes, so a
    `monkeypatch.delattr` here would still look like breakage at that point and
    error this file's own tests. A test-local fixture finalizes first.
    """
    import memory.backends  # noqa: F401  (ensure both are in sys.modules)

    parent = sys.modules["memory"]
    saved = getattr(parent, "backends", None)
    if saved is not None:
        delattr(parent, "backends")
    assert not hasattr(parent, "backends"), "precondition not established"
    try:
        yield parent
    finally:
        if saved is not None:
            parent.backends = saved
            sys.modules.setdefault("memory", parent)


def test_the_attribute_path_really_does_break(parent_without_the_attribute):
    """Without this, the test below proves nothing."""

    import memory  # the cached, attribute-less parent

    try:
        memory.backends
    except AttributeError as e:
        assert "no attribute 'backends'" in str(e), str(e)
    else:
        raise AssertionError("expected the attribute path to fail")


def test_importlib_reaches_the_submodule_anyway(parent_without_the_attribute):

    mod = importlib.import_module("memory.backends")
    assert mod is sys.modules["memory.backends"]
    assert callable(mod.active_backend)


def test_the_dialect_accessor_survives_that_state(parent_without_the_attribute):

    mod = importlib.import_module("memory.backends")
    assert callable(mod.dialect), type(mod.dialect).__name__
