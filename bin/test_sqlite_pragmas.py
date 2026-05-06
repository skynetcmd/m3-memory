"""test_sqlite_pragmas.py — regression tests for bin/sqlite_pragmas.py.

Tests:
- Each profile applies without error on a real on-disk DB.
- journal_mode=WAL after apply.
- wal_autocheckpoint and journal_size_limit match the profile spec.
- 1000-row write loop in WAL mode never grows the WAL beyond journal_size_limit.
- profile_for_db() returns the expected profile for known DB names and a
  generic path.

Run:
    python -m pytest bin/test_sqlite_pragmas.py -v
    python bin/test_sqlite_pragmas.py            # or run directly
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

# Ensure bin/ is on sys.path when run directly.
_BIN = Path(__file__).resolve().parent
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from sqlite_pragmas import (
    PROFILES,
    apply_pragmas,
    checkpoint_passive,
    checkpoint_truncate,
    profile_for_db,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _pragma(conn: sqlite3.Connection, name: str):
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


# ---------------------------------------------------------------------------
# profile_for_db()
# ---------------------------------------------------------------------------

class TestProfileForDb:
    def test_main_memory_db(self):
        assert profile_for_db("memory/agent_memory.db") == "production"

    def test_chatlog_db(self):
        assert profile_for_db("memory/agent_chatlog.db") == "chatlog"

    def test_chatlog_db_suffix(self):
        assert profile_for_db("/some/path/test_chatlog.db") == "chatlog"

    def test_lme_m_db(self):
        assert profile_for_db("memory/lme_m.db") == "bench"

    def test_agent_test_bench_db(self):
        assert profile_for_db("memory/agent_test_bench.db") == "bench"

    def test_arbitrary_bench_db(self):
        assert profile_for_db("/data/my_results_bench.db") == "bench"

    def test_arbitrary_path_defaults_to_production(self):
        assert profile_for_db("/var/data/myapp.db") == "production"

    def test_path_object(self):
        assert profile_for_db(Path("memory/agent_memory.db")) == "production"


# ---------------------------------------------------------------------------
# apply_pragmas() — correctness
# ---------------------------------------------------------------------------

class TestApplyPragmas:
    @pytest.fixture(params=list(PROFILES.keys()))
    def profile(self, request):
        return request.param

    def test_apply_without_error(self, profile, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _new_conn(db_path)
        try:
            apply_pragmas(conn, profile)  # must not raise
        finally:
            conn.close()

    def test_journal_mode_is_wal(self, profile, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _new_conn(db_path)
        apply_pragmas(conn, profile)
        assert _pragma(conn, "journal_mode").lower() == "wal"
        conn.close()

    def test_wal_autocheckpoint_matches_profile(self, profile, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _new_conn(db_path)
        apply_pragmas(conn, profile)
        expected = PROFILES[profile]["wal_autocheckpoint"]
        assert _pragma(conn, "wal_autocheckpoint") == expected
        conn.close()

    def test_journal_size_limit_matches_profile(self, profile, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _new_conn(db_path)
        apply_pragmas(conn, profile)
        expected = PROFILES[profile]["journal_size_limit"]
        assert _pragma(conn, "journal_size_limit") == expected
        conn.close()

    def test_unknown_profile_raises(self, tmp_path):
        conn = _new_conn(str(tmp_path / "test.db"))
        with pytest.raises(KeyError):
            apply_pragmas(conn, "nonexistent_profile")
        conn.close()

    def test_overrides_applied(self, tmp_path):
        conn = _new_conn(str(tmp_path / "test.db"))
        apply_pragmas(conn, "production", overrides={"wal_autocheckpoint": 9999})
        assert _pragma(conn, "wal_autocheckpoint") == 9999
        conn.close()

    def test_idempotent(self, tmp_path):
        """Calling apply_pragmas twice on the same connection must not raise."""
        conn = _new_conn(str(tmp_path / "test.db"))
        apply_pragmas(conn, "production")
        apply_pragmas(conn, "production")  # must not raise
        conn.close()


# ---------------------------------------------------------------------------
# WAL growth test — the headline regression guard.
#
# Create a table, write 1000 rows in a WAL-mode DB, and assert the WAL file
# never exceeds journal_size_limit for the profile under test.
# In-memory DBs do not have WAL semantics; we use a real on-disk file.
# ---------------------------------------------------------------------------

class TestWalGrowth:
    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            "  id TEXT PRIMARY KEY,"
            "  payload TEXT"
            ")"
        )
        conn.commit()

    @pytest.mark.parametrize("profile", list(PROFILES.keys()))
    def test_wal_stays_under_limit(self, profile, tmp_path):
        db_path = tmp_path / "wal_test.db"
        wal_path = tmp_path / "wal_test.db-wal"

        conn = _new_conn(str(db_path))
        apply_pragmas(conn, profile)
        self._create_schema(conn)

        limit = PROFILES[profile]["journal_size_limit"]
        payload = "x" * 512  # 512-byte payload per row

        for i in range(1000):
            conn.execute(
                "INSERT OR REPLACE INTO items (id, payload) VALUES (?, ?)",
                (str(uuid.uuid4()), payload),
            )
            if (i + 1) % 100 == 0:
                conn.commit()
                # After commit, SQLite may run the autocheckpoint. Check WAL size.
                if wal_path.exists():
                    wal_size = wal_path.stat().st_size
                    assert wal_size <= limit, (
                        f"WAL grew to {wal_size} bytes (limit={limit}) "
                        f"after {i+1} rows in profile {profile!r}"
                    )

        conn.commit()
        checkpoint_passive(conn)

        if wal_path.exists():
            wal_size = wal_path.stat().st_size
            assert wal_size <= limit, (
                f"WAL is {wal_size} bytes after final passive checkpoint "
                f"(limit={limit}, profile={profile!r})"
            )
        conn.close()


# ---------------------------------------------------------------------------
# checkpoint helpers
# ---------------------------------------------------------------------------

class TestCheckpointHelpers:
    def test_checkpoint_passive_returns_tuple(self, tmp_path):
        conn = _new_conn(str(tmp_path / "cp.db"))
        apply_pragmas(conn, "production")
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.commit()
        result = checkpoint_passive(conn)
        assert len(result) == 3
        conn.close()

    def test_checkpoint_truncate_returns_tuple(self, tmp_path):
        conn = _new_conn(str(tmp_path / "cp.db"))
        apply_pragmas(conn, "production")
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.commit()
        result = checkpoint_truncate(conn)
        assert len(result) == 3
        conn.close()


# ---------------------------------------------------------------------------
# Standalone runner (no pytest)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import traceback

    PASS = []
    FAIL = []

    def run(name, fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
            PASS.append(name)
            print(f"  PASS  {name}")
        except Exception:
            FAIL.append(name)
            print(f"  FAIL  {name}")
            traceback.print_exc()

    print("=== test_sqlite_pragmas standalone run ===")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # profile_for_db tests
        pfd = TestProfileForDb()
        run("profile_for_db/main_memory_db", pfd.test_main_memory_db)
        run("profile_for_db/chatlog_db", pfd.test_chatlog_db)
        run("profile_for_db/lme_m_db", pfd.test_lme_m_db)
        run("profile_for_db/agent_test_bench_db", pfd.test_agent_test_bench_db)
        run("profile_for_db/arbitrary_path", pfd.test_arbitrary_path_defaults_to_production)

        # apply_pragmas tests
        t = TestApplyPragmas()
        for p in PROFILES:
            run(f"apply/{p}/without_error", t.test_apply_without_error, p, td)
            run(f"apply/{p}/journal_mode_wal", t.test_journal_mode_is_wal, p, td)
            run(f"apply/{p}/wal_autocheckpoint", t.test_wal_autocheckpoint_matches_profile, p, td)
            run(f"apply/{p}/journal_size_limit", t.test_journal_size_limit_matches_profile, p, td)

        run("apply/unknown_profile_raises", t.test_unknown_profile_raises, td)
        run("apply/overrides_applied", t.test_overrides_applied, td)
        run("apply/idempotent", t.test_idempotent, td)

        # WAL growth tests
        wg = TestWalGrowth()
        for p in PROFILES:
            run(f"wal_growth/{p}", wg.test_wal_stays_under_limit, p, td)

        # checkpoint helpers
        ch = TestCheckpointHelpers()
        run("checkpoint/passive", ch.test_checkpoint_passive_returns_tuple, td)
        run("checkpoint/truncate", ch.test_checkpoint_truncate_returns_tuple, td)

    print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
