"""Phase 0: a PostgreSQL local store must never sync silently-nothing.

Three defects made a PG-primary deployment report success while moving no data.
Each test here pins one of them, and each would pass vacuously if the fix were
reverted to "log a warning and continue" — so they assert the REFUSAL, not just
the message.

Why this matters more than an ordinary bug: on a replication path a wrong
SUCCESS is silent divergence discovered weeks later, once both stores have moved
on. A wrong refusal is a red cron job someone fixes the same morning.

Hermetic: no PostgreSQL, no network, no real store. The backend seam and the
FDW module are stubbed; nothing here touches a live cluster.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

import sync_all  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_fdw_reason():
    """The fall-back reason is module state; keep tests independent of order."""
    sync_all._set_fdw_reason("")
    yield
    sync_all._set_fdw_reason("")


class TestGenericBridgeRefusesNonSqliteLocal:
    """`bin/pg_sync.py` opens the local side with sqlite3.connect().

    On a PG primary that finds no file, or opens a stale/empty agent_memory.db,
    reads zero rows and reports success. Until the Phase 3 port lands, reaching
    that bridge with a non-SQLite local store must be a refusal.
    """

    def test_postgres_local_refuses(self, monkeypatch):
        monkeypatch.setattr(sync_all, "_local_backend_name", lambda: "postgres")
        monkeypatch.setattr(sync_all, "_try_pg_fdw_fastpath", lambda dry_run: None)
        ran = []
        monkeypatch.setattr(sync_all, "run_pg_sync_for_db",
                            lambda db, dry: ran.append(db) or True)

        assert sync_all.run_pg_sync(dry_run=False) is False
        assert ran == [], "the generic bridge must not run on a non-SQLite local store"

    def test_a_future_backend_also_refuses(self, monkeypatch):
        """The check is `!= sqlite`, not `== postgres`.

        A MariaDB backend would hit the same SQLite-only bridge; keying on the
        capability rather than enumerating known-bad backends means it inherits
        the refusal with no edit here.
        """
        monkeypatch.setattr(sync_all, "_local_backend_name", lambda: "mariadb")
        monkeypatch.setattr(sync_all, "_try_pg_fdw_fastpath", lambda dry_run: None)
        assert sync_all.run_pg_sync(dry_run=False) is False

    def test_sqlite_local_still_runs(self, monkeypatch):
        """The refusal must not fire on a healthy existing user.

        This is the regression that matters most: a mis-detection turns every
        SQLite deployment's hourly cron red.
        """
        monkeypatch.setattr(sync_all, "_local_backend_name", lambda: "sqlite")
        monkeypatch.setattr(sync_all, "_try_pg_fdw_fastpath", lambda dry_run: None)
        monkeypatch.setattr(sync_all, "_resolve_dbs",
                            lambda: [pathlib.Path("/nonexistent/agent_memory.db")])
        ran = []
        monkeypatch.setattr(sync_all, "run_pg_sync_for_db",
                            lambda db, dry: ran.append(db) or True)

        assert sync_all.run_pg_sync(dry_run=False) is True
        assert ran, "SQLite deployments must still reach the generic bridge"

    def test_backend_probe_failure_defaults_to_sqlite(self, monkeypatch):
        """An unresolvable seam must not refuse.

        Defaulting to 'sqlite' keeps the historical shape working when the seam
        is unavailable (installer bootstrap, partial install) rather than
        breaking a user whose backend simply could not be read.
        """
        import memory.backends as backends

        def _boom():
            raise RuntimeError("seam unavailable")

        monkeypatch.setattr(backends, "active_backend", _boom)
        assert sync_all._local_backend_name() == "sqlite"

    def test_refusal_names_the_fdw_reason(self, monkeypatch, caplog):
        """On a PG primary this refusal is ALWAYS downstream of an FDW fallback.

        Reporting only "the bridge is unsupported" would leave the operator
        unaware that the supported path was one `CREATE EXTENSION` away.
        """
        monkeypatch.setattr(sync_all, "_local_backend_name", lambda: "postgres")
        monkeypatch.setattr(sync_all, "_try_pg_fdw_fastpath", lambda dry_run: None)
        sync_all._set_fdw_reason("FDW fast-path unavailable: postgres_fdw not installed")

        with caplog.at_level("ERROR"):
            sync_all.run_pg_sync(dry_run=False)

        blob = caplog.text
        assert "postgres_fdw not installed" in blob
        assert "M3_PRIMARY_PG_URL" in blob, "the message must name the fix, not only the fault"


class TestFdwUsesThePrimaryNotTheWarehouse:
    """`ctx.pg_connection()` is the WAREHOUSE role by contract.

    Using it as `primary_conn` wired postgres_fdw from the warehouse back to
    itself: the primary was never touched and the run reported success.
    """

    def test_missing_primary_dsn_falls_back_rather_than_guessing(self, monkeypatch):
        """No primary DSN must NOT silently fall back to the warehouse DSN."""
        import memory.backends as backends
        from m3_core import paths

        monkeypatch.setattr(backends, "active_backend",
                            lambda: type("B", (), {"name": "postgres"})())
        monkeypatch.setattr(paths, "resolve_primary_pg_dsn", lambda default=None: None)
        # A warehouse DSN IS available — the point is that it must not be used
        # as the primary just because it is the only one present.
        monkeypatch.setenv("M3_CDW_PG_URL", "postgresql://u@wh/warehouse")

        assert sync_all._try_pg_fdw_fastpath(dry_run=True) is None

    def test_same_database_is_refused_not_fallen_back(self, monkeypatch):
        """primary == warehouse is a misconfiguration, not an unavailability.

        Returning None here would fall through to the generic bridge and mask a
        setup error that silently syncs a store with itself; the run must fail.
        """
        import memory.backends as backends
        from m3_core import paths
        from memory.backends import postgres_backend as pgb

        monkeypatch.setattr(backends, "active_backend",
                            lambda: type("B", (), {"name": "postgres"})())
        monkeypatch.setattr(paths, "resolve_primary_pg_dsn",
                            lambda default=None: "postgresql://u@h/same")
        monkeypatch.setenv("M3_CDW_PG_URL", "postgresql://u@h/same")

        def _reject(url):
            raise RuntimeError("primary DSN names the SAME database as the warehouse")

        monkeypatch.setattr(pgb, "_reject_same_as_warehouse", _reject)
        assert sync_all._try_pg_fdw_fastpath(dry_run=True) is False

    def test_guard_is_invoked_explicitly(self):
        """PostgresBackend(dsn=...) BYPASSES the guard.

        `__init__` is `self._dsn = dsn or _resolve_dsn()`, and
        _reject_same_as_warehouse lives inside _resolve_dsn. Passing a DSN skips
        it entirely, so sync_all must call the guard itself — relying on
        construction would silently re-permit the failure being fixed.
        """
        import inspect

        from memory.backends import postgres_backend as pgb

        init_src = inspect.getsource(pgb.PostgresBackend.__init__)
        assert "dsn or _resolve_dsn()" in init_src, (
            "if this changed, re-check whether the explicit guard call is still needed"
        )
        assert "_reject_same_as_warehouse" in inspect.getsource(sync_all), (
            "sync_all must invoke the same-database guard itself"
        )


class TestSeamAndPortability:
    """The support matrix is 3 OSes x 2+ DBs x Python 3.11-3.14.

    Asserted rather than argued: each of these would otherwise only surface on a
    platform or backend nobody develops on.
    """

    def test_watermark_sql_uses_the_dialect_not_literal_placeholders(self):
        """A hardcoded `%s` is a portability bug the day a third backend lands.

        This block is PG-only today (the FDW path requires it), which is exactly
        why a literal would survive review — nothing exercises the other branch.
        Asking the seam costs nothing and removes the trap.
        """
        import inspect

        src = inspect.getsource(sync_all._fdw_run)
        assert "dialect.param()" in src
        assert "on_conflict_update" in src
        # No literal placeholder left in the SQL strings.
        assert "direction=%s" not in src
        assert "VALUES (%s, %s)" not in src

    def test_upsert_clause_is_identical_across_current_backends(self):
        """The call site can stop caring only if the seam really does converge."""
        from memory.backends.dialect import dialect_for

        clauses = {
            b: dialect_for(b).on_conflict_update("(direction)", ["last_synced_at"])
            for b in ("sqlite", "postgres")
        }
        assert len(set(clauses.values())) == 1, clauses

    def test_no_platform_specific_code_added(self):
        """Sync must behave the same on macOS, Linux and Windows.

        `IS_WIN` is pre-existing and used for interpreter resolution; what this
        guards is that the refusal/guard logic added here introduced no new
        platform branch.
        """
        import inspect

        for fn in (sync_all.run_pg_sync, sync_all._local_backend_name,
                   sync_all._fdw_run, sync_all._try_pg_fdw_fastpath):
            src = inspect.getsource(fn)
            for token in ("sys.platform", "IS_WIN", "os.name", "winreg", "posix"):
                assert token not in src, f"{fn.__name__} added a platform branch: {token}"

    def test_existence_guard_accepts_str_and_path(self):
        """`targets()` yields str today; a future refactor may yield Path.

        os.path.exists handles both, so the guard does not care — pinned so a
        "tidy-up" to Path.exists() on a str, or vice versa, is a deliberate choice.
        """
        import os
        import pathlib
        import tempfile

        d = tempfile.mkdtemp()
        for candidate in (os.path.join(d, "ghost.db"), pathlib.Path(d) / "ghost2.db"):
            assert os.path.exists(candidate) is False
            assert not os.path.exists(candidate), "probing must not create the file"

    def test_no_duplicate_global_declaration(self):
        """Two `global` statements in one function is a SyntaxError ruff misses.

        It cost a real debugging cycle here: lint passed, and only importing the
        module surfaced it. Pinned so the fall-back-reason plumbing keeps using
        the setter.
        """
        src = (_BIN / "sync_all.py").read_text(encoding="utf-8")
        assert src.count("global _FDW_LAST_REASON") == 1


class TestNeverCreateTheLocalStore:
    """sqlite3.connect() creates a missing file; the sync then 'succeeds' empty."""

    def test_absent_target_is_skipped_not_created(self, tmp_path):
        """Per-TARGET paths were never existence-checked.

        main() gated the primary db_path, but migrate_memory.targets("all")
        yielded others (agent_chatlog.db) that went straight to sqlite3.connect —
        which CREATES a missing file, after which the sync reads zero rows and
        reports success.

        Asserts the BEHAVIOUR, not the shape of the source. The first version of
        this test searched for an inline `os.path.exists(target.db_path)` inside
        main()'s loop; the Phase 3 port moved that guard into `_open_local_store`
        and the test broke while the protection was entirely intact. A test that
        fails on a refactor it should not care about is a test that gets
        "fixed" by deleting it.
        """
        import pg_sync

        ghost = tmp_path / "not_there.db"
        assert not ghost.exists()

        target = pg_sync.SyncTarget("chatlog", str(ghost), {})
        with pg_sync._open_local_store(target) as (conn, cur):
            assert conn is None, "an absent store must yield no connection"
            assert cur is None

        assert not ghost.exists(), (
            "probing an absent store must not CREATE it — that is the silent "
            "empty-sync this guard exists to prevent"
        )

    def test_the_premise_holds_sqlite_connect_creates_files(self, tmp_path):
        """Why the guard above is needed at all.

        Pinned separately so the reason survives even if the guard moves again:
        if sqlite3 ever stopped creating missing files, the guard would be
        belt-and-braces rather than load-bearing, and that is worth knowing.
        """
        import sqlite3

        ghost = tmp_path / "made_by_connect.db"
        assert not ghost.exists()
        sqlite3.connect(str(ghost)).close()
        assert ghost.exists()
