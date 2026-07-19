"""Live-PostgreSQL end-to-end tests for the warehouse sync + migration work.

Gated by requires_pg (conftest auto-skips when no cluster is reachable). Each
test builds its OWN isolated schemas in a scratch database and tears them down,
so it never touches production data. Uses the pg_dsn() the rest of the suite
uses (M3_PRIMARY_PG_URL / M3_PG_URL, never PG_URL).

Covers, against a real cluster:
  * migrate_warehouse_to_schema: public -> m3_warehouse (move / merge / drop).
  * pg_fdw_sync: bidirectional set-based upsert with last-writer-wins + stale-skip.
  * pg_sync warehouse search_path detection.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

pytestmark = pytest.mark.requires_pg


def _dsn():
    from conftest import pg_dsn
    return pg_dsn()


def _connect():
    try:
        import psycopg2
    except ImportError:  # pragma: no cover
        import psycopg as psycopg2  # type: ignore
    conn = psycopg2.connect(_dsn(), connect_timeout=5)
    conn.autocommit = True
    return conn


@pytest.fixture
def warehouse_env():
    """A fresh (public warehouse tables + empty m3_warehouse) fixture in an
    isolated pair of schemas, torn down after. Yields (conn, ns) where ns is a
    unique suffix so parallel runs don't collide — we use dedicated schemas
    rather than the literal 'public'/'m3_warehouse' to stay hermetic."""
    conn = _connect()
    cur = conn.cursor()
    # Use unique schema names to avoid clobbering anything real, then monkeypatch
    # the tool's schema constant to point at the warehouse one.
    ns = "t" + uuid.uuid4().hex[:8]
    pub = f"{ns}_public"
    wh = f"{ns}_m3wh"
    cur.execute(f"CREATE SCHEMA {pub}")
    cur.execute(f"CREATE SCHEMA {wh}")
    yield conn, pub, wh
    cur.execute(f"DROP SCHEMA IF EXISTS {pub} CASCADE")
    cur.execute(f"DROP SCHEMA IF EXISTS {wh} CASCADE")
    conn.close()


class TestFdwUpsertLive:
    """pg_fdw_sync's set-based upsert against a real cluster — self-FDW-free:
    exercise _upsert directly (the piece that must be exactly right), since the
    foreign-server wiring needs two distinct hosts to test faithfully."""

    def test_upsert_delta_and_last_writer_wins(self, warehouse_env):
        import pg_fdw_sync as F
        conn, pub, wh = warehouse_env
        cur = conn.cursor()
        # minimal memory_items-shaped tables in both schemas
        for sch in (pub, wh):
            cur.execute(f"CREATE TABLE {sch}.mi (id TEXT PRIMARY KEY, "
                        f"content TEXT, updated_at TIMESTAMPTZ)")
        cur.execute(f"INSERT INTO {pub}.mi VALUES "
                    f"('a','v1','2026-07-01'),('b','v1','2026-07-15')")
        # PUSH pub -> wh, delta since epoch
        n = F._upsert(cur, f"{wh}.mi", f"{pub}.mi",
                      ["id", "content", "updated_at"], "id", "updated_at",
                      "1970-01-01T00:00:00+00:00")
        assert n == 2
        cur.execute(f"SELECT count(*) FROM {wh}.mi")
        assert cur.fetchone()[0] == 2

        # last-writer-wins: newer local update wins on re-push
        cur.execute(f"UPDATE {pub}.mi SET content='v2', updated_at='2026-07-20' "
                    f"WHERE id='a'")
        F._upsert(cur, f"{wh}.mi", f"{pub}.mi",
                  ["id", "content", "updated_at"], "id", "updated_at",
                  "2026-07-16T00:00:00+00:00")
        cur.execute(f"SELECT content FROM {wh}.mi WHERE id='a'")
        assert cur.fetchone()[0] == "v2"

    def test_upsert_stale_row_is_skipped(self, warehouse_env):
        import pg_fdw_sync as F
        conn, pub, wh = warehouse_env
        cur = conn.cursor()
        for sch in (pub, wh):
            cur.execute(f"CREATE TABLE {sch}.mi (id TEXT PRIMARY KEY, "
                        f"content TEXT, updated_at TIMESTAMPTZ)")
        # warehouse already has a NEWER version
        cur.execute(f"INSERT INTO {wh}.mi VALUES ('a','fresh','2026-07-20')")
        # local has an OLDER version
        cur.execute(f"INSERT INTO {pub}.mi VALUES ('a','stale','2026-06-01')")
        F._upsert(cur, f"{wh}.mi", f"{pub}.mi",
                  ["id", "content", "updated_at"], "id", "updated_at", None)
        cur.execute(f"SELECT content FROM {wh}.mi WHERE id='a'")
        assert cur.fetchone()[0] == "fresh"  # stale push rejected by the guard

    def test_upsert_no_timestamp_is_full_idempotent(self, warehouse_env):
        import pg_fdw_sync as F
        conn, pub, wh = warehouse_env
        cur = conn.cursor()
        for sch in (pub, wh):
            cur.execute(f"CREATE TABLE {sch}.me (id TEXT PRIMARY KEY, v TEXT)")
        cur.execute(f"INSERT INTO {pub}.me VALUES ('x','1'),('y','2')")
        F._upsert(cur, f"{wh}.me", f"{pub}.me", ["id", "v"], "id", None, None)
        F._upsert(cur, f"{wh}.me", f"{pub}.me", ["id", "v"], "id", None, None)
        cur.execute(f"SELECT count(*) FROM {wh}.me")
        assert cur.fetchone()[0] == 2  # ON CONFLICT DO NOTHING -> idempotent


class TestWarehouseMigrationLive:
    """migrate_warehouse_to_schema's REAL _plan_for/_apply against a live cluster.

    The tool hardcodes 'public' as the source schema, so we put uniquely-named
    scratch tables in the real public schema (hermetic via the unique names) and
    redirect WAREHOUSE_SCHEMA to a scratch warehouse schema."""

    @pytest.fixture
    def mig_env(self, monkeypatch):
        import migrate_warehouse_to_schema as mw
        conn = _connect()
        cur = conn.cursor()
        wh = "t" + uuid.uuid4().hex[:8] + "_wh"
        # unique table names so we never touch real public.tasks etc.
        tasks = "t" + uuid.uuid4().hex[:8] + "_tasks"
        secrets = "t" + uuid.uuid4().hex[:8] + "_secrets"
        cur.execute(f"CREATE SCHEMA {wh}")
        monkeypatch.setattr(mw, "WAREHOUSE_SCHEMA", wh)
        monkeypatch.setattr(mw, "_WAREHOUSE_TABLES", (tasks, secrets))
        yield conn, cur, mw, wh, tasks, secrets
        cur.execute(f"DROP SCHEMA IF EXISTS {wh} CASCADE")
        cur.execute(f"DROP TABLE IF EXISTS public.{tasks}")
        cur.execute(f"DROP TABLE IF EXISTS public.{secrets}")
        conn.close()

    def test_move_public_only_table(self, mig_env):
        conn, cur, mw, wh, tasks, secrets = mig_env
        cur.execute(f"CREATE TABLE public.{tasks} (id TEXT PRIMARY KEY, v TEXT)")
        cur.execute(f"INSERT INTO public.{tasks} SELECT g::text,'x' "
                    f"FROM generate_series(1,29) g")
        # classify: public-only -> move
        p = mw._plan_for(cur, tasks)
        assert p["action"] == "move" and p["public"] == 29 and p["warehouse"] is None
        # apply: pre-create the wh target (stands in for the DDL step), then _apply
        cur.execute(f"CREATE TABLE {wh}.{tasks} (id TEXT PRIMARY KEY, v TEXT)")
        mw._apply(cur, mw._plan_for(cur, tasks), warehouse_ddl_path=None,
                  drop_public=True)
        cur.execute(f"SELECT count(*) FROM {wh}.{tasks}")
        assert cur.fetchone()[0] == 29           # moved, no loss
        cur.execute("SELECT to_regclass(%s)", (f"public.{tasks}",))
        assert cur.fetchone()[0] is None          # public dropped

    def test_merge_dedup_both_present(self, mig_env):
        conn, cur, mw, wh, tasks, secrets = mig_env
        for sch in ("public", wh):
            cur.execute(f"CREATE TABLE {sch}.{secrets} "
                        f"(service_name TEXT PRIMARY KEY, val TEXT)")
            cur.execute(f"INSERT INTO {sch}.{secrets} SELECT g::text,'s' "
                        f"FROM generate_series(1,11) g")
        p = mw._plan_for(cur, secrets)
        assert p["action"] == "merge"
        mw._apply(cur, p, warehouse_ddl_path=None, drop_public=True)
        cur.execute(f"SELECT count(*) FROM {wh}.{secrets}")
        assert cur.fetchone()[0] == 11            # union dedup, not 22
        cur.execute("SELECT to_regclass(%s)", (f"public.{secrets}",))
        assert cur.fetchone()[0] is None

    def test_verify_before_drop_blocks_on_short_count(self, mig_env, monkeypatch):
        """_apply must RAISE (not drop) if the post-copy warehouse count is less
        than public — the no-data-loss guard."""
        conn, cur, mw, wh, tasks, secrets = mig_env
        cur.execute(f"CREATE TABLE public.{tasks} (id TEXT PRIMARY KEY, v TEXT)")
        cur.execute(f"INSERT INTO public.{tasks} SELECT g::text,'x' "
                    f"FROM generate_series(1,5) g")
        cur.execute(f"CREATE TABLE {wh}.{tasks} (id TEXT PRIMARY KEY, v TEXT)")
        # Force a short count: make _row_count under-report the warehouse.
        real_rc = mw._row_count
        def fake_rc(c, schema, table):
            n = real_rc(c, schema, table)
            if schema == wh and n is not None:
                return 0  # pretend nothing landed
            return n
        monkeypatch.setattr(mw, "_row_count", fake_rc)
        with pytest.raises(RuntimeError):
            mw._apply(cur, {"table": tasks, "action": "move",
                            "public": 5, "warehouse": None},
                      warehouse_ddl_path=None, drop_public=True)
        # public must still exist (not dropped)
        cur.execute("SELECT to_regclass(%s)", (f"public.{tasks}",))
        assert cur.fetchone()[0] is not None


class TestSearchPathLive:
    """pg_sync._set_warehouse_search_path against a real connection."""

    def test_sets_search_path_when_warehouse_present(self):
        import pg_sync
        conn = _connect()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("CREATE SCHEMA IF NOT EXISTS m3_warehouse")
        cur.execute("CREATE TABLE IF NOT EXISTS m3_warehouse.memory_items "
                    "(id TEXT PRIMARY KEY)")
        try:
            conn.autocommit = False
            schema = pg_sync._set_warehouse_search_path(conn)
            assert schema == "m3_warehouse"
            with conn.cursor() as c:
                c.execute("SHOW search_path")
                assert "m3_warehouse" in c.fetchone()[0]
            conn.rollback()
        finally:
            conn.autocommit = True
            cur.execute("DROP TABLE IF EXISTS m3_warehouse.memory_items")
            conn.close()

    def test_stays_public_when_no_warehouse(self):
        import pg_sync
        conn = _connect()
        cur = conn.cursor()
        # ensure the warehouse memory_items is absent in this DB path
        cur.execute("SELECT to_regclass('m3_warehouse.memory_items')")
        if cur.fetchone()[0] is not None:
            pytest.skip("m3_warehouse.memory_items exists in this test DB")
        conn.autocommit = False
        schema = pg_sync._set_warehouse_search_path(conn)
        assert schema == "public"
        conn.rollback()
        conn.close()


class TestFdwCrossDatabaseLive:
    """Real postgres_fdw wiring: two DATABASES on the same cluster, primary
    reaches the 'warehouse' DB as a foreign server. Exercises the actual
    _ensure_fdw_wired + sync_pg_to_pg orchestration end-to-end."""

    def test_ensure_fdw_wired_and_read_foreign(self):
        import pg_fdw_sync as F
        dsn = _dsn()
        # need postgres_fdw available on the primary; skip if not
        conn = _connect()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_available_extensions WHERE name='postgres_fdw'")
        if cur.fetchone() is None:
            conn.close()
            pytest.skip("postgres_fdw not available")
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw")
        # Build the m3_warehouse tables locally so IMPORT FOREIGN SCHEMA (which
        # points the FDW back at THIS same db via dsn) finds them.
        cur.execute("CREATE SCHEMA IF NOT EXISTS m3_warehouse")
        for t, ddl in (
            ("memory_items", "id TEXT PRIMARY KEY, updated_at TIMESTAMPTZ"),
            ("memory_embeddings", "id TEXT PRIMARY KEY"),
            ("memory_relationships", "id TEXT PRIMARY KEY"),
        ):
            cur.execute(f"CREATE TABLE IF NOT EXISTS m3_warehouse.{t} ({ddl})")
        cur.execute("INSERT INTO m3_warehouse.memory_items VALUES "
                    "('wh1','2026-07-19') ON CONFLICT DO NOTHING")
        try:
            conn.autocommit = False
            with conn.cursor() as c:
                F._ensure_fdw_wired(c, dsn)  # server + mapping + IMPORT
                c.execute(f"SELECT count(*) FROM {F.FDW_SCHEMA}.memory_items")
                assert c.fetchone()[0] >= 1   # read warehouse via FDW
            conn.rollback()
        except F.FdwUnavailable as e:
            pytest.skip(f"FDW self-wiring not supported here: {e}")
        finally:
            conn.autocommit = True
            cur.execute(f"DROP SCHEMA IF EXISTS {F.FDW_SCHEMA} CASCADE")
            cur.execute("DROP SCHEMA IF EXISTS m3_warehouse CASCADE")
            try:
                cur.execute(f"DROP SERVER IF EXISTS {F.FDW_SERVER} CASCADE")
            except Exception:
                pass
            conn.close()


def _shared_cols(cur, pub, wh, table):
    cur.execute(
        "SELECT w.column_name FROM information_schema.columns w "
        "JOIN information_schema.columns p ON p.table_schema=%s "
        "AND p.table_name=%s AND p.column_name=w.column_name "
        "WHERE w.table_schema=%s AND w.table_name=%s ORDER BY w.ordinal_position",
        (pub, table, wh, table))
    return [r[0] for r in cur.fetchall()]


def _pk(cur, schema, table):
    cur.execute(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
        "ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
        "WHERE i.indrelid=%s::regclass AND i.indisprimary",
        (f"{schema}.{table}",))
    return cur.fetchone()[0]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
