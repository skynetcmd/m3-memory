"""Phase 0 tests for the storage-backend capability seam.

These assert the seam's *shape and behavior-preservation*, not any PostgreSQL
functionality (none exists yet). Specifically:
  - the selector resolves the default to SQLite and validates the flag loudly;
  - selecting postgres fails loud (not a silent SQLite fallback);
  - the SQLite backend delegates to the SAME pooled connection flow as `_db()`,
    so Phase 0 is a genuine zero-behavior-change refactor;
  - the placeholder helper matches the qmark idiom it replaces.
"""
from __future__ import annotations

import sqlite3

import pytest
from memory.backends import (
    Capabilities,
    StorageBackend,
    active_backend,
    resolve_backend_name,
)
from memory.backends import selector as _selector


@pytest.fixture(autouse=True)
def _reset_backend_cache():
    """Each test resolves the backend fresh (the cache is process-global)."""
    _selector._reset_for_tests()
    yield
    _selector._reset_for_tests()


def test_default_backend_is_sqlite(monkeypatch):
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    monkeypatch.delenv("DB_BACKEND", raising=False)
    assert resolve_backend_name() == "sqlite"
    backend = active_backend()
    assert backend.name == "sqlite"
    # The protocol is runtime-checkable — the adapter satisfies the seam.
    assert isinstance(backend, StorageBackend)


def test_explicit_sqlite_selection(monkeypatch):
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    assert resolve_backend_name() == "sqlite"
    assert active_backend().name == "sqlite"


def test_legacy_alias_resolves(monkeypatch):
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    assert resolve_backend_name() == "sqlite"


def test_invalid_backend_raises_not_silent(monkeypatch):
    """A typo must fail loud (§3), never silently run the default."""
    monkeypatch.setenv("M3_DB_BACKEND", "postgre")  # typo
    with pytest.raises(ValueError, match="not recognized"):
        resolve_backend_name()


def test_postgres_selection_builds_backend(monkeypatch):
    """Selecting postgres now builds a real PostgresBackend (Phase 1).

    Construction must NOT open a connection (the pool is lazy), so this passes
    with only a DSN present and no reachable server — the connection-time
    behavior is covered by the live integration tests.
    """
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", "postgresql://u:p@127.0.0.1:5433/nonexistent")
    assert resolve_backend_name() == "postgres"
    backend = active_backend()
    assert backend.name == "postgres"
    assert isinstance(backend, StorageBackend)
    # dialect is available without any connection (pure, lazy pool untouched)
    assert backend.dialect().backend == "postgres"
    assert backend.placeholder(2) == "%s, %s"


def test_postgres_selection_no_dsn_fails_loud(monkeypatch):
    """postgres backend with NO DSN anywhere raises, never silently uses SQLite."""
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.delenv("M3_PG_URL", raising=False)
    monkeypatch.delenv("PG_URL", raising=False)
    # Force the vault fallback to yield nothing, so the real _resolve_dsn runs
    # its full env -> vault -> raise path with the vault deterministically empty
    # (independent of whatever secret this machine/CI box has stored).
    try:
        from m3_core.context import M3Context

        monkeypatch.setattr(M3Context, "get_secret", lambda self, name: None)
    except ImportError:
        pass
    # With no env DSN and an empty vault, construction must raise -- never a
    # silent fallback to SQLite.
    with pytest.raises(RuntimeError, match="no DSN found|PG_URL"):
        active_backend()


def test_placeholder_matches_qmark_idiom(monkeypatch):
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    backend = active_backend()
    assert backend.placeholder(1) == "?"
    assert backend.placeholder(3) == "?, ?, ?"
    # Matches the ",".join("?"*n) idiom it replaces (modulo the standard ", ").
    assert backend.placeholder(3).replace(" ", "") == ",".join("?" * 3)
    with pytest.raises(ValueError):
        backend.placeholder(0)


def test_sqlite_connection_delegates_to_db(monkeypatch, tmp_path):
    """The backend's connection() IS the existing pooled `_db()` flow.

    Proves Phase 0 changes no connection behavior: a row written through the
    backend's connection is visible through a fresh `_db()` — same DB, same pool.
    """
    from conftest import create_memory_items_schema  # test helper

    db_path = tmp_path / "agent_memory.db"
    create_memory_items_schema(db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")

    from memory import db as db_mod

    backend = active_backend()
    caps = backend.capabilities()
    assert isinstance(caps, Capabilities)
    assert caps.backend == "sqlite"
    assert caps.keyword == "fts5"  # native baseline always present

    # Write via the backend's connection...
    with backend.connection() as conn:
        assert isinstance(conn, sqlite3.Connection)
        conn.execute(
            "INSERT INTO memory_items (id, type, content) VALUES (?, ?, ?)",
            ("seam-phase0", "note", "written through the backend seam"),
        )

    # ...read via the plain `_db()` path — same store, so the row is there.
    with db_mod._db() as conn:
        row = conn.execute(
            "SELECT content FROM memory_items WHERE id = ?", ("seam-phase0",)
        ).fetchone()
    assert row is not None
    assert row[0] == "written through the backend seam"
