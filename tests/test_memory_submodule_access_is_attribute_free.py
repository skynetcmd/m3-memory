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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _parent_without_the_attribute(monkeypatch):
    """`memory` present but missing `backends`; `memory.backends` still cached."""
    import memory.backends  # noqa: F401  (ensure both are in sys.modules)

    parent = sys.modules["memory"]
    # raising=False: an earlier test may have ALREADY left the attribute absent
    # -- that is the state this helper exists to create, so finding it is not an
    # error. Without this the file failed intermittently on the PG lane, which is
    # the flakiness this whole area is about.
    monkeypatch.delattr(parent, "backends", raising=False)
    assert not hasattr(parent, "backends"), "precondition not established"
    return parent


def test_the_attribute_path_really_does_break(monkeypatch):
    """Without this, the test below proves nothing."""
    _parent_without_the_attribute(monkeypatch)

    import memory  # the cached, attribute-less parent

    try:
        memory.backends
    except AttributeError as e:
        assert "no attribute 'backends'" in str(e), str(e)
    else:
        raise AssertionError("expected the attribute path to fail")


def test_importlib_reaches_the_submodule_anyway(monkeypatch):
    _parent_without_the_attribute(monkeypatch)

    mod = importlib.import_module("memory.backends")
    assert mod is sys.modules["memory.backends"]
    assert callable(mod.active_backend)


def test_the_dialect_accessor_survives_that_state(monkeypatch):
    _parent_without_the_attribute(monkeypatch)

    mod = importlib.import_module("memory.backends")
    assert callable(mod.dialect), type(mod.dialect).__name__
