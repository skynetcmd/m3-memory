"""Parity: `Dialect.date_bound` / `normalize_date_bound` agree on every backend.

Regression for the 2026-09-06 `until=<bare date>` bug: comparing a bare
`YYYY-MM-DD` against a timestamp column silently excluded the requested day on
SQLite (lexicographic: 'T' sorts after '') and kept only the midnight row on
PostgreSQL (bare date casts to 00:00:00). Same wrong query, two different wrong
answers, neither obviously broken in isolation.

The live-PG leg follows `test_schema_parity_pg_live.py`: it SKIPS when no PG URL
is configured rather than failing, so the suite stays green on a SQLite-only
box while still proving portability wherever PG is reachable.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from memory.backends.dialect import Dialect  # noqa: E402

# Three rows on 2026-09-06 (incl. one at 23:59:59.9995 — the sub-millisecond
# row an inclusive '...23:59:59.999Z' bound would silently drop on PG) and one
# on 2026-09-07. A correct `until=2026-09-06` returns exactly 3.
_ROWS = [
    (1, "2026-09-06T00:00:00Z"),
    (2, "2026-09-06T02:01:29Z"),
    (3, "2026-09-06T23:59:59.9995Z"),
    (4, "2026-09-07T00:00:00Z"),
]
_EXPECT_UNTIL_0906 = 3
_EXPECT_SINCE_0906 = 4


class TestNormalizeDateBound(unittest.TestCase):
    """Pure-function contract — no database required."""

    def test_bare_date_expands_half_open(self):
        self.assertEqual(
            Dialect.normalize_date_bound("2026-09-06", "since"), "2026-09-06T00:00:00Z")
        self.assertEqual(
            Dialect.normalize_date_bound("2026-09-06", "until"), "2026-09-07T00:00:00Z")

    def test_operators_pair_with_normalization(self):
        self.assertEqual(Dialect.date_bound_op("since"), ">=")
        self.assertEqual(Dialect.date_bound_op("until"), "<")

    def test_month_and_year_rollover(self):
        self.assertEqual(
            Dialect.normalize_date_bound("2026-01-31", "until"), "2026-02-01T00:00:00Z")
        self.assertEqual(
            Dialect.normalize_date_bound("2026-12-31", "until"), "2027-01-01T00:00:00Z")
        # leap day: 2028 is a leap year
        self.assertEqual(
            Dialect.normalize_date_bound("2028-02-28", "until"), "2028-02-29T00:00:00Z")

    def test_full_timestamp_passes_through(self):
        """A caller-supplied precise instant keeps exact semantics."""
        for v in ("2026-09-06T02:01:29Z", "2026-09-06T02:01:29.123456+00:00"):
            self.assertEqual(Dialect.normalize_date_bound(v, "until"), v)

    def test_empty_passes_through(self):
        self.assertEqual(Dialect.normalize_date_bound("", "since"), "")

    def test_bad_side_raises(self):
        """Fail loud on a typo'd side rather than guessing a direction."""
        with self.assertRaises(ValueError):
            Dialect.normalize_date_bound("2026-09-06", "befor")
        with self.assertRaises(ValueError):
            Dialect.date_bound_op("after")


class TestDateBoundSqlite(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE t (id INTEGER, created_at TEXT)")
        self.conn.executemany("INSERT INTO t VALUES (?,?)", _ROWS)

    def tearDown(self):
        self.conn.close()

    def _count(self, side, value):
        d = Dialect(backend="sqlite", param_style="qmark")
        sql = f"SELECT COUNT(*) FROM t WHERE {d.date_bound('created_at', side)}"
        return self.conn.execute(
            sql, (d.normalize_date_bound(value, side),)).fetchone()[0]

    def test_until_includes_whole_day(self):
        self.assertEqual(self._count("until", "2026-09-06"), _EXPECT_UNTIL_0906)

    def test_since_includes_whole_day(self):
        self.assertEqual(self._count("since", "2026-09-06"), _EXPECT_SINCE_0906)

    def test_legacy_bare_date_was_broken(self):
        """Pin the old behaviour so the regression can't silently return."""
        n = self.conn.execute(
            "SELECT COUNT(*) FROM t WHERE created_at <= ?", ("2026-09-06",)).fetchone()[0]
        self.assertEqual(n, 0, "legacy lexicographic compare should drop the whole day")

    def test_index_is_still_usable(self):
        """A range bound must stay index-eligible (not degrade to a full scan)."""
        self.conn.execute("CREATE INDEX idx_t_created ON t(created_at)")
        d = Dialect(backend="sqlite", param_style="qmark")
        plan = self.conn.execute(
            f"EXPLAIN QUERY PLAN SELECT id FROM t WHERE {d.date_bound('created_at','until')}",
            (d.normalize_date_bound("2026-09-06", "until"),)).fetchall()
        self.assertIn("idx_t_created", " ".join(str(r[-1]) for r in plan))


@unittest.skipUnless(
    os.environ.get("M3_PRIMARY_PG_URL") or os.environ.get("M3_CDW_PG_URL"),
    "no PostgreSQL URL configured (M3_PRIMARY_PG_URL / M3_CDW_PG_URL)",
)
class TestDateBoundPostgresLive(unittest.TestCase):
    """The leg that matters: TIMESTAMPTZ has microsecond precision, so an
    inclusive '...23:59:59.999Z' bound (correct on SQLite) silently DROPS the
    23:59:59.9995 row here. Only the half-open form agrees on both."""

    @classmethod
    def setUpClass(cls):
        try:
            import psycopg  # noqa: F401
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("psycopg not installed")
        cls.url = os.environ.get("M3_PRIMARY_PG_URL") or os.environ["M3_CDW_PG_URL"]

    def setUp(self):
        import psycopg
        self.conn = psycopg.connect(self.url, connect_timeout=10)
        cur = self.conn.cursor()
        cur.execute("DROP TABLE IF EXISTS _m3_date_bound_parity")
        cur.execute("CREATE TABLE _m3_date_bound_parity (id int, created_at timestamptz)")
        cur.executemany("INSERT INTO _m3_date_bound_parity VALUES (%s,%s)", _ROWS)
        self.conn.commit()

    def tearDown(self):
        cur = self.conn.cursor()
        cur.execute("DROP TABLE IF EXISTS _m3_date_bound_parity")
        self.conn.commit()
        self.conn.close()

    def _count(self, side, value):
        d = Dialect(backend="postgres", param_style="format")
        sql = (f"SELECT COUNT(*) FROM _m3_date_bound_parity "
               f"WHERE {d.date_bound('created_at', side)}")
        cur = self.conn.cursor()
        cur.execute(sql, (d.normalize_date_bound(value, side),))
        return cur.fetchone()[0]

    def test_until_includes_whole_day(self):
        self.assertEqual(self._count("until", "2026-09-06"), _EXPECT_UNTIL_0906)

    def test_since_includes_whole_day(self):
        self.assertEqual(self._count("since", "2026-09-06"), _EXPECT_SINCE_0906)

    def test_legacy_bare_date_was_broken_differently(self):
        """PG's failure mode differs from SQLite's: the bare date casts to
        midnight, keeping exactly the 00:00:00 row instead of dropping all."""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM _m3_date_bound_parity WHERE created_at <= %s",
                    ("2026-09-06",))
        self.assertEqual(cur.fetchone()[0], 1)

    def test_inclusive_millisecond_bound_would_lose_a_row(self):
        """Why half-open, not '...23:59:59.999Z' — pins the design decision."""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM _m3_date_bound_parity WHERE created_at <= %s",
                    ("2026-09-06T23:59:59.999Z",))
        self.assertEqual(cur.fetchone()[0], 2, "sub-millisecond row silently dropped")


if __name__ == "__main__":
    unittest.main()
