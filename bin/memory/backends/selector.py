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

# ⚠ The backend NAME is deliberately NOT memoized. A module-global memo lives
# per module INSTANCE, and this module can be loaded twice in one process; two
# memos then disagree and there is no correct winner — the seam served sqlite to
# code under M3_DB_BACKEND=postgres. Resolution is an env read plus a membership
# check, so the memo bought nothing measurable. Consequence: an in-process
# M3_DB_BACKEND change takes effect on the next call (installer relies on this).


def resolve_backend_name() -> BackendName:
    """Resolve the configured backend name from the environment, on every call.

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
    ``active_database()`` overrides the DB *path* and ``M3_DB_BACKEND`` is re-read
    on every call, so a global captured at import would serve a stale dialect.
    Each call is an env read plus cache hits (cached backend -> frozen dialect
    singleton).
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


def backend_for(uri: str) -> StorageBackend:
    """A backend addressing ONE SPECIFIC store, chosen by the shape of `uri`.

    Distinct from `active_backend()`, which is deliberately singular: one backend
    per configured KIND, its instance memoized. Some callers legitimately
    need a SECOND store at the same time — sync holds a local store and a remote
    warehouse open together — and neither `active_backend()` nor
    `open_readonly(path)` can express that (the latter discards the path on
    PostgreSQL and is read-only by contract).

    Dispatch is on the URI shape, which is the only thing a caller reliably has:

      ``postgresql://…`` / ``postgres://``  -> PostgresBackend(dsn=uri)
      anything else                          -> a filesystem path -> SQLite

    Keyed on an explicit scheme allowlist rather than "not a file" so a THIRD
    backend registers by adding its scheme here — one place that knows the
    mapping, in the same spirit as `chatlog_table_for`'s explicit
    ``backend == "sqlite"`` predicate rather than an else-means-postgres accident.

    ⚠ NOT memoized, unlike `active_backend()`. Each call builds a backend that
    owns a connection pool, so the CALLER owns its lifetime and must `close()` it
    — typically in a `finally`. Caching instead would keep pools alive for stores
    a short-lived process touched once, which is precisely wrong for an hourly
    cron.

    ⚠ Constructing a PostgresBackend with an explicit dsn BYPASSES `_resolve_dsn`,
    and with it the `_reject_same_as_warehouse` / `_reject_forbidden_host` guards
    that run there. A caller pointing this at a primary store must invoke those
    guards itself; this function deliberately does not, because it has no way to
    know which ROLE (primary vs warehouse) the caller means the URI to play.

    A SQLite path returns a PATH-BOUND backend (`SqliteBackend(db_path=…)`),
    which addresses that file rather than the process-active store. This
    deliberately raised until SqliteBackend could take a path: returning an
    unpinned backend "for" /some/other.db would have silently read and written
    the DEFAULT database — a caller believing it addressed store A while touching
    store B, with no error, which is the precise failure this effort removes.
    """
    if not uri:
        raise ValueError("backend_for() needs a store URI; got an empty value.")
    scheme = uri.split("://", 1)[0].lower() if "://" in uri else ""
    if scheme in ("postgresql", "postgres"):
        from .postgres_backend import PostgresBackend

        return PostgresBackend(dsn=uri)
    if scheme:
        raise ValueError(
            f"Unrecognized store URI scheme {scheme!r} in {uri!r}. Supported: a "
            "filesystem path (SQLite) or a postgresql:// DSN. Refusing rather "
            "than guessing a backend for an unknown scheme."
        )
    from .sqlite_backend import SqliteBackend

    return SqliteBackend(db_path=uri)


def _reset_for_tests() -> None:
    """Clear the backend cache. Test-only — drops backend instances (and their
    pools) built under an earlier env. The name needs no reset; it is re-read
    on every call."""
    with _lock:
        _backends.clear()
