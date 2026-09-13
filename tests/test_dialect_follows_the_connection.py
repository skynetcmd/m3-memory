"""The dialect must follow the CONNECTION you hold, not a global env var.

Every other seam entry point answers "what is this deployment configured to
use?" -- they read `M3_DB_BACKEND`. That is the wrong question when a function
is HANDED a connection, because a process can legitimately hold two at once.

`pg_sync` is exactly that shape: a local SQLite store and a remote PostgreSQL
warehouse in the same call. It had a module-level `_LOCAL_PARAM` baked at
import from the global, so with `M3_DB_BACKEND=postgres` it rendered `%s` into
statements executed on the SQLite cursor::

    WARNING pg_sync: Lock acquisition failed: near "%": syntax error

16 tests failed that way on a real PostgreSQL run (#171) -- and they were RIGHT
to hand those functions a `sqlite3` cursor, because the local sync-lock and
watermark store IS SQLite whatever the env says.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory.backends.dialect import dialect_for_connection  # noqa: E402


@pytest.fixture
def sqlite_conn():
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


def test_a_sqlite_connection_gets_the_sqlite_param(sqlite_conn):
    assert dialect_for_connection(sqlite_conn).param() == "?"


def test_a_sqlite_cursor_gets_the_sqlite_param(sqlite_conn):
    """Call sites hold CURSORS, not connections -- the cursor is the shape that
    actually reaches the seam."""
    assert dialect_for_connection(sqlite_conn.cursor()).param() == "?"


@pytest.mark.parametrize("backend", ["postgres", "sqlite", "not-a-backend"])
def test_the_global_env_var_cannot_override_a_real_connection(
    backend, sqlite_conn, monkeypatch
):
    """THE regression. A SQLite cursor gets `?` no matter what M3_DB_BACKEND
    says -- including a bogus value, which must not make it guess."""
    monkeypatch.setenv("M3_DB_BACKEND", backend)
    assert dialect_for_connection(sqlite_conn.cursor()).param() == "?", (
        f"M3_DB_BACKEND={backend!r} leaked into a SQLite connection's dialect; "
        f"PostgreSQL syntax would reach a SQLite cursor"
    )


def test_pg_sync_param_helper_follows_the_cursor(sqlite_conn, monkeypatch):
    """The call-site wrapper must inherit the property, not just the seam."""
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    import pg_sync

    assert pg_sync._param_of(sqlite_conn.cursor()) == "?"


def test_no_call_site_still_interpolates_the_import_time_constant():
    """`_LOCAL_PARAM` may remain as the bootstrap fallback, but interpolating it
    into SQL is the bug: it is bound once at import from the global."""
    import pathlib

    src = pathlib.Path(_BIN, "pg_sync.py").read_text(encoding="utf-8")
    assert "{_LOCAL_PARAM}" not in src, (
        "a call site still interpolates the import-time constant; it must use "
        "_param_of(<cursor>) so the placeholder follows the connection"
    )


def test_an_unknown_driver_falls_back_rather_than_raising():
    """A pooled proxy or wrapper must behave as it did before, never explode."""
    assert dialect_for_connection(object()).param() in ("?", "%s")


@pytest.mark.skipif(
    not os.environ.get("M3_PRIMARY_PG_URL"),
    reason="no PostgreSQL URL configured",
)
def test_a_real_postgres_cursor_gets_the_postgres_param(monkeypatch):
    """The other half: the detector must actually DISCRIMINATE. Without this,
    a function that always returned '?' would pass every test above."""
    # Either driver proves the point; take whichever this environment has.
    # CI installs psycopg2-binary (what postgres_backend itself uses), while a
    # dev box may have psycopg 3. Importing only one made this a hard FAILURE
    # on the CI lane rather than a skip -- measured 2026-09-13.
    driver = None
    for mod in ("psycopg", "psycopg2"):
        try:
            driver = __import__(mod)
            break
        except ImportError:
            continue
    if driver is None:
        pytest.skip("neither psycopg nor psycopg2 is installed")

    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")  # global says the opposite
    conn = driver.connect(os.environ["M3_PRIMARY_PG_URL"], connect_timeout=10)
    try:
        assert dialect_for_connection(conn).param() == "%s"
        assert dialect_for_connection(conn.cursor()).param() == "%s"
    finally:
        conn.close()
