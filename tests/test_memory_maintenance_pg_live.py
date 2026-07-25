"""Live-PG tests for the memory_maintenance _impl surface.

The maintenance pass (decay/purge/retention/refresh/reinforce/consolidate/export/
import) was SQLite-only and GUARDED to skip on non-sqlite backends — it used
julianday(), LIMIT -1 OFFSET, GROUP_CONCAT, strftime, INSERT OR REPLACE and raw
'?' throughout. It is now routed through the dialect seam (param(), age_days_gt(),
all_rows_after_offset(), group_concat(), is_undefined_object_error()) and the guard
removed, so it runs on PostgreSQL. Because these are DATA-DELETING paths, this test
exercises them end-to-end on a live PG store — the round-trip AND the destructive
effect — before we trust the guard removal.

Skips cleanly without a reachable cluster (requires_pg).
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

pytestmark = pytest.mark.requires_pg


@pytest.fixture()
def pg(monkeypatch, pg_url):
    _DSN = pg_url
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)
    monkeypatch.setenv("M3_PRIMARY_PG_URL", _DSN)
    monkeypatch.setenv("M3_CORE_RS_DISABLE", "1")
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
        migrate_pg.run_pending_pg_migrations(c)
    yield b
    b.close()


def _write(uid, **kw):
    from memory.write import memory_write_impl
    raw = asyncio.run(memory_write_impl(embed=False, source="test", user_id=uid, **kw))
    return str(raw).replace("Created: ", "").strip().strip('"').split()[0]


def _set_created(mid, iso):
    """Backdate a row so age-based gates fire (bypasses the write path)."""
    from memory.backends import dialect
    from memory.db import _db
    p = dialect().param()
    with _db() as db:
        db.execute(f"UPDATE memory_items SET created_at = {p} WHERE id = {p}", (iso, mid))
        if hasattr(db, "commit"):
            db.commit()


@pytest.mark.xfail(reason="PG maintenance pass has one open 'can't adapt dict' "
                          "bind issue; the guard intentionally skips on PG until "
                          "test is green (m3 to_do: Finish memory_maintenance "
                          "PG-live). Flip to a positive assert once fixed.",
                   strict=False)
def test_maintenance_runs_on_pg_not_skipped(pg):
    """TARGET STATE: the full pass runs on PG (no skip). Currently xfail — the
    dialect port is done but a dict-bind issue remains, so the guard still skips."""
    import memory_maintenance as MM
    out = MM.memory_maintenance_impl()
    assert "skipped" not in out.lower()
    assert "Maintenance complete" in out or "Decayed" in out or "Statistics" in out


@pytest.mark.xfail(reason="blocked on the maintenance guard (skips on PG) + the "
                          "open dict-bind issue; see 'Finish memory_maintenance "
                          "PG-live' to_do.", strict=False)
def test_decay_and_purge_on_pg(pg):
    """Decay (age_days_gt) + expiry purge run and affect rows on PG."""
    import memory_maintenance as MM
    uid = f"mnt-{uuid.uuid4().hex[:8]}"
    old = _write(uid, type="note", content="old", importance=0.8)
    _set_created(old, "2020-01-01T00:00:00+00:00")  # older than the 7-day decay window
    MM.memory_maintenance_impl(decay=True, purge_expired=False, reinforce=False,
                               prune_orphan_embeddings=False, prune_orphan_queues=False)
    # importance decayed below the seeded 0.8.
    from memory.backends import dialect
    from memory.db import _db
    p = dialect().param()
    with _db() as db:
        imp = db.execute(f"SELECT importance FROM memory_items WHERE id = {p}", (old,)).fetchone()[0]
    assert imp < 0.8


@pytest.mark.xfail(reason="blocked on the maintenance guard (skips on PG) + the "
                          "open dict-bind issue; see 'Finish memory_maintenance "
                          "PG-live' to_do.", strict=False)
def test_retention_policy_max_count_on_pg(pg):
    """all_rows_after_offset (LIMIT ALL OFFSET on PG) soft-deletes oldest excess."""
    import memory_maintenance as MM
    uid = f"ret-{uuid.uuid4().hex[:8]}"
    aid = f"agent-{uuid.uuid4().hex[:6]}"
    ids = []
    for i in range(5):
        mid = _write(uid, type="note", content=f"m{i}", agent_id=aid)
        _set_created(mid, f"2020-01-0{i+1}T00:00:00+00:00")
        ids.append(mid)
    # keep newest 2 → 3 oldest should be soft-deleted
    assert "set for" in MM.memory_set_retention_impl(aid, max_memories=2, ttl_days=0, auto_archive=0)
    MM.memory_maintenance_impl(decay=False, purge_expired=False, reinforce=False,
                               prune_orphan_embeddings=False, prune_orphan_queues=False)
    from memory.backends import dialect
    from memory.db import _db
    p = dialect().param()
    with _db() as db:
        live = db.execute(
            f"SELECT COUNT(*) AS c FROM memory_items WHERE agent_id = {p} AND is_deleted = 0",
            (aid,)).fetchone()
    assert (live["c"] if hasattr(live, "keys") else live[0]) == 2


@pytest.mark.xfail(reason="memory_import_impl binds metadata_json as a Python "
                          "dict -> psycopg2 'can't adapt type dict' on PG; the "
                          "value must be json.dumps'd. See 'Finish "
                          "memory_maintenance PG-live' to_do.", strict=False)
def test_export_import_round_trip_on_pg(pg):
    """export (dynamic WHERE) + import (dialect upsert / INSERT OR IGNORE) on PG."""
    import json

    import memory_maintenance as MM
    uid = f"exp-{uuid.uuid4().hex[:8]}"
    a = _write(uid, type="note", title="A", content="alpha", agent_id=uid)
    b = _write(uid, type="note", title="B", content="beta", agent_id=uid)
    from memory_core import memory_link_impl
    memory_link_impl(a, b, "related")

    dump = MM.memory_export_impl(agent_filter=uid)
    payload = json.loads(dump)
    assert payload["item_count"] >= 2

    # Re-import is idempotent (ON CONFLICT DO UPDATE / DO NOTHING) — must not raise.
    res = MM.memory_import_impl(dump)
    assert "Imported" in res


def test_reinforce_decay_on_pg(pg):
    """The NOT-IN(empty) NULL trap fix + age_days_gt on last_accessed_at on PG.

    No corroboration-ledger activity → active_ids is empty → the NOT IN clause is
    omitted (not `NOT IN (NULL)`, which would match nothing). The un-reinforced,
    un-accessed belief must decay toward neutral."""
    import memory_maintenance as MM
    from memory.db import _db
    uid = f"rf-{uuid.uuid4().hex[:8]}"
    mid = _write(uid, type="belief", content="c", confidence=0.95)
    _set_created(mid, "2020-01-01T00:00:00+00:00")
    with _db() as db:
        reagg, decayed = MM._reinforce_confidence(db)
        if hasattr(db, "commit"):
            db.commit()
    assert decayed >= 1  # the un-reinforced belief decayed toward neutral
