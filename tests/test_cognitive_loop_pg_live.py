"""Live-PG tests for m3_cognitive_loop's ported DB paths (de-gated for PostgreSQL).

Skips cleanly without a reachable cluster. Exercises the loop's work-gates
(_probe_core: M3Context on SQLite / mc._db on PG), the WAL-checkpoint no-op on PG,
and the classify pass (SELECT + UPDATE via _conn_for_pass) — the DB-touching parts
of the orchestrator. The LLM classify call is monkeypatched; the pass structure is
what's under test.

DSN from M3_PRIMARY_PG_URL/M3_PG_URL (never PG_URL).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))


def _dsn():
    return (os.environ.get("M3_PRIMARY_PG_URL") or os.environ.get("M3_PG_URL") or "").strip() or None


def _reachable(dsn):
    try:
        import psycopg2

        psycopg2.connect(dsn, connect_timeout=3).close()
        return True
    except Exception:
        return False


_DSN = _dsn()
pytestmark = pytest.mark.skipif(
    _DSN is None or not _reachable(_DSN),
    reason="no reachable PostgreSQL (set M3_PRIMARY_PG_URL to a throwaway cluster)",
)


@pytest.fixture()
def pg(monkeypatch):
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)
    monkeypatch.setenv("M3_PRIMARY_PG_URL", _DSN)
    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    b = PostgresBackend(dsn=_DSN)
    # Own the schema deterministically on the shared cluster. Drop EVERY public
    # table AND schema_versions so ensure_schema + the migration runner rebuild the
    # full schema from scratch (dropping a migration-managed table without clearing
    # its stamped version would leave the runner thinking it's already applied and
    # NOT recreate it).
    with b.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        for (t,) in cur.fetchall():
            cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
    b._schema_ready = False
    b.ensure_schema()
    import migrate_pg

    with b.connection() as c:
        migrate_pg.run_pending_pg_migrations(c)
    yield b
    b.close()


def test_work_gates_on_pg(pg):
    import m3_cognitive_loop as L

    aid = str(uuid.uuid4())
    with pg.connection() as c:
        cur = c.cursor()
        cur.execute(
            "INSERT INTO memory_items (id,type,content,scope) VALUES (%s,'auto','x','agent')",
            (aid,),
        )
        cur.execute(
            "INSERT INTO observation_queue (conversation_id,user_id,attempts) VALUES ('cv','u',0)"
        )
        cur.execute(
            "INSERT INTO chat_log_items (id,type,content,scope) VALUES (%s,'chat_log','y','agent')",
            (str(uuid.uuid4()),),
        )
    try:
        assert L.has_classify_work(None) is True   # the 'auto' row
        assert L.has_enrich_work(None) is True      # observation_queue non-empty
        assert L.has_entity_work(None, None) is True  # rows without entities
        # no aged data -> these are False (probes run without error, which is the point)
        assert L.has_consolidate_work(None, "observation", 1, 0) is False
        assert L.has_chatlog_prune_work(None, 30, 0) is False
    finally:
        with pg.connection() as c:
            cur = c.cursor()
            cur.execute("DELETE FROM memory_items WHERE id=%s", (aid,))
            cur.execute("DELETE FROM observation_queue WHERE conversation_id='cv'")
            cur.execute("DELETE FROM chat_log_items")


def test_checkpoint_wal_is_noop_on_pg(pg):
    import m3_cognitive_loop as L

    # Must not raise and must not attempt any SQLite WAL work on PG.
    L._checkpoint_wal(None)
    L._checkpoint_wal("/nonexistent/path.db")  # ignored on PG


def test_classify_pass_updates_on_pg(pg, monkeypatch):
    import m3_cognitive_loop as L
    import memory.enrich as _enrich

    async def _fake_classify(content, title):
        return "note"

    monkeypatch.setattr(_enrich, "_auto_classify", _fake_classify)

    aid = str(uuid.uuid4())
    with pg.connection() as c:
        c.cursor().execute(
            "INSERT INTO memory_items (id,type,content,scope) VALUES (%s,'auto','classify me','agent')",
            (aid,),
        )
    try:
        asyncio.run(L.run_classify_pass(
            argparse.Namespace(database=None, limit_per_pass=10)
        ))
        with pg.connection() as c:
            cur = c.cursor()
            cur.execute("SELECT type FROM memory_items WHERE id=%s", (aid,))
            t = cur.fetchone()[0]
        assert t == "note"  # auto -> resolved type, written via _conn_for_pass on PG
    finally:
        with pg.connection() as c:
            c.cursor().execute("DELETE FROM memory_items WHERE id=%s", (aid,))
