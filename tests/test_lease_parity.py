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

import os
import re
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

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


_REQUIRED_FENCE_TERMS = {
    ("id", "="),
    ("lease_token", "="),
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
                "claimed_by", "lease_token", "id"):
        for op in ("IS NOT NULL", "IS NULL", "<=", ">=", "<", ">", "="):
            # `IS NOT NULL` must win over `IS NULL`, and `<=` over `<`.
            if re.search(rf"\b{col}\s+{re.escape(op)}", sql):
                if op == "IS NULL" and (col, "IS NOT NULL") in out:
                    continue
                if op == "<" and (col, "<=") in out:
                    continue
                out.add((col, op))
    return out


def _expired_sql(d) -> str:
    """The `expired` predicate as dialect instance ``d`` renders it.

    Now a CALL, not a source scrape. The predicate used to be inlined in
    ``sweep_expired_leases`` and hand-copied per backend, so the only way to
    compare the copies was to read them out of the source. It has since been
    collapsed into ``Dialect.lease_expired_predicate`` -- one owner, every
    backend calling it -- which is what §10a asks for and what makes the copies
    unable to drift.

    The term assertions below therefore no longer compare two copies. They pin
    the OWNER's terms, and prove every registered backend resolves to that one
    owner rather than reintroducing a local spelling.
    """
    return d.lease_expired_predicate()


def _registered_dialects() -> "list[tuple[str, object]]":
    """Every REGISTERED backend's dialect singleton, derived, never hardcoded.

    §1 expects MariaDB. A hardcoded (sqlite, postgres) pair would leave a third
    backend's override silently unguarded -- the same "they agree today" failure
    this file exists to catch, one level up. The registry is the documented
    single source of truth mapping a backend name to its dialect, so ask it.
    """
    from memory.backends import registry as _registry
    from memory.backends.selector import _VALID

    out = []
    for name in _VALID:
        _registry._ensure_registered(name)
        out.append((name, _registry.dialect_singleton_for(name)))
    return out


class TestExpiredPredicateHasOneOwner(unittest.TestCase):
    """Pure-function contract -- no database required."""

    def test_every_registered_backend_resolves_to_the_same_owner(self):
        """No backend may reintroduce a local spelling of `expired`.

        This replaces a copy-vs-copy comparison. The copies are gone; what must
        now hold is that nobody adds one back by overriding the owner.
        """
        owners = {
            name: type(d).lease_expired_predicate.__qualname__
            for name, d in _registered_dialects()
        }
        distinct = set(owners.values())
        self.assertEqual(
            len(distinct), 1,
            "a backend overrode the lease `expired` predicate.\n"
            f"observed: {owners}\n"
            "cause: lease_expired_predicate is defined in more than one place, "
            "so the definition of an expired lease can differ per backend\n"
            "possible impact: the >=lease_ttl retry gap, which the dispatch "
            "SSRF argument rests on, holds on one backend and not another\n"
            "inspect: lease_expired_predicate in bin/memory/backends/dialect.py "
            "and any backend module that redefines it",
        )

    def test_the_owner_keeps_every_required_term(self):
        """Pins the terms so dropping one is caught even with a single owner.

        One owner removes DRIFT between backends; it does not stop someone
        deleting a term from the owner itself.
        """
        for name, d in _registered_dialects():
            with self.subTest(backend=name):
                got = _terms(_expired_sql(d))
                missing = _REQUIRED_TERMS - got
                self.assertEqual(
                    got & _REQUIRED_TERMS, _REQUIRED_TERMS,
                    f"`expired` lost a required term on {name}.\n"
                    f"observed: missing={sorted(missing)}\n"
                    "cause: a term required by the lease contract is absent "
                    "from lease_expired_predicate\n"
                    "possible impact: dropping `failed_at IS NULL` makes a "
                    "dead-lettered row eligible for reclaim again -- the "
                    "infinite retry loop sweep_expired_leases exists to bound; "
                    "dropping `read_at IS NULL` reclaims completed work\n"
                    "inspect: Dialect.lease_expired_predicate",
                )

    def test_the_renderings_still_differ_per_backend(self):
        """The owner is a seam METHOD, not a constant -- prove it still varies.

        If these ever render identically the seam has stopped earning its place,
        and `now()` has probably regressed on one backend.
        """
        rendered = {name: _expired_sql(d) for name, d in _registered_dialects()}
        self.assertEqual(
            len(set(rendered.values())), len(rendered),
            f"observed: {rendered}\n"
            "cause: two backends rendered the same SQL for an expression that "
            "must be backend-specific (TEXT compare vs TIMESTAMPTZ)\n"
            "inspect: Dialect.now on each backend",
        )


class TestLeaseFencePredicate(unittest.TestCase):
    """The fence owner -- `complete`, `fail` and `renew` all render from it.

    `tests/test_lease_fencing.py` already proves the fence BEHAVES on SQLite
    (4 tests fail if the token check is removed). What it cannot show is that
    the predicate is the same on PostgreSQL, or that the owner keeps its terms
    -- it has no PG leg at all. These fill exactly that gap.
    """

    def test_every_registered_backend_resolves_to_the_same_owner(self):
        owners = {
            name: type(d).lease_fence_predicate.__qualname__
            for name, d in _registered_dialects()
        }
        self.assertEqual(
            len(set(owners.values())), 1,
            "a backend overrode the lease fence predicate.\n"
            f"observed: {owners}\n"
            "cause: lease_fence_predicate is defined in more than one place, so "
            "what counts as a valid attempt can differ per backend\n"
            "possible impact: a worker whose lease lapsed could complete or fail "
            "a row another agent now owns, which is the duplicate execution the "
            "fence exists\n"
            "inspect: lease_fence_predicate in bin/memory/backends/dialect.py",
        )

    def test_the_owner_keeps_every_fence_term(self):
        for name, d in _registered_dialects():
            with self.subTest(backend=name):
                got = _terms(d.lease_fence_predicate())
                self.assertEqual(
                    got & _REQUIRED_FENCE_TERMS, _REQUIRED_FENCE_TERMS,
                    f"the lease fence lost a required term on {name}.\n"
                    f"observed: missing={sorted(_REQUIRED_FENCE_TERMS - got)}\n"
                    "cause: a term required by the fence contract is absent from "
                    "lease_fence_predicate\n"
                    "possible impact: dropping `lease_token` lets a reclaimed "
                    "worker act on someone else's attempt; dropping `read_at` or "
                    "`failed_at` lets a terminal row be written again\n"
                    "inspect: Dialect.lease_fence_predicate",
                )

    def test_live_lease_only_adds_the_expiry_bound_and_nothing_else(self):
        """`renew_lease` needs the extra term; `complete`/`fail` must NOT have it.

        Expressed as a flag on one owner rather than a second predicate, so the
        difference is a named parameter instead of a diff between two strings in
        two methods. This pins that the flag is purely additive.
        """
        for name, d in _registered_dialects():
            with self.subTest(backend=name):
                basic = d.lease_fence_predicate()
                live = d.lease_fence_predicate(live_lease_only=True)
                self.assertTrue(
                    live.startswith(basic),
                    f"observed: live_lease_only rewrote the base fence on {name}\n"
                    "inspect: Dialect.lease_fence_predicate",
                )
                self.assertNotIn(
                    "claim_expires_at", basic,
                    f"observed: the default fence carries an expiry bound on {name}\n"
                    "possible impact: complete/fail would then refuse a row whose lease "
                    "lapsed mid-work, even though the token still matches\n"
                    "inspect: Dialect.lease_fence_predicate",
                )
                self.assertIn("claim_expires_at", live)


class TestExpiryComparisonIsPortable(unittest.TestCase):
    """The expiry bound compares TEXT on SQLite and TIMESTAMPTZ on PostgreSQL.

    On SQLite the whole correctness of `claim_expires_at <= now` rests on
    lexicographic order matching chronological order, which holds only while
    every timestamp is the SAME FIXED WIDTH. An unpadded month would sort after
    a padded one and a lapsed lease would read as live -- silently, on one
    backend only, which is the §0.4 hermeticity trap this file exists to close.

    Measured: `strftime('%Y-%m-%dT%H:%M:%SZ', ...)` always zero-pads, and
    `renew_lease` is the ONLY writer of the column (via `now_plus_seconds`), so
    nothing can introduce the unpadded form today. These pin that.
    """

    def test_sqlite_now_is_fixed_width_utc(self):
        """No `localtime` modifier: the same instant renders identically on
        Windows, macOS and Linux."""
        rendered = SqliteDialect().now()
        self.assertIn("%Y-%m-%dT%H:%M:%SZ", rendered)
        self.assertNotIn("localtime", rendered)

    def test_text_ordering_matches_time_ordering(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (id INTEGER, claim_expires_at TEXT, "
                     "read_at TEXT, failed_at TEXT)")
        conn.executemany(
            "INSERT INTO t (id, claim_expires_at) VALUES (?,?)",
            [(1, "2025-12-31T23:59:59Z"),   # last year, lapsed
             (2, "2026-09-16T00:00:00Z"),   # lapsed
             (3, "2099-01-01T00:00:00Z")],  # live
        )
        pred = SqliteDialect().lease_expired_predicate()
        got = [r[0] for r in conn.execute(
            f"SELECT id FROM t WHERE {pred} ORDER BY id").fetchall()]
        self.assertEqual(
            got, [1, 2],
            "lexicographic order no longer matches chronological order.\n"
            f"observed: expired={got} expected=[1, 2]\n"
            "possible: the timestamp format changed width, so a lapsed lease "
            "can sort as live\n"
            "inspect: Dialect.now on the SQLite backend",
        )
        conn.close()

    def test_strftime_zero_pads_so_the_unpadded_form_cannot_be_written(self):
        """The invariant above holds because the only writer cannot break it."""
        conn = sqlite3.connect(":memory:")
        got = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','2026-01-05 00:00:00')"
        ).fetchone()[0]
        self.assertEqual(got, "2026-01-05T00:00:00Z")
        conn.close()


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
