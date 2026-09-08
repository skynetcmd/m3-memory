"""`BEGIN IMMEDIATE` is SQLite-only; the dialect renders the portable form.

Three read-modify-write passes (chatlog enrich backfill, chatlog prune, entity
vocab migration) each spelled `conn.execute("BEGIN IMMEDIATE")` inline. That is
two defects at once:

  * **It is a hard error on PostgreSQL.** Verified against a live PostgreSQL
    16.14 cluster on 2026-09-08, not reasoned about:

        BEGIN IMMEDIATE;
        ERROR:  syntax error at or near "IMMEDIATE"

    A plain `BEGIN` is accepted, and `ON CONFLICT DO NOTHING` de-duplicates as
    expected (a duplicate insert left exactly 1 row).

  * **Three copies of one idiom drift** (§10a), independent of correctness. One
    of the three had already grown a call-site `if _is_sqlite:` guard — which is
    the branch the seam exists to remove: a THIRD backend falls into the "do
    nothing" arm and silently runs its read-modify-write WITHOUT the lock it
    needs, which is worse than failing.

`Dialect.begin_immediate(conn)` is concrete on the base (plain `BEGIN`, correct
under MVCC) and overridden by SQLite (`BEGIN IMMEDIATE`, which takes the
RESERVED lock at once instead of at first write). Deliberately NOT abstract: a
new backend that implements nothing still gets correct behaviour rather than a
NotImplementedError partway through a maintenance pass.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory.backends import dialect as _active_dialect  # noqa: E402
from memory.backends.dialect import Dialect  # noqa: E402


class _Recorder:
    """Captures the SQL a dialect issues, without needing a real connection."""

    def __init__(self):
        self.sql: list[str] = []

    def execute(self, sql, *a):
        self.sql.append(sql)
        return self


class TestBeginImmediate(unittest.TestCase):

    def test_base_dialect_emits_portable_begin(self):
        """The default must be plain BEGIN — what every SQL backend accepts."""
        rec = _Recorder()
        Dialect.begin_immediate(Dialect.__new__(Dialect), rec)
        self.assertEqual(rec.sql, ["BEGIN"])
        self.assertNotIn("IMMEDIATE", rec.sql[0])

    def test_sqlite_overrides_with_immediate(self):
        """SQLite needs the RESERVED lock NOW; a deferred BEGIN takes it at the
        first write, so two passes can both read and then one fails
        'database is locked' after doing its work."""
        from memory.backends.sqlite_backend import SQLITE

        rec = _Recorder()
        SQLITE.begin_immediate(rec)
        self.assertEqual(rec.sql, ["BEGIN IMMEDIATE"])

    def test_sqlite_override_is_actually_installed(self):
        """Guard against the override being dropped and silently inheriting the
        weaker base form."""
        from memory.backends.sqlite_backend import SqliteDialect

        self.assertIsNot(
            SqliteDialect.begin_immediate, Dialect.begin_immediate,
            "SqliteDialect must override begin_immediate",
        )

    def test_round_trip_on_a_real_sqlite_db(self):
        """Behaviour, not just the emitted string."""
        path = Path(tempfile.mkdtemp()) / "t.db"
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            conn.execute("CREATE TABLE t (a INTEGER)")
            _active_dialect().begin_immediate(conn)
            conn.execute("INSERT INTO t VALUES (1)")
            conn.execute("COMMIT")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)
        finally:
            conn.close()

    def test_no_inline_begin_immediate_left_in_feature_code(self):
        """The whole point: the idiom lives in the dialect, not at call sites.

        CODE only — the dialect and this test legitimately name the string in
        prose, and a naive scan that flagged its own explanation would train
        readers to ignore it.
        """
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for p in (root / "bin").rglob("*.py"):
            rel = p.relative_to(root).as_posix()
            if "/backends/" in rel:          # the seam defines it
                continue
            for i, line in enumerate(
                p.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                s = line.strip()
                if s.startswith("#"):
                    continue
                if "BEGIN IMMEDIATE" in s:
                    offenders.append(f"{rel}:{i}")
        self.assertEqual(
            offenders, [],
            "inline BEGIN IMMEDIATE (SQLite-only; PostgreSQL raises "
            'ERROR: syntax error at or near "IMMEDIATE"). '
            f"Use dialect().begin_immediate(conn): {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
