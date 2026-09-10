"""The sync lock and watermark tables — declared, portable, and host-aware.

Phase 2 of PG-local sync. Three things are pinned here:

  1. Both tables are DECLARED by migrations rather than created by whichever code
     path ran first. The lock previously lived in `sync_state`, a ChromaDB table
     that migration 040 legitimately dropped — after which the lock SELECT raised
     "no such table", a bare-except read that as "another sync is in progress",
     and EVERY sync silently skipped (the 2026-07-19 stale-warehouse outage).

  2. The lock records `host|pid`, not a bare pid. A pid is only evidence on the
     host that wrote it; on a shared store a bare pid lets one machine judge
     another's live lock stale and steal it.

  3. The SQL is dialect-rendered. `INSERT OR REPLACE` was SQLite-only.

Hermetic: a temp SQLite store built through the real migration chain. The
PostgreSQL half is covered by test_schema_parity_pg_live (the tables must exist
on both backends) and by the live lane.
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
_MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "memory" / "migrations"
sys.path.insert(0, str(_BIN))

import pg_sync  # noqa: E402


@pytest.fixture()
def migrated_conn():
    """A SQLite store built through the REAL migration chain, not hand-made DDL.

    Using the chain is the point: it proves 044 actually creates the table, and
    that 040's drop of `sync_state` still stands.
    """
    import migrate_memory as m

    db = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    conn = sqlite3.connect(db)
    m.init_migrations_table(conn)
    migs = m.discover_migrations(str(_MIGRATIONS))
    for v in sorted(migs):
        m.apply_migration(conn, v, migs[v]["name"], migs[v]["up"])
    yield conn
    conn.close()


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class TestDeclaredByMigration:
    def test_migration_044_creates_sync_locks(self, migrated_conn):
        assert "sync_locks" in _tables(migrated_conn)

    def test_watermarks_still_declared(self, migrated_conn):
        assert "sync_watermarks" in _tables(migrated_conn)

    def test_chroma_sync_state_stays_dropped(self, migrated_conn):
        """040 retired ChromaDB and dropped `sync_state` — correctly.

        The lock used to squat in that table, so the drop broke sync and the fix
        was an ad-hoc CREATE TABLE that quietly resurrected it. Giving the lock
        its own table means 040's drop stays meaningful; if `sync_state` comes
        back, someone has re-created the coupling.
        """
        assert "sync_state" not in _tables(migrated_conn)


class TestLockLifecycle:
    def test_acquire_blocks_second_holder_then_releases(self, migrated_conn):
        cur = migrated_conn.cursor()
        assert pg_sync._acquire_sync_lock(cur) is True
        assert pg_sync._acquire_sync_lock(cur) is False, "a held lock must block"
        pg_sync._release_sync_lock(cur)
        assert pg_sync._acquire_sync_lock(cur) is True

    def test_missing_table_self_heals_rather_than_wedging(self):
        """A missing lock table must read as "not locked", never as "locked".

        The opposite reading is what made every sync skip forever. Targets that
        run a different migration set still reach this path, so the self-heal
        stays even though 044 declares the table.
        """
        conn = sqlite3.connect(":memory:")
        try:
            assert pg_sync._acquire_sync_lock(conn.cursor()) is True
            assert "sync_locks" in _tables(conn)
        finally:
            conn.close()


class TestHostAwareStaleness:
    """A pid is only evidence on the host that wrote it."""

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _expired():
        return (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    _DEAD_PID = 999999

    def test_own_host_dead_pid_is_reclaimed_immediately(self):
        raw = f"{self._now()}|{self._DEAD_PID}@{pg_sync._this_host()}"
        assert pg_sync._sync_lock_is_stale(raw) is True

    def test_foreign_host_dead_pid_is_NOT_stolen(self):
        """The W1 failure: two machines sharing one lock row on a PG primary.

        Machine A reads B's live pid, finds no such process locally, and would
        judge the lock stale — two concurrent syncs. A foreign host's pid must be
        unevaluable, falling through to the timestamp ceiling.
        """
        raw = f"{self._now()}|{self._DEAD_PID}@some-other-machine"
        assert pg_sync._sync_lock_is_stale(raw) is False

    def test_foreign_host_expired_lock_is_reclaimed(self):
        """The ceiling still covers a genuinely abandoned foreign lock."""
        raw = f"{self._expired()}|{self._DEAD_PID}@some-other-machine"
        assert pg_sync._sync_lock_is_stale(raw) is True

    def test_legacy_value_is_treated_as_same_host(self):
        """Backward compatibility that PRESERVES crash recovery.

        My first version treated a host-less value as foreign, which read as
        "cautious" but silently cost the immediate dead-pid reclaim this whole
        check exists for: a lock left by the previous build would have blocked
        for the full hour. An existing test in
        test_warehouse_migrate_and_sync_resilience.py caught it.

        Legacy values only ever came from a per-machine SQLite file, where the
        pid WAS local by construction — so same-host is not a guess, it is the
        only thing they could have been. A shared store can contain only
        host-tagged values, because only this build writes there.
        """
        assert pg_sync._sync_lock_is_stale(f"{self._now()}|{self._DEAD_PID}") is True
        assert pg_sync._sync_lock_is_stale(f"{self._expired()}|{self._DEAD_PID}") is True

    def test_round_trip_of_the_lock_value(self):
        ts = self._now()
        parsed = pg_sync._parse_lock_value(pg_sync._lock_value(ts))
        assert parsed[0] == ts
        assert isinstance(parsed[1], int)
        assert parsed[2] == pg_sync._this_host()

    def test_unparseable_timestamp_does_not_wedge(self):
        """Fail toward "stale": a corrupt value must not block sync forever."""
        assert pg_sync._sync_lock_is_stale("not-a-timestamp|1@h") is True


class TestPortability:
    def test_lock_sql_uses_the_dialect_placeholder(self):
        """`INSERT OR REPLACE` is SQLite-only syntax.

        The lock is written on whichever store is local, so a SQLite-only
        statement is a portability bug the moment the local half is ported
        (Phase 3) or a third backend lands.

        Inspects the SQL STRINGS via the AST, not the raw source: the function's
        own comment says "Upsert rather than INSERT OR REPLACE", and a substring
        check fails on that comment — it cannot tell code from prose about code.
        (Fourth instance of that trap in this work; it is a genuinely easy one to
        walk into, because the comment exists precisely to warn about the thing.)
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(pg_sync._acquire_sync_lock)))
        sql = " ".join(
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ) + " ".join(
            v.value for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
            for v in n.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
        assert "INSERT OR REPLACE" not in sql, "SQLite-only syntax in the lock write"
        assert "ON CONFLICT" in sql

    def test_no_literal_placeholder_in_lock_statements(self):
        import inspect

        for fn in (pg_sync._acquire_sync_lock, pg_sync._release_sync_lock):
            src = inspect.getsource(fn)
            assert "= '?'" not in src
            assert 'lock_name = ?' not in src

    def test_hostname_lookup_never_raises(self, monkeypatch):
        """A lock must not fail to be taken because gethostname() misbehaved."""
        import socket

        monkeypatch.setattr(socket, "gethostname", lambda: (_ for _ in ()).throw(OSError))
        assert pg_sync._this_host() == "unknown-host"
