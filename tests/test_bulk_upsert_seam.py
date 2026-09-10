"""`bulk_upsert` — the batched insert-or-update seam primitive.

The behavioural half of conformance. `tests/test_backend_conformance.py` proves
a backend HAS the method (the Protocol is runtime_checkable, so name presence is
automatic), but it cannot prove the two implementations AGREE — its behavioural
layer only covers search. Divergence in a merge primitive is the worst kind:
both backends "work", and rows quietly differ.

The SQLite half runs everywhere. The PostgreSQL half is a `requires_pg` sibling
in test_bulk_upsert_seam_pg_live.py, so the same contract is asserted against a
real cluster rather than a mock that agrees with whatever we wrote.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

from memory.backends.sqlite_backend import SqliteBackend  # noqa: E402

_COLS = ["id", "val", "updated_at"]
_LWW_GUARD = "WHERE t.updated_at IS NULL OR excluded.updated_at > t.updated_at"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE t (id TEXT PRIMARY KEY, val TEXT, updated_at TEXT)")
    yield c
    c.close()


def _rows(c):
    return dict(c.execute("SELECT id, val FROM t").fetchall())


def test_inserts_new_rows(conn):
    n = SqliteBackend().bulk_upsert(
        conn, "t", _COLS, [("a", "v1", "2026-01-01"), ("b", "v1", "2026-01-01")],
        conflict_target="(id)", update_columns=["val", "updated_at"],
    )
    assert n == 2
    assert _rows(conn) == {"a": "v1", "b": "v1"}


def test_updates_on_conflict(conn):
    b = SqliteBackend()
    b.bulk_upsert(conn, "t", _COLS, [("a", "v1", "2026-01-01")],
                  conflict_target="(id)", update_columns=["val", "updated_at"])
    b.bulk_upsert(conn, "t", _COLS, [("a", "v2", "2026-02-02")],
                  conflict_target="(id)", update_columns=["val", "updated_at"])
    assert _rows(conn) == {"a": "v2"}


def test_guard_rejects_older_and_accepts_newer(conn):
    """Last-write-wins, the shape every sync_* caller depends on.

    A guard that silently did nothing would turn a conditional merge into an
    unconditional overwrite — the same rows, quietly wrong, which is why this
    asserts BOTH directions in one call rather than only the happy one.
    """
    b = SqliteBackend()
    b.bulk_upsert(conn, "t", _COLS,
                  [("a", "keep", "2026-01-01"), ("b", "keep", "2026-01-01")],
                  conflict_target="(id)", update_columns=["val", "updated_at"])
    b.bulk_upsert(conn, "t", _COLS,
                  [("a", "OLDER", "2025-01-01"), ("b", "NEWER", "2026-06-06")],
                  conflict_target="(id)", update_columns=["val", "updated_at"],
                  guard_sql=_LWW_GUARD)
    assert _rows(conn) == {"a": "keep", "b": "NEWER"}


def test_version_precedence_guard_also_works(conn):
    """`sync_secrets` resolves conflicts by VERSION, not timestamp.

    Pinned because it is the reason `guard_sql` is a pass-through fragment
    rather than an LWW-shaped parameter: a primitive that assumed `updated_at`
    would silently mis-merge the highest-value rows in the store.
    """
    c = conn
    c.execute("CREATE TABLE s (name TEXT PRIMARY KEY, secret TEXT, version INTEGER)")
    b = SqliteBackend()
    cols = ["name", "secret", "version"]
    b.bulk_upsert(c, "s", cols, [("k", "v1", 5)],
                  conflict_target="(name)", update_columns=["secret", "version"])
    b.bulk_upsert(c, "s", cols, [("k", "stale", 3)],
                  conflict_target="(name)", update_columns=["secret", "version"],
                  guard_sql="WHERE excluded.version > s.version")
    assert c.execute("SELECT secret FROM s").fetchone()[0] == "v1"
    b.bulk_upsert(c, "s", cols, [("k", "fresh", 9)],
                  conflict_target="(name)", update_columns=["secret", "version"],
                  guard_sql="WHERE excluded.version > s.version")
    assert c.execute("SELECT secret FROM s").fetchone()[0] == "fresh"


def test_empty_rows_is_a_noop(conn):
    """No rows must not become `VALUES ()` — a syntax error on both engines."""
    assert SqliteBackend().bulk_upsert(
        conn, "t", _COLS, [], conflict_target="(id)", update_columns=["val"]
    ) == 0


def test_table_is_not_schema_qualified():
    """Guards reference the UNQUALIFIED table name.

    Every current caller writes `WHERE {table}.{col} …`. If an implementation
    silently qualified the target, the guard would stop matching and every merge
    would become an unconditional overwrite — data loss with no error.
    """
    import inspect

    for mod in ("sqlite_backend", "postgres_backend"):
        src = inspect.getsource(
            __import__(f"memory.backends.{mod}", fromlist=[mod])
        )
        start = src.index("def bulk_upsert")
        body = src[start:src.index("def maintenance_checkpoint", start)]
        assert "INSERT INTO {table}" in body, f"{mod} must use the table name as given"


def test_postgres_does_not_use_executemany():
    """psycopg2's executemany runs the statement ONCE PER ROW.

    Measured on a live cluster over a local bridge: 26.7x slower than
    execute_values for 3000 rows, and that gap widens with network latency —
    on a remote CDW it is the difference between a working sync and one that
    trips the 600s timeout. A refactor that "simplified" this to executemany
    would pass every other test in this file.

    Asserted on the AST, not the source text: the method's own docstring warns
    against executemany by name, and a substring check fails on that warning —
    it cannot tell CODE from PROSE ABOUT CODE. (Same trap as the repo's drift
    guard that tripped over a comment cautioning against an import.)
    """
    import ast
    import inspect
    import textwrap

    from memory.backends import postgres_backend

    src = textwrap.dedent(inspect.getsource(postgres_backend.PostgresBackend.bulk_upsert))
    called = {
        node.func.attr
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    named = {
        node.id for node in ast.walk(ast.parse(src)) if isinstance(node, ast.Name)
    }
    assert "execute_values" in named, "the PG path must batch via execute_values"
    assert "executemany" not in called, "executemany is one round trip per row"


class TestBackendForFactory:
    """`backend_for(uri)` — addressing a SPECIFIC store, not the configured one.

    `active_backend()` is singular and memoized by design; sync needs two stores
    open at once. This is the one place that maps a URI shape to a backend, so a
    third backend registers here rather than in every caller.
    """

    def test_postgres_dsn_both_schemes(self):
        from memory.backends.postgres_backend import PostgresBackend
        from memory.backends.selector import backend_for

        for uri in ("postgresql://u@h/db", "postgres://u@h/db"):
            assert isinstance(backend_for(uri), PostgresBackend)

    def test_sqlite_path_refuses_rather_than_returning_the_wrong_store(self):
        """The footgun this factory could easily have shipped.

        SqliteBackend has no per-instance path — connection() delegates to the
        process-wide configured store — so returning one "for" /some/other.db
        would silently operate on a DIFFERENT database with no error. That is
        precisely the failure mode the PG-local-sync work exists to remove, so
        the factory refuses until SqliteBackend can take a path.
        """
        from memory.backends.selector import backend_for

        with pytest.raises(NotImplementedError) as ctx:
            backend_for("/some/other.db")
        msg = str(ctx.value)
        assert "open_readonly" in msg and "active_database" in msg, (
            "the refusal must name the honest alternatives, not just decline"
        )

    def test_unknown_scheme_refuses(self):
        """A future backend registers its scheme HERE; it is not guessed."""
        from memory.backends.selector import backend_for

        with pytest.raises(ValueError):
            backend_for("mysql://u@h/db")

    def test_empty_uri_refuses(self):
        from memory.backends.selector import backend_for

        with pytest.raises(ValueError):
            backend_for("")
