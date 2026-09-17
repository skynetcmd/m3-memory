"""Parity: the lease `expired` predicate agrees on every backend.

`sweep_expired_leases` decides which rows are reclaimable. Its `expired`
predicate is maintained in TWO HAND-EDITED COPIES with nothing forcing them to
agree:

    bin/memory/backends/dialect.py:552-555          (SQLite / base)
    bin/memory/backends/postgres_backend.py:544-547 (PostgreSQL)

They carry identical terms today. Add a term to one and the retry gap holds on
one backend and not the other -- with no error, on either. That is not merely a
§10a duplication defect: the ">= lease_ttl retry interval" property that the
dispatch design rests on becomes backend-dependent, so a security argument
silently stops holding on half the fleet. §0.4's hermeticity trap wearing §10a's
clothes.

`tests/test_lease_fencing.py` covers the mechanism but has ZERO PostgreSQL
coverage (`grep requires_pg|pg_live|postgres` -> 0 hits), so the divergence is
not just undefended by a comment -- it is untested by construction.

⚠ TERM-WISE, NOT STRING-WISE. The two renderings are DELIBERATELY different
text and must stay comparable anyway:

    SQLite  claim_expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now')   (text compare)
    PG      claim_expires_at <= NOW()                                   (timestamptz compare)

A string equality check would fail on correct code, which is worse than no test:
it trains the next author to delete the guard. So the assertions below compare
the SET OF TERMS and prove the BEHAVIOUR, never the SQL text.

The live-PG leg follows `test_schema_parity_pg_live.py` and
`test_date_bound_parity.py`: it SKIPS when no PG URL is configured rather than
failing, so the suite stays green on a SQLite-only box while still proving
portability wherever PG is reachable.
"""
from __future__ import annotations

import inspect
import os
import re
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from memory.backends.dialect import Dialect  # noqa: E402
from memory.backends.postgres_backend import PostgresDialect  # noqa: E402
from memory.backends.sqlite_backend import SqliteDialect  # noqa: E402

# The table the dispatch design will add (D10). Named here so the guard is
# explicit about WHICH table it exercises: `notifications` and
# `notification_dispatch` differ by a suffix, and six hand-written predicates
# against the former already exist (plan P3). A test that accepts whatever
# table it is handed cannot catch a query aimed at the wrong one.
_TABLE = "notifications"

# The seven lease columns migration 047 added, plus the two the predicate reads.
_LEASE_DDL = """
CREATE TABLE {t} (
    id              INTEGER PRIMARY KEY,
    agent_id        TEXT,
    read_at         TEXT,
    received_at     TEXT,
    claimed_by      TEXT,
    claimed_at      TEXT,
    claim_expires_at TEXT,
    lease_token     TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    failed_at       TEXT
)
"""

# Every term the `expired` predicate MUST contain, as (column, operator) pairs
# normalised away from backend-specific rendering. Derived by reading both
# implementations; asserted against both so adding a term to one is caught.
_REQUIRED_TERMS = {
    ("claim_expires_at", "IS NOT NULL"),
    ("claim_expires_at", "<="),
    ("read_at", "IS NULL"),
    ("failed_at", "IS NULL"),
}


def _terms(sql: str) -> set:
    """The (column, operator) pairs in a predicate, ignoring rendering.

    Deliberately NOT a string compare: `strftime(...)` and `NOW()` are correct
    divergence. What must agree is WHICH COLUMNS are constrained and HOW.
    """
    out = set()
    for col in ("claim_expires_at", "read_at", "failed_at", "attempt_count",
                "claimed_by", "lease_token"):
        for op in ("IS NOT NULL", "IS NULL", "<=", ">=", "<", ">", "="):
            # `IS NOT NULL` must win over `IS NULL`, and `<=` over `<`.
            if re.search(rf"\b{col}\s+{re.escape(op)}", sql):
                if op == "IS NULL" and (col, "IS NOT NULL") in out:
                    continue
                if op == "<" and (col, "<=") in out:
                    continue
                out.add((col, op))
    return out


def _expired_sql(cls) -> str:
    """The `expired` predicate READ FROM THE SOURCE of ``cls.sweep_expired_leases``.

    ⚠ Read, never transcribed. The first version of this file hand-copied the
    predicate into the test, which made the guard BLIND: planting an extra term
    in `postgres_backend.py` left all 15 assertions green, because the test was
    comparing its own two copies rather than the code's. §12c -- a guard that
    cannot demonstrate a catch reads as coverage while providing none.

    The predicate is an inline local inside the method (that inlining IS the
    defect under test), so there is nothing importable to assert against;
    reading the source is what makes the assertion real.
    """
    src = inspect.getsource(cls.sweep_expired_leases)
    m = re.search(r"expired\s*=\s*\((.*?)\)" + chr(10), src, re.S)
    if not m:
        raise AssertionError(
            f"could not locate the `expired = (...)` literal in "
            f"{cls.__name__}.sweep_expired_leases. If the predicate moved or was "
            f"refactored into a shared owner, UPDATE THIS EXTRACTOR -- do not "
            f"delete the guard: a silently unextractable predicate is exactly "
            f"the blind spot this file exists to close."
        )
    return " ".join(re.findall(r'[fr]?"([^"]*)"', m.group(1))).strip()


class TestExpiredPredicateTerms(unittest.TestCase):
    """Pure-function contract -- no database required."""

    def test_both_backends_constrain_the_same_columns(self):
        sqlite_terms = _terms(_expired_sql(Dialect))
        pg_terms = _terms(_expired_sql(PostgresDialect))
        self.assertEqual(
            sqlite_terms, pg_terms,
            "the `expired` predicate diverged between backends.\n"
            f"observed: sqlite-only={sorted(sqlite_terms - pg_terms)} "
            f"pg-only={sorted(pg_terms - sqlite_terms)}\n"
            "cause: the predicate is maintained in two hand-edited copies and "
            "they no longer carry identical terms\n"
            "possible impact: the >=lease_ttl retry gap, which the dispatch "
            "SSRF argument rests on, now holds on one backend and not the "
            "other -- with no error on either\n"
            "inspect: bin/memory/backends/dialect.py sweep_expired_leases, "
            "bin/memory/backends/postgres_backend.py sweep_expired_leases",
        )

    def test_every_required_term_is_present(self):
        """Pins the terms themselves, so DROPPING one from BOTH copies is caught.

        Equality between the two copies is not sufficient: deleting
        `failed_at IS NULL` from both keeps them equal and makes a dead-lettered
        row eligible for reclaim again -- the infinite-retry loop the
        implementation's own docstring warns about.
        """
        for d, name in ((Dialect, "sqlite/base"), (PostgresDialect, "postgres")):
            with self.subTest(backend=name):
                self.assertEqual(
                    _terms(_expired_sql(d)) & _REQUIRED_TERMS, _REQUIRED_TERMS,
                    f"`expired` lost a required term on {name}.\n"
                    f"observed: missing={sorted(_REQUIRED_TERMS - _terms(_expired_sql(d)))}\n"
                    "cause: a term required by the lease contract is absent "
                    "from the predicate source\n"
                    "possible impact: dropping `failed_at IS NULL` makes a "
                    "dead-lettered row eligible for reclaim again -- the "
                    "infinite retry loop the implementation docstring warns "
                    "about; dropping `read_at IS NULL` reclaims completed work\n"
                    f"inspect: {d.__module__}.{d.__name__}.sweep_expired_leases",
                )

    def test_the_renderings_are_allowed_to_differ(self):
        """Guards the guard: a string compare here would fail on CORRECT code.

        If someone 'simplifies' this file into an equality check on the SQL
        text, this test documents why that is wrong.
        """
        self.assertNotEqual(
            _expired_sql(Dialect), _expired_sql(PostgresDialect),
            "rendering is expected to differ (strftime vs NOW()); only TERMS "
            "must match. If these ever render identically, this assertion is "
            "safe to delete -- but do not replace the term comparison with a "
            "string comparison.",
        )


class TestSweepExpiredLeasesSqlite(unittest.TestCase):
    """Behaviour, not text: run the real sweeper against a real table."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(_LEASE_DDL.format(t=_TABLE))
        self.d = SqliteDialect()

    def tearDown(self):
        self.conn.close()

    def _insert(self, rid, *, expires, read_at=None, failed_at=None, attempts=0):
        self.conn.execute(
            f"INSERT INTO {_TABLE} (id, agent_id, claimed_by, lease_token, "
            f"claim_expires_at, read_at, failed_at, attempt_count) "
            f"VALUES (?,?,?,?,?,?,?,?)",
            (rid, "a@1", "worker", "tok", expires, read_at, failed_at, attempts),
        )
        self.conn.commit()

    def test_an_expired_lease_is_reclaimed(self):
        self._insert(1, expires="2000-01-01T00:00:00Z")
        back, dead = self.d.sweep_expired_leases(self.conn, table=_TABLE)
        self.assertEqual((back, dead), (1, 0))
        row = self.conn.execute(
            f"SELECT claimed_by, lease_token FROM {_TABLE} WHERE id=1").fetchone()
        self.assertEqual(row, (None, None), "reclaim must clear the fence")

    def test_a_live_lease_is_untouched(self):
        self._insert(1, expires="2999-01-01T00:00:00Z")
        self.assertEqual(self.d.sweep_expired_leases(self.conn, table=_TABLE), (0, 0))

    def test_a_completed_row_is_never_reclaimed(self):
        """`read_at IS NULL` -- the term shared with six other call sites."""
        self._insert(1, expires="2000-01-01T00:00:00Z", read_at="2026-01-01T00:00:00Z")
        self.assertEqual(self.d.sweep_expired_leases(self.conn, table=_TABLE), (0, 0))

    def test_a_dead_lettered_row_is_never_reclaimed(self):
        """`failed_at IS NULL` -- without it, fail_message stops being terminal
        and the row re-enters the queue forever."""
        self._insert(1, expires="2000-01-01T00:00:00Z", failed_at="2026-01-01T00:00:00Z")
        self.assertEqual(self.d.sweep_expired_leases(self.conn, table=_TABLE), (0, 0))

    def test_max_attempts_dead_letters_instead_of_requeueing(self):
        self._insert(1, expires="2000-01-01T00:00:00Z", attempts=5)
        back, dead = self.d.sweep_expired_leases(self.conn, table=_TABLE, max_attempts=5)
        self.assertEqual((back, dead), (0, 1))
        self.assertIsNotNone(
            self.conn.execute(
                f"SELECT failed_at FROM {_TABLE} WHERE id=1").fetchone()[0])

    def test_sweep_is_idempotent(self):
        """P1's resolution has every polling agent sweep opportunistically, so a
        second pass must be a no-op rather than double-counting."""
        self._insert(1, expires="2000-01-01T00:00:00Z")
        first = self.d.sweep_expired_leases(self.conn, table=_TABLE)
        second = self.d.sweep_expired_leases(self.conn, table=_TABLE)
        self.assertEqual(first, (1, 0))
        self.assertEqual(second, (0, 0))


@unittest.skipUnless(
    os.environ.get("M3_PRIMARY_PG_URL") or os.environ.get("M3_CDW_PG_URL"),
    "no PostgreSQL URL configured (M3_PRIMARY_PG_URL / M3_CDW_PG_URL)",
)
class TestSweepExpiredLeasesPostgresLive(unittest.TestCase):
    """The leg that does not exist today.

    `claim_expires_at` is TIMESTAMPTZ here and TEXT on SQLite, and the bound is
    `NOW()` rather than a formatted string. Same predicate, two engines: the
    only way to know they agree is to run both.
    """

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
        cur.execute("DROP TABLE IF EXISTS _m3_lease_parity")
        cur.execute(
            "CREATE TABLE _m3_lease_parity ("
            " id int PRIMARY KEY, agent_id text, read_at timestamptz,"
            " received_at timestamptz, claimed_by text, claimed_at timestamptz,"
            " claim_expires_at timestamptz, lease_token text,"
            " attempt_count int NOT NULL DEFAULT 0, failed_at timestamptz)")
        self.conn.commit()
        self.d = PostgresDialect()

    def tearDown(self):
        cur = self.conn.cursor()
        cur.execute("DROP TABLE IF EXISTS _m3_lease_parity")
        self.conn.commit()
        self.conn.close()

    def _insert(self, rid, *, expires, read_at=None, failed_at=None, attempts=0):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO _m3_lease_parity (id, agent_id, claimed_by, lease_token,"
            " claim_expires_at, read_at, failed_at, attempt_count)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (rid, "a@1", "worker", "tok", expires, read_at, failed_at, attempts))
        self.conn.commit()

    def test_an_expired_lease_is_reclaimed(self):
        self._insert(1, expires="2000-01-01T00:00:00Z")
        back, dead = self.d.sweep_expired_leases(self.conn, table="_m3_lease_parity")
        self.assertEqual((back, dead), (1, 0))

    def test_a_live_lease_is_untouched(self):
        self._insert(1, expires="2999-01-01T00:00:00Z")
        self.assertEqual(
            self.d.sweep_expired_leases(self.conn, table="_m3_lease_parity"), (0, 0))

    def test_a_completed_row_is_never_reclaimed(self):
        self._insert(1, expires="2000-01-01T00:00:00Z", read_at="2026-01-01T00:00:00Z")
        self.assertEqual(
            self.d.sweep_expired_leases(self.conn, table="_m3_lease_parity"), (0, 0))

    def test_a_dead_lettered_row_is_never_reclaimed(self):
        self._insert(1, expires="2000-01-01T00:00:00Z", failed_at="2026-01-01T00:00:00Z")
        self.assertEqual(
            self.d.sweep_expired_leases(self.conn, table="_m3_lease_parity"), (0, 0))

    def test_max_attempts_dead_letters_instead_of_requeueing(self):
        self._insert(1, expires="2000-01-01T00:00:00Z", attempts=5)
        back, dead = self.d.sweep_expired_leases(
            self.conn, table="_m3_lease_parity", max_attempts=5)
        self.assertEqual((back, dead), (0, 1))

    def test_sweep_is_idempotent(self):
        self._insert(1, expires="2000-01-01T00:00:00Z")
        first = self.d.sweep_expired_leases(self.conn, table="_m3_lease_parity")
        second = self.d.sweep_expired_leases(self.conn, table="_m3_lease_parity")
        self.assertEqual(first, (1, 0))
        self.assertEqual(second, (0, 0))


if __name__ == "__main__":
    unittest.main()
