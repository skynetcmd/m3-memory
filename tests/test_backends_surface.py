"""Public-surface + identity gate for the memory.backends package (plan A5, RH5).

WHY a dedicated file: ``test_memory_core_parity.py`` snapshots only ``memory_core``
symbols — which do NOT include the dialect classes or the ``dialect()`` accessor —
and it passes on ADDED symbols. So it is BLIND to a dialect relocation or a
dropped backend export (RH5). This file pins the ``memory.backends`` surface
directly and asserts the singleton identities the co-location refactor must
preserve.

Regenerate ``EXPECTED_EXPORTS`` on an INTENDED surface change (a new backend
export, a renamed accessor) — the assertion prints the diff — exactly like the
symbol-parity discipline.
"""
from __future__ import annotations

import memory.backends as b

# The intended public surface of memory.backends. A DROP here (e.g. dialect() or a
# backend export vanishing) or an unplanned ADD fails loudly.
EXPECTED_EXPORTS = {
    "BackendName",
    "Capabilities",
    "KeywordHit",
    "StorageBackend",
    "VectorHit",
    "active_backend",
    "chatlog_table",
    "chatlog_table_for",
    "dialect",
    "require_sqlite_backend",
    "resolve_backend_name",
}


def test_backends_all_matches_expected():
    actual = set(b.__all__)
    assert actual == EXPECTED_EXPORTS, (
        "memory.backends.__all__ drifted:\n"
        f"  added:   {actual - EXPECTED_EXPORTS}\n"
        f"  removed: {EXPECTED_EXPORTS - actual}\n"
        "If intended, update EXPECTED_EXPORTS."
    )


def test_every_export_is_importable():
    # __all__ names must actually resolve (a stale name in __all__ is a silent bug).
    for name in b.__all__:
        assert hasattr(b, name), f"memory.backends.__all__ lists {name!r} but it is absent"


def test_dialect_accessor_is_callable_not_the_submodule():
    # Guards the accessor/submodule name collision (see __init__.py note): the
    # PACKAGE attribute `dialect` must be the accessor FUNCTION, while the submodule
    # stays reachable by its qualified name.
    import sys

    assert callable(b.dialect), "memory.backends.dialect must be the accessor function"
    assert "memory.backends.dialect" in sys.modules, "the dialect submodule must remain importable"
    mod = sys.modules["memory.backends.dialect"]
    assert hasattr(mod, "dialect_for"), "the qualified submodule must still expose dialect_for"


def test_dialect_identity_held():
    # Co-location must not mint a new singleton: the accessor, active_backend's
    # dialect, and dialect_for must all return the SAME frozen object per backend.
    from memory.backends.dialect import dialect_for
    from memory.backends.postgres_backend import POSTGRES
    from memory.backends.sqlite_backend import SQLITE

    assert b.dialect() is b.active_backend().dialect()
    assert b.dialect() is SQLITE  # sqlite is the default backend
    assert dialect_for("sqlite") is SQLITE
    assert dialect_for("postgres") is POSTGRES
    # frozen: identity is stable across calls
    assert dialect_for("sqlite") is dialect_for("sqlite")


def test_registered_dialect_matches_backend_dialect():
    # The registry's dialect and the backend instance's dialect must be the SAME
    # frozen dialect. We assert VALUE equality (frozen dataclass ==), not identity:
    # conftest's module-restore can reimport memory.backends.sqlite_backend under a
    # test run, minting a second equal SQLITE object — the same reason the seam
    # tests assert shape, not class identity. In a single import both are `is` (see
    # test_dialect_identity_held, which runs within one import graph); the invariant
    # that matters here is that the registry never serves a DIFFERENT dialect than
    # the backend.
    from memory.backends import registry
    from memory.backends.sqlite_backend import SqliteBackend

    registry._ensure_registered("sqlite")
    reg = registry.dialect_singleton_for("sqlite")
    got = SqliteBackend().dialect()
    # Behavioral equality (reload-proof): conftest's sys.modules-restore can make
    # `reg` and `got` distinct class objects across a run, so compare what actually
    # matters — the SQL they emit and their backend/param identity — not `is`/`==`
    # over a possibly-reloaded frozen class.
    assert type(reg).__name__ == type(got).__name__ == "SqliteDialect"
    assert reg.backend == got.backend == "sqlite"
    assert reg.param() == got.param() == "?"
    assert reg.now() == got.now()
    assert reg.insert_or_ignore() == got.insert_or_ignore()
