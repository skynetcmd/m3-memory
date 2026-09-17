"""A time bound must compare against the format the column is WRITTEN in.

THE DEFECT THIS CLOSES, measured 2026-09-17. `now_minus_minutes` rendered
`datetime('now', '-N minutes')` -> "2026-09-17 10:51:43", while every timestamp
column is written by `now()` as "2026-09-17T10:51:43Z". Compared as TEXT those
are incommensurable: 'T' (0x54) sorts after ' ' (0x20), so

    last_seen >= datetime('now','-10 minutes')

is TRUE for any same-day row regardless of its age. The filter reads correct and
does nothing. A heartbeat stamped 120 minutes ago passed a 10-minute window, so
a node offline for hours still counted as active.

Four production callers were affected: the fleet-topology detector, chatlog
status, two dashboard queue-stat queries, and the SessionStart capture check --
the last of which has a documented history of crying wolf.

Same class as the `until=<bare date>` bug `test_date_bound_parity.py` exists
for: one wrong query, a different wrong answer per backend, neither obviously
broken in isolation. PostgreSQL is immune because NOW() - interval yields a real
timestamptz compared against a real timestamptz; this is a SQLite-only trap,
which is exactly why it needs a test rather than a reviewer.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from memory.backends.sqlite_backend import SqliteDialect  # noqa: E402

_STORED_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class TestTimeBoundsMatchTheStoredFormat(unittest.TestCase):
    def setUp(self):
        self.d = SqliteDialect()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, ts TEXT)")

    def tearDown(self):
        self.conn.close()

    def _insert(self, rid: int, minutes_ago: int) -> None:
        ts = (datetime.now(timezone.utc)
              - timedelta(minutes=minutes_ago)).strftime(_STORED_FORMAT)
        self.conn.execute("INSERT INTO t (id, ts) VALUES (?,?)", (rid, ts))
        self.conn.commit()

    def test_now_renders_the_stored_format(self):
        """The anchor: every other bound must match what now() writes."""
        rendered = self.conn.execute(f"SELECT {self.d.now()}").fetchone()[0]
        datetime.strptime(rendered, _STORED_FORMAT)   # raises if it drifted

    def test_minute_bound_excludes_a_stale_row(self):
        """The regression itself. A 120-minute-old row must fail a 10-minute
        window; before the fix it passed, because 'T' > ' '."""
        self._insert(1, minutes_ago=2)
        self._insert(2, minutes_ago=120)
        got = [r["id"] for r in self.conn.execute(
            f"SELECT id FROM t WHERE ts >= {self.d.now_minus_minutes('?')} "
            f"ORDER BY id", (10,)).fetchall()]
        self.assertEqual(
            got, [1],
            "the minute bound matched a stale row.\n"
            f"observed: rows within 10 minutes = {got}, expected [1]\n"
            "cause: the bound renders a different timestamp format than the "
            "column stores, so the TEXT comparison is meaningless\n"
            "possible impact: every caller's time window becomes inert -- "
            "liveness checks count dead agents, capture checks cry wolf\n"
            "inspect: SqliteDialect.now_minus_minutes",
        )

    def test_day_bound_excludes_a_stale_row(self):
        self._insert(1, minutes_ago=60)
        self._insert(2, minutes_ago=60 * 24 * 9)
        got = [r["id"] for r in self.conn.execute(
            f"SELECT id FROM t WHERE ts >= {self.d.now_minus_days('?')} "
            f"ORDER BY id", (7,)).fetchall()]
        self.assertEqual(
            got, [1],
            f"observed: rows within 7 days = {got}, expected [1]\n"
            "inspect: SqliteDialect.now_minus_days",
        )

    def test_every_bound_renders_the_same_shape(self):
        """Guards the guard: a NEW time helper that forgets the format would
        pass the two tests above only by luck, so compare them structurally."""
        for name, sql in (
            ("now", self.d.now()),
            ("now_minus_minutes", self.d.now_minus_minutes("?")),
            ("now_minus_days", self.d.now_minus_days("?")),
            ("now_plus_seconds", self.d.now_plus_seconds("?")),
        ):
            with self.subTest(helper=name):
                self.assertIn(
                    "%Y-%m-%dT%H:%M:%SZ", sql,
                    f"{name} does not render the stored timestamp format.\n"
                    f"observed: {sql}\n"
                    "cause: a bare datetime() renders a space-separated, "
                    "Z-less string that cannot be compared against the columns\n"
                    "inspect: bin/memory/backends/sqlite_backend.py",
                )


if __name__ == "__main__":
    unittest.main()
