"""Regression test for the M3_MEMORY_ROOT engine-DB drift.

Bug: _default_db_path() returned M3_MEMORY_ROOT/engine/agent_memory.db whenever
M3_MEMORY_ROOT (or M3_ENGINE_ROOT) was set — even when that engine DB was a
0-table stub auto-created on first connect — silently shadowing a populated
legacy memory/agent_memory.db. Fix: _db_is_populated() gates the derived engine
path; an empty/missing derived engine DB falls through to a populated legacy DB.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import m3_sdk  # noqa: E402


def _make_db(path: str, populated: bool) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    if populated:
        conn.execute("CREATE TABLE memory_items (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO memory_items(id) VALUES ('x')")
        conn.commit()
    # else: leave it as a 0-table stub (the drift scenario)
    conn.close()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for v in ("M3_ENGINE_ROOT", "M3_MEMORY_ROOT", "M3_DATABASE"):
        monkeypatch.delenv(v, raising=False)


def test_db_is_populated(tmp_path):
    missing = str(tmp_path / "nope.db")
    stub = str(tmp_path / "stub.db")
    full = str(tmp_path / "full.db")
    _make_db(stub, populated=False)
    _make_db(full, populated=True)
    assert m3_sdk._db_is_populated(missing) is False
    assert m3_sdk._db_is_populated(stub) is False
    assert m3_sdk._db_is_populated(full) is True


def test_empty_engine_stub_falls_through_to_populated_legacy(tmp_path, monkeypatch):
    """The exact drift: M3_MEMORY_ROOT set, engine/ DB is an empty stub, but
    memory/ has the real data -> resolve to memory/, not the stub."""
    root = tmp_path / "m3-memory"
    engine_db = root / "engine" / "agent_memory.db"
    legacy_db = root / "memory" / "agent_memory.db"
    _make_db(str(engine_db), populated=False)   # empty stub
    _make_db(str(legacy_db), populated=True)     # real data
    monkeypatch.setenv("M3_MEMORY_ROOT", str(root))

    resolved = m3_sdk._default_db_path()
    assert os.path.abspath(resolved) == os.path.abspath(str(legacy_db)), (
        f"expected populated legacy DB, got {resolved}"
    )


def test_populated_engine_wins_over_legacy(tmp_path, monkeypatch):
    """Once the engine DB is populated (post-migration), it is preferred."""
    root = tmp_path / "m3-memory"
    engine_db = root / "engine" / "agent_memory.db"
    legacy_db = root / "memory" / "agent_memory.db"
    _make_db(str(engine_db), populated=True)
    _make_db(str(legacy_db), populated=True)
    monkeypatch.setenv("M3_MEMORY_ROOT", str(root))
    assert os.path.abspath(m3_sdk._default_db_path()) == os.path.abspath(str(engine_db))


def test_explicit_engine_root_honored_even_if_empty(tmp_path, monkeypatch):
    """An explicit M3_ENGINE_ROOT is a deliberate operator choice; honor it
    verbatim even when empty (fresh deployment), without scanning for legacy."""
    engine_root = tmp_path / "explicit_engine"
    engine_root.mkdir()
    monkeypatch.setenv("M3_ENGINE_ROOT", str(engine_root))
    resolved = m3_sdk._default_db_path()
    assert os.path.abspath(resolved) == os.path.abspath(str(engine_root / "agent_memory.db"))


def test_fresh_install_no_data_returns_engine_path(tmp_path, monkeypatch):
    """No env, no populated DB anywhere -> derived engine path (created later)."""
    root = tmp_path / "m3-memory"
    monkeypatch.setenv("M3_MEMORY_ROOT", str(root))
    # nothing exists yet
    resolved = m3_sdk._default_db_path()
    expected = os.path.join(m3_sdk.get_m3_engine_root(), "agent_memory.db")
    assert os.path.abspath(resolved) == os.path.abspath(expected)


# ── DSN-in-M3_DATABASE guard ──────────────────────────────────────────────────
# Regression: resolve_db_path() ran os.path.abspath() on its candidate
# unconditionally, so a DSN became a path rooted at the CWD. On Windows that
# produced WinError 123 and wedged AgentOS_NotificationWaiter in a restart loop
# (2026-09-12); on POSIX the mangled name is a LEGAL path, so SQLite would have
# created an empty DB there and every read would have silently returned nothing.

@pytest.mark.parametrize("dsn", [
    "postgresql://user:pw@db.example.invalid:5432/agent_memory",
    "postgres://u@h/db",
    "mysql://u@h/db",
    "mariadb://u@h/db",
    "sqlite:///tmp/x.db",
    "  POSTGRESQL://upper@h/db  ",  # whitespace + case must not evade the guard
])
def test_dsn_is_rejected_not_coerced(dsn, monkeypatch):
    monkeypatch.setenv("M3_DATABASE", dsn)
    with pytest.raises(ValueError) as exc:
        m3_sdk.resolve_db_path(None)
    assert "M3_DATABASE" in str(exc.value)
    assert "M3_DB_BACKEND" in str(exc.value), "must point at the right knob"


def test_dsn_error_does_not_leak_password(monkeypatch):
    """A DSN carries credentials; the message must not echo them."""
    monkeypatch.setenv("M3_DATABASE", "postgresql://dbuser:hunter2@h:5432/db")
    with pytest.raises(ValueError) as exc:
        m3_sdk.resolve_db_path(None)
    assert "hunter2" not in str(exc.value)
    assert "dbuser" not in str(exc.value)


def test_explicit_dsn_argument_is_rejected(monkeypatch):
    with pytest.raises(ValueError) as exc:
        m3_sdk.resolve_db_path("postgresql://u@h/db")
    assert "explicit argument" in str(exc.value)


def test_windows_drive_path_is_not_mistaken_for_a_dsn(monkeypatch):
    r"""`C:\...` parses as scheme 'c' under urlparse — must still be a PATH.

    This is why the guard matches known DSN schemes instead of asking whether
    the value merely has a scheme.
    """
    monkeypatch.setenv("M3_DATABASE", r"C:\Users\x\.m3\engine\agent_memory.db")
    assert m3_sdk.resolve_db_path(None).lower().endswith("agent_memory.db")


def test_ordinary_paths_still_resolve(tmp_path, monkeypatch):
    """The guard must not disturb the normal path."""
    p = tmp_path / "agent_memory.db"
    monkeypatch.setenv("M3_DATABASE", str(p))
    assert m3_sdk.resolve_db_path(None) == os.path.abspath(str(p))
    # explicit arg wins over env, and is still absolutised
    other = tmp_path / "other.db"
    assert m3_sdk.resolve_db_path(str(other)) == os.path.abspath(str(other))


def test_relative_path_is_still_absolutised(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("M3_DATABASE", "rel/agent_memory.db")
    assert os.path.isabs(m3_sdk.resolve_db_path(None))
