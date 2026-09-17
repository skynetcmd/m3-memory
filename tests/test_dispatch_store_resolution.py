"""The dispatch store is a THIRD store, resolved through the paths seam.

m3 keeps three kinds of state with three lifetimes, and each gets its own store
so it can be handled on its own terms:

    agent_memory.db    permanent   durability, FTS, embeddings, GDPR erasure,
                                   backup, warehouse sync
    agent_chatlog.db   decaying    bulk ingest, decay, promotion to canonical
    agent_dispatch.db  EPHEMERAL   write churn, lease reclaim, pruning

These pin the resolution rules, the two wrong ones that have actually been made
in this codebase, and the fact that the store carries the lease columns the
shared primitives need.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from m3_core import paths as P  # noqa: E402
from memory.backends.sqlite_backend import SqliteDialect  # noqa: E402

_DISPATCH_ENV = ("M3_DISPATCH_DB", "M3_DISPATCH_MODE", "M3_DISPATCH_PG_SCHEMA")

# The seven lease columns 047 added. A table carrying these works with the
# shared primitives and needs no new code -- that inheritance is the whole
# architectural claim, so it is pinned rather than assumed.
_LEASE_COLUMNS = {
    "claimed_by", "claimed_at", "claim_expires_at",
    "lease_token", "attempt_count", "failed_at", "received_at",
}


class _CleanEnv(unittest.TestCase):
    """Each test starts from a known env, and restores it afterwards."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _DISPATCH_ENV}
        for k in _DISPATCH_ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestStoreResolution(_CleanEnv):
    def test_separate_is_the_default(self):
        """A delivery record is ephemeral; it does not belong in the file that
        is backed up, synced to the warehouse and erased for GDPR."""
        self.assertEqual(P.dispatch_store_mode(), "separate")
        self.assertTrue(P.resolve_dispatch_db().endswith("agent_dispatch.db"))

    def test_integrated_mode_uses_the_main_store(self):
        """Offered for a single-agent install where a second file is overhead."""
        os.environ["M3_DISPATCH_MODE"] = "integrated"
        self.assertEqual(P.dispatch_store_mode(), "integrated")
        self.assertTrue(P.resolve_dispatch_db().endswith("agent_memory.db"))

    def test_an_unrecognised_mode_falls_back_to_separate(self):
        """Never silently integrate on a typo: that would put ephemeral churn
        into the permanent store, which is the outcome the split prevents."""
        for bogus in ("hybrid", "Separate ", "", "yes"):
            with self.subTest(mode=bogus):
                os.environ["M3_DISPATCH_MODE"] = bogus
                self.assertTrue(
                    P.resolve_dispatch_db().endswith("agent_dispatch.db"),
                    f"observed: mode={bogus!r} resolved to the MAIN store\n"
                    "possible: the mode comparison widened beyond 'integrated'\n"
                    "inspect: dispatch_store_mode in bin/m3_core/paths.py",
                )

    def test_an_explicit_path_wins(self):
        os.environ["M3_DISPATCH_DB"] = os.path.join("custom", "x.db")
        self.assertEqual(P.resolve_dispatch_db(), os.path.join("custom", "x.db"))

    def test_hybrid_is_deliberately_not_a_mode(self):
        """The chatlog has integrated/separate/hybrid; dispatch takes the first
        two. Hybrid exists there because chatlog_promote copies rows into
        canonical memory, and a delivery record has no equivalent."""
        os.environ["M3_DISPATCH_MODE"] = "hybrid"
        self.assertEqual(P.dispatch_store_mode(), "separate")


class TestMigrationsDirIsCodeNotData(_CleanEnv):
    """DDL ships WITH THE CODE. Two wrong resolutions have been made here.

    `resolve_engine_file` points at <engine>/dispatch_migrations, which nothing
    populates -- the bug chatlog_config documents. `get_m3_root()` points at the
    relocatable M3_MEMORY_ROOT, which is the DATA root, not the code; that one
    was written and caught while building this feature, by this test.
    """

    def test_the_directory_actually_exists(self):
        d = P.dispatch_migrations_dir()
        self.assertTrue(
            os.path.isdir(d),
            f"the dispatch migrations directory does not exist.\n"
            f"observed: resolved to {d!r}\n"
            "possible: it was resolved against a DATA root (M3_MEMORY_ROOT or "
            "the engine root) rather than the installed code\n"
            "inspect: dispatch_migrations_dir in bin/m3_core/paths.py",
        )

    def test_it_holds_the_bootstrap_for_both_backends(self):
        d = pathlib.Path(P.dispatch_migrations_dir())
        self.assertTrue((d / "001_bootstrap.up.sql").is_file())
        self.assertTrue((d / "001_bootstrap.down.sql").is_file())
        self.assertTrue((d / "postgres" / "pg_001_bootstrap.up.sql").is_file())
        self.assertTrue((d / "postgres" / "pg_001_bootstrap.down.sql").is_file())

    def test_it_does_not_move_when_the_data_roots_move(self):
        """The engine root is relocatable; the code is not."""
        before = P.dispatch_migrations_dir()
        saved = os.environ.get("M3_ENGINE_ROOT")
        try:
            os.environ["M3_ENGINE_ROOT"] = os.path.join("somewhere", "else")
            self.assertEqual(P.dispatch_migrations_dir(), before)
        finally:
            if saved is None:
                os.environ.pop("M3_ENGINE_ROOT", None)
            else:
                os.environ["M3_ENGINE_ROOT"] = saved


class TestPgSchemaIsolation(_CleanEnv):
    def test_default_schema(self):
        self.assertEqual(P.dispatch_pg_schema(), "m3_dispatch")

    def test_per_fleet_override(self):
        """Several m3 fleets share one server by taking a schema each."""
        os.environ["M3_DISPATCH_PG_SCHEMA"] = "m3_fleet_a"
        self.assertEqual(P.dispatch_pg_schema(), "m3_fleet_a")


class TestBootstrapSchema(unittest.TestCase):
    """The bootstrap must produce a store the shared primitives can drive."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        up = pathlib.Path(P.dispatch_migrations_dir()) / "001_bootstrap.up.sql"
        self.conn.executescript(up.read_text(encoding="utf-8"))

    def tearDown(self):
        self.conn.close()

    def test_up_is_idempotent(self):
        up = pathlib.Path(P.dispatch_migrations_dir()) / "001_bootstrap.up.sql"
        self.conn.executescript(up.read_text(encoding="utf-8"))

    def test_down_returns_the_store_to_empty(self):
        down = pathlib.Path(P.dispatch_migrations_dir()) / "001_bootstrap.down.sql"
        self.conn.executescript(down.read_text(encoding="utf-8"))
        left = sorted(r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"))
        self.assertEqual(left, [], f"observed: tables left behind={left}")

    def test_it_carries_its_own_schema_versions(self):
        """A separate store tracks its own migrations, like the chatlog's."""
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='schema_versions'").fetchone())

    def test_the_lease_columns_are_present(self):
        cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(notification_dispatch)")}
        self.assertEqual(
            cols & _LEASE_COLUMNS, _LEASE_COLUMNS,
            "the dispatch store is missing lease columns.\n"
            f"observed: missing={sorted(_LEASE_COLUMNS - cols)}\n"
            "possible impact: claim/complete/fail/renew/sweep cannot run against "
            "it, and the tempting fix is a dispatch-specific copy -- the "
            "duplication the seam exists to prevent\n"
            "inspect: memory/dispatch_migrations/001_bootstrap.up.sql",
        )

    def test_the_lease_primitives_work_with_no_new_code(self):
        d = SqliteDialect()
        T = "notification_dispatch"
        self.conn.execute(
            f"INSERT INTO {T} (agent_id, kind) VALUES ('a@1','ping')")
        self.conn.commit()

        got = d.claim_message(self.conn, table=T, where_sql="agent_id = ?",
                              where_params=("a@1",), claimant="w1", lease_ttl=300)
        self.assertIsNotNone(got, "claim_message could not claim a dispatch row")
        rid, tok = got
        self.assertTrue(d.renew_lease(self.conn, table=T, row_id=rid, lease_token=tok))
        self.assertFalse(d.renew_lease(self.conn, table=T, row_id=rid,
                                       lease_token="wrong"),
                         "the fence accepted a foreign token")
        self.assertTrue(d.complete_message(self.conn, table=T, row_id=rid,
                                           lease_token=tok))
        self.assertFalse(d.complete_message(self.conn, table=T, row_id=rid,
                                            lease_token=tok),
                         "a completed row was completed twice")

    def test_the_hot_path_queries_use_an_index(self):
        """§8: the waiter runs one of these per agent every few seconds."""
        self.conn.executemany(
            "INSERT INTO notification_dispatch (agent_id, kind, claim_expires_at) "
            "VALUES (?,?,?)",
            [(f"a@{i % 5}", "ping", "2020-01-01T00:00:00Z") for i in range(500)])
        self.conn.commit()
        self.conn.execute("ANALYZE")

        d = SqliteDialect()
        for label, sql in (
            ("sweeper", "SELECT id FROM notification_dispatch WHERE "
                        + d.lease_expired_predicate()),
            ("waiter", "SELECT id FROM notification_dispatch "
                       "WHERE agent_id = 'a@1' AND read_at IS NULL"),
        ):
            with self.subTest(query=label):
                plan = " ".join(str(r[-1]) for r in self.conn.execute(
                    "EXPLAIN QUERY PLAN " + sql).fetchall())
                self.assertIn(
                    "idx_dispatch", plan,
                    f"the {label} query no longer uses an index.\n"
                    f"observed: plan={plan}\n"
                    "possible: an index was dropped, or its partial WHERE was "
                    "narrowed so the planner can no longer use it\n"
                    "inspect: memory/dispatch_migrations/001_bootstrap.up.sql",
                )


if __name__ == "__main__":
    unittest.main()
