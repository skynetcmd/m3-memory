"""Backend registry — the single source of truth mapping a backend NAME to its
implementation (backend class + dialect singleton).

Why this exists (DESIGN_PHILOSOPHIES §2, modularity): before this, adding a
backend meant editing an ``if name == "..."`` ladder in ``selector.py`` AND a
``_BY_NAME`` dict literal in ``dialect.py`` — two shared files touched for every
new backend, and a cycle risk because ``dialect.py`` had to import the concrete
dialect singletons to build that dict. The registry inverts the dependency: each
backend module *declares itself* via the :func:`register_backend` decorator at
import time, and the shared modules (``selector.active_backend`` /
``dialect.dialect_for``) READ the registry lazily. Result: a new backend is ONE
self-contained file — ``<name>_backend.py`` with its class, its dialect, and a
``@register_backend("<name>")`` line — and ZERO edits to any shared module.

Fail-loud (§3): registration does NOT widen the allow-list. ``BackendName`` /
``selector._VALID`` remain the authoritative set of *selectable* names; a name
that is registered but not allow-listed still raises when selected, and a name
that is allow-listed but never registered raises a clear "not registered" error
rather than silently falling back to another backend's SQL.

Cycle-break (§2): this module imports NOTHING from the backend modules or from
``memory_core``. The backend modules import THIS (for the decorator); the shared
readers import THIS. The dependency flows one way: backend module -> registry
<- shared reader. Registration happens as a side effect of importing the backend
module, which ``selector.active_backend`` triggers lazily on first use.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .base import BackendName, StorageBackend
    from .dialect import Dialect


class _Entry:
    """One registered backend: how to build it and its shared dialect singleton."""

    __slots__ = ("backend_factory", "dialect")

    def __init__(
        self, backend_factory: "Callable[[], StorageBackend]", dialect: "Dialect"
    ) -> None:
        self.backend_factory = backend_factory
        self.dialect = dialect


# name -> _Entry. Populated by @register_backend at backend-module import time.
_REGISTRY: "dict[str, _Entry]" = {}


def register_backend(
    name: str, *, dialect: "Dialect"
) -> "Callable[[Callable[[], StorageBackend]], Callable[[], StorageBackend]]":
    """Decorator a backend module applies to its backend class (or factory).

    Usage, at the bottom of ``sqlite_backend.py``::

        SQLITE = SqliteDialect()

        @register_backend("sqlite", dialect=SQLITE)
        class SqliteBackend:
            ...

    The decorated object must be callable with no args to produce a
    ``StorageBackend`` instance (a class is exactly that). ``dialect`` is the
    frozen per-backend :class:`Dialect` singleton — registered here so
    ``dialect_for`` never needs to import the concrete dialect classes.

    Registering the same name twice raises (a real bug — two modules claiming one
    backend), rather than silently overwriting.
    """

    def _decorator(
        factory: "Callable[[], StorageBackend]",
    ) -> "Callable[[], StorageBackend]":
        if name in _REGISTRY:
            raise ValueError(
                f"backend {name!r} is already registered; two modules must not "
                f"claim the same backend name"
            )
        _REGISTRY[name] = _Entry(backend_factory=factory, dialect=dialect)
        return factory

    return _decorator


def _ensure_registered(name: str) -> None:
    """Import the backend module for ``name`` so its @register_backend runs.

    Registration is a side effect of importing the backend module. The shared
    readers call this lazily (never at package import) to keep the import graph
    acyclic and to avoid importing a heavy backend that is not in use. Mapping a
    name to its module is the ONE place that knows the file-naming convention;
    adding a backend still needs no edit here as long as it follows
    ``<name>_backend.py`` — kept explicit (not ``importlib`` on a computed name)
    so a typo fails loudly and static analysis can see the imports.

    Resilient to ``sys.modules`` churn (test isolation): if the backend module is
    ALREADY in ``sys.modules`` (so a plain ``import`` is a no-op) but THIS
    registry's ``_REGISTRY`` is empty — the exact split conftest's memory.* purge
    can create, where a stale ``<name>_backend`` registered into a now-discarded
    ``registry`` instance — force a fresh import so ``@register_backend`` re-runs
    against the live registry. Without this the reader would raise "not registered"
    on a perfectly valid backend after an unrelated test reimported the namespace.
    """
    if name in _REGISTRY:
        return
    module_name = {
        "sqlite": "memory.backends.sqlite_backend",
        "postgres": "memory.backends.postgres_backend",
    }.get(name)
    if module_name is None:
        return  # unknown name; caller (dialect_singleton_for/backend_factory_for) raises
    import importlib
    import sys

    existing = sys.modules.get(module_name)
    if existing is not None and name not in _REGISTRY:
        # Present but not registered here -> it registered into a discarded registry.
        # Reload to re-run @register_backend against THIS module's _REGISTRY.
        importlib.reload(existing)
    else:
        importlib.import_module(module_name)


def backend_factory_for(name: "BackendName") -> "Callable[[], StorageBackend]":
    """The registered factory (class) that builds the backend for ``name``.

    Raises ``ValueError`` if the name is allow-listed (selector validated it) but
    no module registered it — a wiring bug, surfaced loudly per §3.
    """
    _ensure_registered(name)
    entry = _REGISTRY.get(name)
    if entry is None:
        raise ValueError(
            f"backend {name!r} is not registered; no ``@register_backend({name!r})`` "
            f"ran. Expected it in ``{name}_backend.py``."
        )
    return entry.backend_factory


def dialect_singleton_for(name: "BackendName") -> "Dialect":
    """The registered frozen :class:`Dialect` singleton for ``name``.

    This is what ``dialect.dialect_for`` delegates to — so ``dialect.py`` holds
    the base class only and never imports a concrete dialect (RH1 cycle-break).
    """
    _ensure_registered(name)
    entry = _REGISTRY.get(name)
    if entry is None:
        raise ValueError(f"no dialect registered for backend {name!r}")
    return entry.dialect


def registered_names() -> "tuple[str, ...]":
    """Names registered SO FAR (after their modules were imported).

    Used by the conformance test to iterate every backend the runtime knows. Call
    ``_ensure_registered`` for each allow-listed name first if you need the full
    set eagerly (the test does this).
    """
    return tuple(_REGISTRY)
