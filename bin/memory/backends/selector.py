"""Backend selection — resolves `M3_DB_BACKEND` to a `StorageBackend`.

Default is ``sqlite`` (DESIGN_PHILOSOPHIES §1: L1 SQLite is the only required
store; PostgreSQL is opt-in). Selecting ``postgres`` before its implementation
ships raises a clear, actionable error rather than silently falling back — §3
"fail loud, fail safe, never silent".
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from m3_sdk import getenv_compat

from .base import BackendName, StorageBackend

if TYPE_CHECKING:
    from .dialect import Dialect

_VALID: tuple[BackendName, ...] = ("sqlite", "postgres")

# Cache the resolved backend per name so capability probes / pools aren't rebuilt
# on every call. Guarded because MCP tool impls may resolve concurrently.
_backends: dict[str, StorageBackend] = {}
_lock = threading.Lock()

# Memoized backend NAME. The backend KIND (sqlite vs postgres) is fixed for a
# process — `active_database()` overrides the DB *path/resolver*, never
# M3_DB_BACKEND — so re-reading the env on every call (getenv_compat, ~0.9µs)
# was pure waste across 100+ hot-path dispatch sites. Resolve once, then serve
# from this cache. `_reset_for_tests()` clears it so a test can flip the env.
_resolved_name: "BackendName | None" = None


def resolve_backend_name() -> BackendName:
    """Resolve the configured backend name (memoized after first call).

    Precedence mirrors every other m3 flag: ``M3_DB_BACKEND`` env, then the
    legacy ``DB_BACKEND`` alias (via ``getenv_compat``), then ``sqlite``.
    An unrecognized value raises rather than defaulting — a typo like
    ``postgre`` must not silently run SQLite.

    The result is cached process-wide: the backend kind cannot change within a
    process (unlike the active DB path, which ``active_database()`` can override).
    Tests that flip ``M3_DB_BACKEND`` must call ``_reset_for_tests()`` first.
    """
    global _resolved_name
    cached = _resolved_name
    if cached is not None:
        return cached
    raw = (getenv_compat("M3_DB_BACKEND", "DB_BACKEND", "sqlite") or "sqlite").strip().lower()
    if raw not in _VALID:
        raise ValueError(
            f"M3_DB_BACKEND={raw!r} is not recognized; expected one of {_VALID}. "
            f"Unset it to use the default 'sqlite'."
        )
    _resolved_name = raw  # type: ignore[assignment]
    return raw  # type: ignore[return-value]


def active_backend() -> StorageBackend:
    """Return the `StorageBackend` for the configured engine.

    ``sqlite`` (default) and ``postgres`` both ship. The registry resolves the
    validated name to its backend factory, importing the backend module on
    demand — no ``if name ==`` ladder here, so adding a backend (e.g. MariaDB)
    touches only its own file. An allow-listed-but-unregistered name still fails
    loud at selection time.
    """
    name = resolve_backend_name()
    cached = _backends.get(name)
    if cached is not None:
        return cached
    with _lock:
        cached = _backends.get(name)
        if cached is not None:
            return cached
        # The registry maps the validated name to its factory (the backend class),
        # importing the backend module on demand so its @register_backend runs.
        # No `if name==` ladder here — adding a backend touches only its own file.
        from .registry import backend_factory_for

        backend: StorageBackend = backend_factory_for(name)()
        _backends[name] = backend
        return backend


def dialect() -> "Dialect":
    """The SQL :class:`Dialect` for the ACTIVE backend (cached singleton).

    Convenience over ``active_backend().dialect()`` — the form ~96 call sites
    repeat. A per-CALL function, deliberately NOT a module-global bound at import:
    the backend *kind* is fixed per process, but ``active_database()`` overrides
    the DB *path* and ``_reset_for_tests()`` flips ``M3_DB_BACKEND`` in tests, so a
    global captured at import would serve a stale dialect. Every call is cache-hits
    only (memoized name -> cached backend -> frozen dialect singleton), so it is
    the same ~1µs the old chained form cost.
    """
    return active_backend().dialect()


def require_sqlite_backend(tool: str) -> None:
    """Fail loud if a SQLite-only tool is run against a PostgreSQL deployment.

    Many maintenance / migration / CLI tools open ``sqlite3.connect`` directly,
    bypassing the backend seam. On a PostgreSQL-primary deployment that would
    silently read or WRITE a stale, empty SQLite file instead of the live PG
    store — a data-correctness hazard that gives no error. Such a tool calls this
    at entry so an operator who set ``M3_DB_BACKEND=postgres`` gets a clear,
    actionable refusal instead of silently editing the wrong database.

    ``tool`` is a short human name for the message (e.g. ``"backfill_content_hash"``).
    Raises ``RuntimeError`` when the active backend is not sqlite; a no-op on
    sqlite (the default), so it never affects normal SQLite deployments.
    """
    name = resolve_backend_name()
    if name != "sqlite":
        raise RuntimeError(
            f"{tool} operates directly on SQLite, but M3_DB_BACKEND={name!r} is "
            f"selected. Running it would touch a stale SQLite file, not the live "
            f"{name} store. This tool is SQLite-only; run it against a SQLite "
            f"deployment, or unset M3_DB_BACKEND. (Refusing rather than silently "
            f"editing the wrong database.)"
        )


def _reset_for_tests() -> None:
    """Clear the backend + resolved-name caches. Test-only — lets a test flip the
    env (M3_DB_BACKEND) and re-resolve on the next call."""
    global _resolved_name
    with _lock:
        _backends.clear()
        _resolved_name = None
