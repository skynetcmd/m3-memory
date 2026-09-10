"""`bulk_upsert` on a LIVE PostgreSQL cluster — the parity half.

tests/test_bulk_upsert_seam.py pins the contract on SQLite. This asserts the
SAME contract on real PostgreSQL, because a merge primitive that diverges
between backends is the worst kind of bug: both stores "work", and their rows
quietly disagree. A mock cannot catch that — it agrees with whatever we wrote.

Also measures the batching claim rather than trusting it. psycopg2's
`cursor.executemany` runs the statement once per row; `execute_values` expands
one multi-row statement. The primitive exists FOR that difference, so the
difference is asserted.

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


def test_bulk_upsert_is_materially_faster_than_executemany(pg):
    """The reason this primitive exists, measured rather than asserted.

    psycopg2's executemany issues one round trip per row. Over the local WSL
    bridge this measured ~27x for 3000 rows; over a real network the gap widens
    with latency. The threshold here is deliberately loose (>3x) — the point is
    to catch a regression to per-row behaviour, not to pin a machine-specific
    number, which would be a flaky test on someone else's hardware.
    """
    import time

    backend, table = pg
    rows = [(f"p{i}", f"v{i}", "2026-01-01") for i in range(2000)]
    sql = (
        f"INSERT INTO {table} (id, val, updated_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET val = EXCLUDED.val"
    )
    with backend.connection() as conn:
        cur = conn.cursor()
        t0 = time.perf_counter()
        backend.bulk_upsert(conn, table, _COLS, rows,
                            conflict_target="(id)", update_columns=["val"])
        bulk = time.perf_counter() - t0

        cur.execute(f"DELETE FROM {table}")
        t0 = time.perf_counter()
        cur.executemany(sql, rows)
        many = time.perf_counter() - t0
        conn.commit()

    assert bulk > 0
    assert many / bulk > 3.0, (
        f"bulk_upsert {bulk:.3f}s vs executemany {many:.3f}s — the batching "
        "advantage has regressed; check that execute_values is still in use"
    )
