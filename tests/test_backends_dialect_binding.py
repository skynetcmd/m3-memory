"""`memory.backends.dialect` must be the ACCESSOR, never the submodule.

The package exports a callable `dialect` AND contains a submodule of the same
name. Which one the attribute holds depends on import ORDER inside
`memory/backends/__init__.py`: importing the submodule binds the attribute to
the module, and the later `from .selector import ... dialect ...` overwrites it
with the function. A completed import always yields the function.

A package observed PART-WAY THROUGH initialisation does not. Measured
2026-09-14 on a full-suite run: 96 `TypeError: 'module' object is not callable`
across 89 failing tests, every one of them a `from memory.backends import
dialect; dialect()` call site, starting mid-run and persisting to the end. The
seam reports nothing -- the failures surface far from the cause.

These tests pin the invariant so a future reordering fails HERE, loudly, with
one message, instead of as 96 mystery TypeErrors somewhere else.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)


def test_the_package_attribute_is_the_callable_not_the_module():
    """THE invariant. Every `from memory.backends import dialect` call site in
    the tree -- 47 production files at last count -- depends on it."""
    import memory.backends as backends

    assert callable(backends.dialect), (
        f"memory.backends.dialect is {type(backends.dialect).__name__}, not the "
        f"accessor function -- every `dialect()` call site now raises "
        f"\"'module' object is not callable\""
    )


def test_it_survives_importing_the_submodule_afterwards():
    """The dangerous direction. Importing the submodule sets the parent
    attribute to the MODULE as a side effect; if that happens after the
    package finished initialising, the accessor must still win."""
    import memory.backends as backends
    import memory.backends.dialect  # noqa: F401 - the side effect IS the test

    assert callable(backends.dialect), (
        "importing memory.backends.dialect rebound the package attribute to "
        "the submodule -- the accessor was shadowed"
    )


def test_the_accessor_actually_resolves_a_dialect():
    """A guard that only checked `callable` would pass on any stray function."""
    from memory.backends import dialect

    d = dialect()
    assert hasattr(d, "param"), (
        f"dialect() returned {d!r}, which is not a Dialect"
    )
    assert callable(d.param), "the resolved dialect has no param() method"


def test_the_submodule_is_still_reachable_by_its_qualified_name():
    """The other polarity: shadowing the ATTRIBUTE must not make the module
    unreachable. All module-level imports use the qualified form."""
    from memory.backends.dialect import chatlog_table_for

    assert callable(chatlog_table_for), (
        "memory.backends.dialect (the submodule) is no longer importable by "
        "its qualified name -- the shadowing went too far"
    )
