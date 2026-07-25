"""Live-PG tests for the memory_core CRUD _impl surface.

These _impl functions are the hot/critical path (the underlying implementation
calls behind the MCP tools). The directive (m3 memory
"m3-critical-path-impl-must-use-instantiated-db-seam-not-raw-qmark") is that they
use the INSTANTIATED seam (dialect().param() etc.) and work on BOTH backends, not
just SQLite via the compat shim. This test drives them THROUGH the seam on a live
PG store and asserts the round-trips — the read side as well as the write side.

Historically these raised on PG: raw ``?`` placeholders -> SyntaxError; a raw
``INSERT INTO memory_relationships`` omitting the NOT NULL ``id`` PK ->
NotNullViolation; ``PRAGMA table_info`` in ensure_pinned_column -> aborted txn.
All fixed 2026-07-24; this locks the fix in.

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


def _new(uid, title="t", content="c"):
    from memory.write import memory_write_impl
    raw = asyncio.run(memory_write_impl(
        type="note", content=content, title=title, embed=False,
        scope="agent", user_id=uid, source="test"))
    return str(raw).replace("Created: ", "").strip().strip('"').split()[0]


def test_get_verify_update_on_pg(pg):
    import memory_core as MC
    uid = f"crud-{uuid.uuid4().hex[:8]}"
    mid = _new(uid, title="alpha", content="original")

    assert "alpha" in MC.memory_get_impl(mid)                 # raw-? SELECT ported
    assert "not found" not in MC.memory_get_impl(mid[:8])     # 8-char prefix path
    v = MC.memory_verify_impl(mid)
    assert "Integrity OK" in v or "no content hash" in v

    r = asyncio.run(MC.memory_update_impl(mid, content="revised", importance=0.9))
    assert "Updated" in r
    assert "revised" in MC.memory_get_impl(mid)               # update persisted


def test_pin_unpin_delete_on_pg(pg):
    """Exercises ensure_pinned_column (was PRAGMA -> aborted PG txn) + pin/delete."""
    import memory_core as MC
    uid = f"pin-{uuid.uuid4().hex[:8]}"
    mid = _new(uid)
    assert "Pinned" in MC.memory_pin_impl(mid)
    assert "Unpinned" in MC.memory_unpin_impl(mid)
    assert "eleted" in MC.memory_delete_impl(mid, hard=True)


def test_link_supplies_id_pk_on_pg(pg):
    """memory_link_impl must supply the memory_relationships.id PK (NOT NULL on PG,
    nullable on SQLite). Idempotent re-link is a no-op via ON CONFLICT."""
    import memory_core as MC
    uid = f"link-{uuid.uuid4().hex[:8]}"
    a, b = _new(uid, title="A"), _new(uid, title="B")
    assert "Linked" in MC.memory_link_impl(a, b, "related")
    assert "Linked" in MC.memory_link_impl(a, b, "related")   # idempotent, no dup/raise
    # Exactly one edge row exists for the pair.
    from memory.backends import dialect
    from memory.db import _db
    p = dialect().param()
    with _db() as conn:
        n = conn.execute(
            f"SELECT COUNT(*) AS c FROM memory_relationships "
            f"WHERE from_id = {p} AND to_id = {p} AND relationship_type = 'related'",
            (a, b),
        ).fetchone()
    assert (n["c"] if hasattr(n, "keys") else n[0]) == 1


def test_enqueue_paths_on_pg(pg):
    """observation/reflector enqueue used INSERT OR IGNORE (SQLite-only) -> now
    dialect insert_or_ignore + ON CONFLICT. Idempotent re-enqueue is a no-op."""
    import memory_core as MC
    uid = f"enq-{uuid.uuid4().hex[:8]}"
    conv = f"conv-{uid}"
    assert "Enqueued" in MC.observation_enqueue_impl(conv)
    assert "Enqueued" in MC.observation_enqueue_impl(conv)     # UNIQUE -> no-op
    assert "nqueued" in MC.reflector_enqueue_impl(conv, uid, 50)


def test_refresh_queue_on_pg(pg):
    """Dynamic-WHERE + LIMIT placeholder path."""
    import memory_core as MC
    uid = f"rq-{uuid.uuid4().hex[:8]}"
    mid = _new(uid)
    asyncio.run(MC.memory_update_impl(mid, refresh_on="2020-01-01T00:00:00Z"))
    out = MC.memory_refresh_queue_impl()
    assert "Refresh queue" in out
    assert mid[:8] in out  # the due memory is listed
