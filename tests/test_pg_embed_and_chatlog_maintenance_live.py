"""Embed backfill, chatlog decay and chatlog prune reach the PostgreSQL stores.

Hazard: each of these opened or required a local SQLite file, so on a
PostgreSQL install deferred embeddings were never filled and decay/prune
returned "DB not found" without touching the chatlog.

Live-PG: skips without a reachable cluster. Rows are inserted with fresh ids and
only those rows are asserted on, because the test database is shared.
"""
from __future__ import annotations

import os
import sys
import time
import types
import uuid
from pathlib import Path

import pytest

from conftest import pg_dsn

pytestmark = pytest.mark.requires_pg
_DSN = pg_dsn()
_BIN = Path(__file__).resolve().parent.parent / "bin"
_DIM = 1024


@pytest.fixture()
def pg(monkeypatch, tmp_path):
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)
    monkeypatch.setenv("M3_PRIMARY_PG_URL", _DSN)
    monkeypatch.delenv("M3_DATABASE", raising=False)
    if str(_BIN) not in sys.path:
        sys.path.insert(0, str(_BIN))
    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends import active_backend

    b = active_backend()
    assert b.name == "postgres"
    b.ensure_schema()
    yield b
    _selector._reset_for_tests()


def _insert(b, table: str, rows: list[tuple]) -> None:
    with b.connection() as c:
        cur = c.cursor()
        for row in rows:
            cur.execute(
                f"INSERT INTO {table} (id, type, title, content, importance, created_at) "
                "VALUES (%s, %s, %s, %s, %s, now() - make_interval(days => %s))",
                row,
            )
        c.commit()


def _count(b, sql: str, params: tuple) -> int:
    with b.connection() as c:
        cur = c.cursor()
        cur.execute(sql, params)
        return cur.fetchone()[0]


def test_embed_targets_cover_both_pg_stores(pg):
    import m3_cognitive_loop as loop

    assert loop._embed_targets(None) == [(None, "core"), (None, "chatlog")]


@pytest.mark.asyncio
@pytest.mark.parametrize("store, items, embeddings", [
    ("core", "memory_items", "memory_embeddings"),
    ("chatlog", "chat_log_items", "chat_log_embeddings"),
])
async def test_backfill_embeds_a_pg_row(pg, monkeypatch, store, items, embeddings):
    import embed_backfill as eb
    import memory_core as mc

    async def fake_embed_many(texts):
        return [([0.01] * _DIM, "test-model") for _ in texts]

    monkeypatch.setattr(mc, "_embed_many", fake_embed_many)
    mid = str(uuid.uuid4())
    _insert(pg, items, [(mid, "note", "t", "embed me on postgres", 0.5, 0)])

    args = eb._parse_args(["--store", store, "--id-prefix", mid[:8],
                           "--no-augment-anchors"])
    eb._verify_schema(args.db, store)
    assert eb._count_pending(args.db, args) >= 1
    await eb._run_sweep(args, eb.Counters())

    assert _count(pg, f"SELECT count(*) FROM {embeddings} WHERE memory_id = %s", (mid,)) == 1


def test_chatlog_decay_and_prune_scan_the_pg_chatlog(pg, tmp_path):
    import chatlog_decay
    import chatlog_prune

    ids = [str(uuid.uuid4()) for _ in range(3)]
    _insert(pg, "chat_log_items",
            [(i, "chat_log", "assistant@host: ok", "ok", 0.1, 40) for i in ids])
    no_file = str(tmp_path / "agent_chatlog.db")

    decay = chatlog_decay.run_sweep(no_file, apply=False)
    assert "error" not in decay and decay["errors"] == []
    assert decay["scanned"] >= 3

    opts = types.SimpleNamespace(
        fresh_days=7, prune_days=30, status_min_cluster=5, generic_imp_max=0.3,
        keep_imp_floor=0.4, generic_protect_len=300, generic_delete_maxlen=300,
        max_actions=10_000, no_generic=False, apply=False)
    prune = chatlog_prune.run(no_file, opts)
    assert "error" not in prune and prune["errors"] == []
    assert prune["scanned"] >= 3


def test_age_days_accepts_a_timestamptz_value():
    import datetime as dt

    sys.path.insert(0, str(_BIN))
    import chatlog_prune

    then = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
    assert 1.9 < chatlog_prune._age_days(then, time.time()) < 2.1
    assert 1.9 < chatlog_prune._age_days(then.isoformat(), time.time()) < 2.1
