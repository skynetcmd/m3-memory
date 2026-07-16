"""Backend selection — resolves `M3_DB_BACKEND` to a `StorageBackend`.

Default is ``sqlite`` (DESIGN_PHILOSOPHIES §1: L1 SQLite is the only required
store; PostgreSQL is opt-in). Selecting ``postgres`` before its implementation
ships raises a clear, actionable error rather than silently falling back — §3
"fail loud, fail safe, never silent".
"""
from __future__ import annotations

import threading

from m3_sdk import getenv_compat

from .base import BackendName, StorageBackend

_VALID: tuple[BackendName, ...] = ("sqlite", "postgres")

# Cache the resolved backend per name so capability probes / pools aren't rebuilt
# on every call. Guarded because MCP tool impls may resolve concurrently.
_backends: dict[str, StorageBackend] = {}
_lock = threading.Lock()


def resolve_backend_name() -> BackendName:
    """Resolve the configured backend name.

    Precedence mirrors every other m3 flag: ``M3_DB_BACKEND`` env, then the
    legacy ``DB_BACKEND`` alias (via ``getenv_compat``), then ``sqlite``.
    An unrecognized value raises rather than defaulting — a typo like
    ``postgre`` must not silently run SQLite.
    """
    raw = (getenv_compat("M3_DB_BACKEND", "DB_BACKEND", "sqlite") or "sqlite").strip().lower()
    if raw not in _VALID:
        raise ValueError(
            f"M3_DB_BACKEND={raw!r} is not recognized; expected one of {_VALID}. "
            f"Unset it to use the default 'sqlite'."
        )
    return raw  # type: ignore[return-value]


def active_backend() -> StorageBackend:
    """Return the `StorageBackend` for the configured engine.

    Phase 0: only ``sqlite`` is implemented. Selecting ``postgres`` raises a
    clear NotImplementedError pointing at the phase that adds it — so the flag
    can be wired end-to-end and tested now without a half-built PG path.
    """
    name = resolve_backend_name()
    cached = _backends.get(name)
    if cached is not None:
        return cached
    with _lock:
        cached = _backends.get(name)
        if cached is not None:
            return cached
        if name == "sqlite":
            from .sqlite_backend import SqliteBackend

            backend: StorageBackend = SqliteBackend()
        elif name == "postgres":
            from .postgres_backend import PostgresBackend

            backend = PostgresBackend()
        else:  # pragma: no cover - resolve_backend_name already validated
            raise ValueError(f"unhandled backend {name!r}")
        _backends[name] = backend
        return backend


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
    """Clear the backend cache. Test-only — lets a test flip the env and re-resolve."""
    with _lock:
        _backends.clear()
