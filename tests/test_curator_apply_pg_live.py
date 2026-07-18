"""Live-PG test for curate_memory_apply (the de-gated curator memory-plan path).

Skips cleanly without a reachable cluster. Proves apply_memory_plan runs end to
end on PostgreSQL now that its delegated bulk impls (memory_delete/update/link_
bulk_impl in memory_core) are dialected + backend-routed and the SQLite-only gate
was removed. DSN from M3_PRIMARY_PG_URL/M3_PG_URL (never PG_URL).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))


# Gated by the requires_pg marker: conftest's collection hook auto-skips this
# module when no Postgres is reachable. pg_dsn() centralizes the
# M3_PRIMARY_PG_URL > M3_PG_URL precedence (replaces the former per-file
# _dsn()/_reachable()/skipif triplet).
from conftest import pg_dsn

pytestmark = pytest.mark.requires_pg
_DSN = pg_dsn()


@pytest.fixture()
def pg_seeded(monkeypatch):
    """Backend=postgres, schema ensured, core tables dropped+rebuilt for a clean
    shape; seeds 4 memories via the write path and yields their ids."""
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)
    monkeypatch.setenv("M3_PRIMARY_PG_URL", _DSN)

    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    b = PostgresBackend(dsn=_DSN)
    with b.connection() as c:
        c.cursor().execute(
            "DROP TABLE IF EXISTS memory_history, memory_relationships, "
            "memory_embeddings, memory_items CASCADE"
        )
    b._schema_ready = False
    b.ensure_schema()

    import memory_core as mc

    ids = []
    from memory.write import memory_write_impl

    for i in range(4):
        r = asyncio.run(memory_write_impl("note", f"curate bulk {i}", title=f"cb{i}", embed=False))
        ids.append(r.split("Created:", 1)[1].strip().split()[0])
    yield mc, b, ids
    with b.connection() as c:
        c.cursor().execute("DELETE FROM memory_items WHERE title LIKE 'cb%' OR title LIKE 'CB%'")
    b.close()


def test_apply_memory_plan_runs_on_pg(pg_seeded):
    mc, b, ids = pg_seeded
    from curator_apply import apply_memory_plan

    plan = {
        "update": [{"id": ids[0], "title": "CB0-updated"}],
        "link": [{"from_id": ids[0], "to_id": ids[1], "relationship_type": "related"}],
        "delete": [ids[2]],          # soft
        "delete_hard": [ids[3]],     # hard
    }
    out = apply_memory_plan(plan)
    assert out["errors"] == [], out["errors"]
    assert out["summary"]["updated"] == 1
    assert out["summary"]["linked"] == 1
    assert out["summary"]["deleted_soft"] == 1
    assert out["summary"]["deleted_hard"] == 1

    with b.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT title FROM memory_items WHERE id=%s", (ids[0],))
        assert cur.fetchone()[0] == "CB0-updated"
        cur.execute("SELECT is_deleted FROM memory_items WHERE id=%s", (ids[2],))
        assert cur.fetchone()[0] == 1  # soft-deleted
        cur.execute("SELECT count(*) FROM memory_items WHERE id=%s", (ids[3],))
        assert cur.fetchone()[0] == 0  # hard-deleted
        cur.execute(
            "SELECT count(*) FROM memory_relationships WHERE from_id=%s AND to_id=%s",
            (ids[0], ids[1]),
        )
        assert cur.fetchone()[0] == 1  # link created


def test_curate_no_longer_gated_on_pg(pg_seeded):
    """The SQLite-only gate is gone: apply_memory_plan must NOT raise the
    require_sqlite_backend RuntimeError on a postgres backend."""
    mc, b, ids = pg_seeded
    from curator_apply import apply_memory_plan

    out = apply_memory_plan({"update": [{"id": ids[0], "importance": 0.9}]})
    # No "SQLite-only" refusal in errors, and the update landed.
    assert not any("SQLite" in str(e) for e in out["errors"])
    assert out["summary"]["updated"] == 1


def test_apply_chatlog_plan_prune_on_pg(monkeypatch):
    """apply_chatlog_plan's PRUNE path (_apply_prunes) must soft-delete chat_log
    rows in the PG chat_log_items table, not a stale SQLite file. Self-contained:
    builds the full PG schema (base + pg_043 chat_log_* + pg_044) and seeds
    chat_log_items directly."""
    import uuid

    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)
    monkeypatch.setenv("M3_PRIMARY_PG_URL", _DSN)
    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    b = PostgresBackend(dsn=_DSN)
    with b.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        for (t,) in cur.fetchall():
            cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
    b._schema_ready = False
    b.ensure_schema()
    import migrate_pg

    with b.connection() as c:
        migrate_pg.run_pending_pg_migrations(c)  # includes pg_043 chat_log_*

    conv = str(uuid.uuid4())
    try:
        with b.connection() as c:
            cur = c.cursor()
            for _ in range(3):
                cur.execute(
                    "INSERT INTO chat_log_items (id,type,title,content,scope,conversation_id) "
                    "VALUES (%s,'chat_log','x','turn','agent',%s)",
                    (str(uuid.uuid4()), conv),
                )
            # a non-chat_log row in the same conversation must survive
            cur.execute(
                "INSERT INTO chat_log_items (id,type,title,content,scope,conversation_id) "
                "VALUES (%s,'decision','keep','important','agent',%s)",
                (str(uuid.uuid4()), conv),
            )

        from curator_apply import apply_chatlog_plan

        out = apply_chatlog_plan({"prune": [{"conversation_id": conv, "reason": "abandoned"}]})
        assert out["summary"]["pruned"] == 3, out

        with b.connection() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM chat_log_items "
                "WHERE conversation_id=%s AND type='chat_log' AND is_deleted=1",
                (conv,),
            )
            assert cur.fetchone()[0] == 3  # all 3 chat_log rows soft-deleted
            cur.execute(
                "SELECT COUNT(*) FROM chat_log_items "
                "WHERE conversation_id=%s AND type='decision' AND is_deleted=0",
                (conv,),
            )
            assert cur.fetchone()[0] == 1  # the decision row untouched
    finally:
        with b.connection() as c:
            c.cursor().execute("DELETE FROM chat_log_items WHERE conversation_id=%s", (conv,))
        b.close()
