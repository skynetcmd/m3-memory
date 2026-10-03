"""`bulk_upsert` on a LIVE PostgreSQL cluster — the parity half.

tests/test_bulk_upsert_seam.py pins the contract on SQLite. This asserts the
SAME contract on real PostgreSQL, because a merge primitive that diverges
between backends is the worst kind of bug: both stores "work", and their rows
quietly disagree. A mock cannot catch that — it agrees with whatever we wrote.

Also checks the batching claim rather than trusting it. psycopg2's
`cursor.executemany` runs the statement once per row; `execute_values` expands
one multi-row statement. The primitive exists FOR that difference, so the
statement count is asserted.

Skips cleanly without a reachable cluster. DSN from M3_PRIMARY_PG_URL/M3_PG_URL
(never PG_URL -- that is the warehouse var).
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

pytestmark = pytest.mark.requires_pg

_COLS = ["id", "val", "updated_at"]


@pytest.fixture()
def pg(pg_url):
    """A live backend plus a throwaway table, dropped afterwards.

    A unique table name per test so a shared dev cluster can run these
    concurrently without cross-talk.
    """
    from memory.backends.postgres_backend import PostgresBackend

    backend = PostgresBackend(dsn=pg_url)
    table = f"t_bulk_{uuid.uuid4().hex[:8]}"
    with backend.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"CREATE TABLE {table} "
            "(id TEXT PRIMARY KEY, val TEXT, updated_at TEXT)"
        )
        conn.commit()
    try:
        yield backend, table
    finally:
        try:
            with backend.connection() as conn:
                conn.cursor().execute(f"DROP TABLE IF EXISTS {table}")
                conn.commit()
        finally:
            backend.close()


def _rows(backend, table):
    with backend.connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT id, val FROM {table} ORDER BY id")
        return dict(cur.fetchall())


def test_insert_and_conflict_update(pg):
    backend, table = pg
    with backend.connection() as conn:
        n = backend.bulk_upsert(
            conn, table, _COLS,
            [("a", "v1", "2026-01-01"), ("b", "v1", "2026-01-01")],
            conflict_target="(id)", update_columns=["val", "updated_at"],
        )
        conn.commit()
    assert n == 2
    assert _rows(backend, table) == {"a": "v1", "b": "v1"}

    with backend.connection() as conn:
        backend.bulk_upsert(
            conn, table, _COLS, [("a", "v2", "2026-02-02")],
            conflict_target="(id)", update_columns=["val", "updated_at"],
        )
        conn.commit()
    assert _rows(backend, table) == {"a": "v2", "b": "v1"}


def test_guard_rejects_older_accepts_newer(pg):
    """Identical assertion to the SQLite test — that is the point.

    Same inputs, same expected outcome, different engine. If these two ever
    disagree, sync moves different rows depending on the backend.
    """
    backend, table = pg
    guard = (
        f"WHERE {table}.updated_at IS NULL "
        f"OR EXCLUDED.updated_at > {table}.updated_at"
    )
    with backend.connection() as conn:
        backend.bulk_upsert(
            conn, table, _COLS,
            [("a", "keep", "2026-01-01"), ("b", "keep", "2026-01-01")],
            conflict_target="(id)", update_columns=["val", "updated_at"],
        )
        backend.bulk_upsert(
            conn, table, _COLS,
            [("a", "OLDER", "2025-01-01"), ("b", "NEWER", "2026-06-06")],
            conflict_target="(id)", update_columns=["val", "updated_at"],
            guard_sql=guard,
        )
        conn.commit()
    assert _rows(backend, table) == {"a": "keep", "b": "NEWER"}


def test_lowercase_excluded_also_accepted(pg):
    """PostgreSQL accepts `excluded` as well as `EXCLUDED`.

    `guard_sql` is passed through verbatim, and the two existing call sites in
    pg_sync spell it differently (`excluded` on the SQLite arm, `EXCLUDED` on
    the PG arm). Pinning this is what lets one fragment serve both.
    """
    backend, table = pg
    guard = f"WHERE excluded.updated_at > {table}.updated_at"
    with backend.connection() as conn:
        backend.bulk_upsert(conn, table, _COLS, [("a", "first", "2026-01-01")],
                            conflict_target="(id)", update_columns=["val", "updated_at"])
        backend.bulk_upsert(conn, table, _COLS, [("a", "second", "2026-05-05")],
                            conflict_target="(id)", update_columns=["val", "updated_at"],
                            guard_sql=guard)
        conn.commit()
    assert _rows(backend, table) == {"a": "second"}


def test_empty_rows_is_a_noop(pg):
    """`VALUES ()` is a syntax error; an empty batch must not reach the server."""
    backend, table = pg
    with backend.connection() as conn:
        assert backend.bulk_upsert(
            conn, table, _COLS, [], conflict_target="(id)", update_columns=["val"]
        ) == 0
        conn.commit()
    assert _rows(backend, table) == {}


def test_batches_beyond_one_page(pg):
    """execute_values pages internally; more rows than a page must all land.

    Guards the off-by-one class where only the first page is written and the
    call still reports the full count.
    """
    from memory.backends import postgres_backend

    backend, table = pg
    n_rows = postgres_backend._UPSERT_PAGE_SIZE * 2 + 7
    rows = [(f"id{i}", f"v{i}", "2026-01-01") for i in range(n_rows)]
    with backend.connection() as conn:
        sent = backend.bulk_upsert(conn, table, _COLS, rows,
                                   conflict_target="(id)", update_columns=["val"])
        conn.commit()
    assert sent == n_rows
    with backend.connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        assert cur.fetchone()[0] == n_rows


def test_bulk_upsert_sends_one_statement_per_page(pg):
    """The reason this primitive exists: one multi-row statement per page, not
    one per row. Counted at the driver, not timed.

    ⚠ A wall-clock ratio (bulk vs executemany > 3x) used to stand here. Its gap
    is round-trip latency, so it held over TCP (~27x over the WSL bridge) and
    failed 5/5 over a local Unix socket (~1.5x) on unchanged code. Counting the
    statements the cursor sends is latency-independent and still fails on a
    regression to per-row behaviour.
    """
    import math

    import psycopg2.extensions
    from memory.backends import postgres_backend

    sent: list[int] = []

    class _CountingCursor(psycopg2.extensions.cursor):
        def execute(self, query, vars=None):  # noqa: A002 - psycopg2's name
            sent.append(1)
            return super().execute(query, vars)

    backend, table = pg
    n_rows = 2000
    rows = [(f"p{i}", f"v{i}", "2026-01-01") for i in range(n_rows)]
    with backend.connection() as conn:
        raw = conn._raw
        saved = raw.cursor_factory
        raw.cursor_factory = _CountingCursor
        try:
            backend.bulk_upsert(conn, table, _COLS, rows,
                                conflict_target="(id)", update_columns=["val"])
        finally:
            raw.cursor_factory = saved  # a pooled connection outlives the test

    pages = math.ceil(n_rows / postgres_backend._UPSERT_PAGE_SIZE)
    assert len(sent) == pages, (
        f"bulk_upsert sent {len(sent)} statements for {n_rows} rows; expected "
        f"{pages} (page size {postgres_backend._UPSERT_PAGE_SIZE}). The batching "
        "has regressed; check that execute_values is still in use"
    )
    with backend.connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        assert cur.fetchone()[0] == n_rows
