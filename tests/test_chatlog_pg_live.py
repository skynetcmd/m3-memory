"""Live-PG tests for the chatlog subsystem on the one-schema/two-table format.

Skips cleanly without a reachable cluster. Exercises the ported chatlog query
paths on PostgreSQL — where chatlog rows live in chat_log_* tables in the SAME
database as core (memory_items): bulk write -> flush (INSERT into chat_log_items),
keyword search (via the seam's tsvector keyword_search against chat_log_items),
list-conversations (json_extract), and promote (same-DB cross-table
chat_log_items -> memory_items, no ATTACH).

DSN from M3_PRIMARY_PG_URL/M3_PG_URL (never PG_URL).
"""
from __future__ import annotations

import json
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
    b._schema_ready = False
    b.ensure_schema()
    import migrate_pg

    with b.connection() as c:
        migrate_pg.run_pending_pg_migrations(c)  # baseline + pg_043 chat_log_*
    yield b
    b.close()


def _item(content, conv, **extra):
    base = {
        "content": content,
        "role": "user",
        "conversation_id": conv,
        "host_agent": "claude-code",
        "provider": "anthropic",
        "model_id": "claude-3-sonnet",
    }
    base.update(extra)
    return base


async def _write_and_flush(items):
    import chatlog_core

    res = await chatlog_core.chatlog_write_bulk_impl(items)
    await chatlog_core._flush_once()
    return res


def test_chatlog_write_flush_into_chat_log_items(pg):
    import asyncio

    conv = f"cv-{uuid.uuid4().hex[:8]}"
    res = asyncio.run(_write_and_flush([
        _item("the quick brown fox jumps over the lazy dog", conv, tokens_in=10, tokens_out=5),
        _item("a second chatlog turn about postgres", conv, tokens_in=8, tokens_out=4),
    ]))
    assert res["failed"] == 0 and len(res["written_ids"]) == 2
    try:
        with pg.connection() as c:
            n = c.execute(
                "SELECT count(*) FROM chat_log_items WHERE conversation_id=%s AND type='chat_log'",
                (conv,),
            ).fetchone()[0]
        assert n == 2  # landed in chat_log_items, NOT core memory_items
        with pg.connection() as c:
            core = c.execute(
                "SELECT count(*) FROM memory_items WHERE conversation_id=%s", (conv,)
            ).fetchone()[0]
        assert core == 0  # chatlog write must not touch the core table
    finally:
        with pg.connection() as c:
            c.execute("DELETE FROM chat_log_items WHERE conversation_id=%s", (conv,))


def test_chatlog_keyword_search_via_seam_on_pg(pg):
    import asyncio

    import chatlog_core

    conv = f"cv-{uuid.uuid4().hex[:8]}"
    asyncio.run(_write_and_flush([
        _item("the quick brown fox jumps", conv),
        _item("completely unrelated tacos and lunch", conv),
    ]))
    try:
        out = asyncio.run(chatlog_core.chatlog_search_impl(query="fox", k=5))
        res = json.loads(out) if isinstance(out, str) else out
        rows = res.get("results", res) if isinstance(res, dict) else res
        contents = " ".join(str(r.get("content", "")) for r in rows)
        assert "fox" in contents  # tsvector keyword search on chat_log_items found it
        assert "tacos" not in contents  # the unrelated row didn't match
    finally:
        with pg.connection() as c:
            c.execute("DELETE FROM chat_log_items WHERE conversation_id=%s", (conv,))


def test_chatlog_promote_cross_table_on_pg(pg):
    """Promote copies chat_log_items rows into memory_items (same-DB cross-table,
    no ATTACH) with the new type."""
    import asyncio

    import chatlog_core

    conv = f"cv-{uuid.uuid4().hex[:8]}"
    res = asyncio.run(_write_and_flush([_item("promote me to core", conv)]))
    mid = res["written_ids"][0]
    try:
        out = asyncio.run(chatlog_core.chatlog_promote_impl(
            ids=[mid], conversation_id="", since="", until="",
            copy=True, target_type="conversation",
        ))
        pr = json.loads(out) if isinstance(out, str) else out
        assert pr["promoted"] == 1 and mid in pr["ids"]
        with pg.connection() as c:
            promoted = c.execute(
                "SELECT count(*) FROM memory_items WHERE id=%s AND type='conversation'",
                (mid,),
            ).fetchone()[0]
        assert promoted == 1  # landed in core memory_items as 'conversation'
        with pg.connection() as c:
            still = c.execute(
                "SELECT count(*) FROM chat_log_items WHERE id=%s", (mid,)
            ).fetchone()[0]
        assert still == 1  # copy=True leaves the chatlog row
    finally:
        with pg.connection() as c:
            c.execute("DELETE FROM chat_log_items WHERE conversation_id=%s", (conv,))
            c.execute("DELETE FROM memory_items WHERE id=%s", (mid,))
