"""`notify_impl` writes to the DISPATCH store, and nothing pending is stranded.

This is the first step that WRITES to the new store, so the risk changes shape:
a mistake here costs undelivered agent mail rather than a failed assertion.
Three properties are pinned:

  1. a new notification lands in the dispatch store, not the main one;
  2. a LEGACY row already sitting in `notifications` is still delivered, so the
     switch strands nobody's pending mail;
  3. the write table and the read sources agree -- if they ever diverge, mail is
     written where nothing looks for it and the inbox just stays empty, with no
     error anywhere.

Each test builds its own engine root, so they are order-independent and leave
no state behind.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from m3_core import paths as P  # noqa: E402

_MAIN_DDL = """
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT, kind TEXT,
    payload_json TEXT, created_at TEXT, read_at TEXT, received_at TEXT,
    claimed_by TEXT, claimed_at TEXT, claim_expires_at TEXT, lease_token TEXT,
    attempt_count INTEGER DEFAULT 0, failed_at TEXT);
CREATE TABLE agents (agent_id TEXT PRIMARY KEY, last_seen TEXT, status TEXT);
"""

# Matches the floor in memory/dispatch_migrations/001_bootstrap.up.sql. Pinned
# here so a change to one without the other fails rather than silently letting
# ids collide again.
_ID_FLOOR = 1_000_000_000


class _Store(unittest.TestCase):
    """A fresh engine root per test, with both stores bootstrapped."""

    INTEGRATED = False

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("M3_ENGINE_ROOT", "M3_DATABASE", "M3_DISPATCH_MODE")}
        root = tempfile.mkdtemp(prefix="m3-notify-test-")
        engine = os.path.join(root, "engine")
        os.makedirs(engine, exist_ok=True)
        os.environ["M3_ENGINE_ROOT"] = engine
        os.environ.pop("M3_DATABASE", None)
        if self.INTEGRATED:
            os.environ["M3_DISPATCH_MODE"] = "integrated"
        else:
            os.environ.pop("M3_DISPATCH_MODE", None)

        self.main = P.resolve_engine_file("agent_memory.db")
        conn = sqlite3.connect(self.main)
        conn.executescript(_MAIN_DDL)
        boot = (pathlib.Path(P.dispatch_migrations_dir())
                / "001_bootstrap.up.sql").read_text(encoding="utf-8")
        self.dispatch = P.resolve_dispatch_db()
        if self.INTEGRATED:
            conn.executescript(boot)     # same file, second table
            conn.commit()
            conn.close()
        else:
            conn.commit()
            conn.close()
            d = sqlite3.connect(self.dispatch)
            d.executescript(boot)
            d.commit()
            d.close()
        os.environ["M3_DATABASE"] = self.main

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _count(self, db_path, table):
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()


class TestSeparateStore(_Store):
    INTEGRATED = False

    def test_a_new_notification_lands_in_the_dispatch_store(self):
        from memory.orchestration import notify_impl
        notify_impl("probe@1", "handoff", {"task": "x"})

        self.assertEqual(
            self._count(self.dispatch, "notification_dispatch"), 1,
            "the notification did not reach the dispatch store.\n"
            "observed: 0 rows in notification_dispatch\n"
            "possible: the write went to the main store, or get_dispatch_conn "
            "fell back to the main pool\n"
            "inspect: _dispatch_write_table and M3Context.get_dispatch_conn",
        )
        self.assertEqual(
            self._count(self.main, "notifications"), 0,
            "the notification also reached the LEGACY table.\n"
            "observed: rows in notifications on a separate-store install\n"
            "possible: the write path still targets the old table\n"
            "inspect: notify_impl in bin/memory/orchestration.py",
        )

    def test_the_new_id_is_past_the_floor(self):
        """Ids must stay globally distinguishable: ack takes a bare integer."""
        from memory.orchestration import notifications_unread_ids_impl as unread
        from memory.orchestration import notify_impl
        notify_impl("probe@1", "handoff", {})
        ids = unread("probe@1")
        self.assertTrue(
            ids and all(i >= _ID_FLOOR for i in ids),
            f"observed: ids={ids} floor={_ID_FLOOR}\n"
            "possible: the sequence seed or the CHECK constraint was removed\n"
            "inspect: memory/dispatch_migrations/001_bootstrap.up.sql",
        )

    def test_a_legacy_unread_row_is_still_delivered(self):
        """The switch must strand nobody's pending mail.

        A row written before the split sits in `notifications`. If the read path
        stopped visiting that table, it would never be delivered and never be
        reported -- an inbox that is quietly wrong.
        """
        conn = sqlite3.connect(self.main)
        conn.execute("INSERT INTO notifications (agent_id, kind) "
                     "VALUES ('probe@1','legacy-unread')")
        conn.commit()
        conn.close()

        from memory.orchestration import notifications_unread_ids_impl as unread
        from memory.orchestration import notify_impl
        notify_impl("probe@1", "new-handoff", {})
        ids = unread("probe@1")

        self.assertIn(
            1, ids,
            "a legacy unread row was not delivered after the switch.\n"
            f"observed: unread={ids}\n"
            "possible: the read path no longer visits the notifications table\n"
            "inspect: _notification_sources in bin/memory/orchestration.py",
        )
        self.assertTrue(any(i >= _ID_FLOOR for i in ids),
                        f"observed: unread={ids} -- no dispatch row present")
        self.assertEqual(len(ids), 2, f"observed: unread={ids}")


class TestIntegratedStore(_Store):
    INTEGRATED = True

    def test_integrated_mode_uses_the_dispatch_table_in_the_main_file(self):
        from memory.orchestration import notifications_unread_ids_impl as unread
        from memory.orchestration import notify_impl
        notify_impl("probe@1", "handoff", {})

        self.assertEqual(self._count(self.main, "notification_dispatch"), 1)
        self.assertEqual(self._count(self.main, "notifications"), 0)
        self.assertTrue(unread("probe@1"),
                        "integrated mode wrote a row the read path cannot see")


class TestWriteAndReadAgree(unittest.TestCase):
    """The write table must be among the read sources, on every topology.

    These two answers live in different functions. If they diverge, mail is
    written where nothing looks for it: no error, no log, an inbox that simply
    stays empty. That is the §3 false negative this subsystem exists to remove.
    """

    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("M3_DISPATCH_MODE", "M3_DISPATCH_PG_SCHEMA")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_the_write_table_is_always_a_read_source(self):
        from memory.orchestration import _dispatch_write_table, _notification_sources
        for mode in ("separate", "integrated"):
            with self.subTest(mode=mode):
                if mode == "integrated":
                    os.environ["M3_DISPATCH_MODE"] = "integrated"
                else:
                    os.environ.pop("M3_DISPATCH_MODE", None)
                write = _dispatch_write_table()
                reads = [t for _, t in _notification_sources()]
                self.assertIn(
                    write, reads,
                    "the write table is not among the read sources.\n"
                    f"observed: write={write!r} reads={reads!r}\n"
                    "cause: the two resolvers disagree about where mail lives\n"
                    "possible impact: a sent notification is never delivered and "
                    "never reported -- the inbox just stays empty\n"
                    "inspect: _dispatch_write_table and _notification_sources",
                )


if __name__ == "__main__":
    unittest.main()
