"""`memory.backends.dialect` must stay the accessor function.

The package re-exports a `dialect` CALLABLE whose name collides with its
`dialect` SUBMODULE. Anything that binds the submodule onto the package turns
every `dialect()` call site into "'module' object is not callable" somewhere far
away, with nothing naming the cause -- 96 of that TypeError across 89 tests on
2026-09-14, 141 across 135 tests on the Windows PostgreSQL lane on 2026-10-02.

The package therefore refuses that rebind and logs where it came from. These
tests pin both halves: the refusal, and the diagnostic that makes it findable.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def test_dialect_is_the_accessor_after_a_normal_import():
    import memory.backends as B

    assert callable(B.dialect), type(B.dialect).__name__


def test_rebinding_dialect_to_a_module_is_refused(caplog):
    """The guard must actually refuse -- and say where the attempt came from."""
    import memory.backends as B

    original = B.dialect
    assert callable(original)

    fake = ModuleType("memory.backends.dialect")
    with caplog.at_level(logging.WARNING, logger="memory.backends"):
        B.dialect = fake          # exactly what the import machinery does

    assert B.dialect is original, "the guard let the submodule shadow the accessor"
    assert callable(B.dialect)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "refused to rebind" in msgs, msgs
    # The diagnostic must locate the attempt, not merely announce it.
    assert f"{Path(__file__).name}" in msgs or ".py:" in msgs, msgs


def test_a_non_module_value_is_still_assignable():
    """The guard is narrow: it must not freeze the attribute outright."""
    import memory.backends as B

    original = B.dialect
    try:
        B.dialect = original      # reassigning the function must work
        assert B.dialect is original
    finally:
        B.dialect = original

    # and an unrelated attribute is untouched by the guard
    B._guard_probe = 1
    assert B._guard_probe == 1
    del B._guard_probe


def test_the_submodule_is_still_reachable_by_its_qualified_name():
    """Refusing the rebind must not make the module itself unimportable."""
    from memory.backends.dialect import dialect_for  # noqa: F401

    assert isinstance(sys.modules["memory.backends.dialect"], ModuleType)
