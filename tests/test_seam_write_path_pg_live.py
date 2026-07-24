"""Live-PG test for the SEAM WRITE PATH: memory_write_impl -> live PostgreSQL.

WHY THIS EXISTS (2026-07-24). The PG-live suite is rich in tests that hand-write
raw SQL to seed rows and then exercise a native PRIMITIVE (keyword_search,
vector_search, CAS supersede). Those correctly test the native paths — but they
BYPASS the seam, so the high-level write path (`memory_write_impl` producing a
schema-valid row on PostgreSQL) was only thinly guarded. Two of those raw-SQL
fixtures had drifted from the real schema (omitting NOT NULL `type` /
memory_embeddings.id) and passed on SQLite while failing on PG.

This test closes that gap from the OTHER direction: it writes THROUGH the seam
(`memory_write_impl`, the same call production uses) against a live PG store and
reads the row back via the seam, asserting the write supplied every required
column and the row is retrievable. It does NOT replace the primitive tests — the
native-path coverage still matters — it ADDS the missing seam-level guarantee.

Skips cleanly without a reachable cluster (requires_pg). DSN from
M3_PRIMARY_PG_URL / M3_PG_URL (never PG_URL — the warehouse var).
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
    # The seam must not try to embed via a native/CUDA model in a headless test.
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


def test_seam_write_produces_schema_valid_row_on_pg(pg):
    """memory_write_impl (embed off) writes a complete, retrievable row on PG.

    Proves the seam supplies every NOT NULL column itself — the guarantee the
    raw-SQL primitive fixtures cannot make. If memory_write_impl omitted `type`
    or any required column, this INSERT would raise a NotNullViolation on PG."""
    from memory.write import memory_write_impl

    uid = f"seam-{uuid.uuid4().hex[:10]}"
    raw = asyncio.run(memory_write_impl(
        type="note", content="seam write path on postgres",
        title="seam probe", importance=0.6, embed=False,
        scope="agent", user_id=uid, source="test",
    ))
    assert raw  # returns the new id (possibly wrapped)

    # Read it back through the seam and confirm the round-trip.
    from memory.backends import dialect
    from memory.db import _db
    d = dialect()
    p = d.param()
    with _db() as conn:
        row = conn.execute(
            f"SELECT id, type, title, content, importance, scope, user_id, "
            f"is_deleted FROM memory_items WHERE user_id = {p}", (uid,)
        ).fetchone()
    assert row is not None, "seam write did not persist a row on PG"
    assert row["type"] == "note"          # the column the drift bug omitted
    assert row["title"] == "seam probe"
    assert row["scope"] == "agent"
    assert row["user_id"] == uid
    assert int(row["is_deleted"]) == 0


def test_seam_seed_helpers_insert_schema_valid_rows_on_pg(pg):
    """The shared conftest seed helpers produce schema-valid rows on PG.

    seed_memory_row / seed_embedding_row are the fix for raw-SQL fixture drift —
    the ONE place that knows every NOT NULL column. This asserts they satisfy the
    real PG schema (memory_items.type NOT NULL, memory_embeddings.id NOT NULL PK)."""
    from conftest import seed_embedding_row, seed_memory_row  # type: ignore
    from memory.backends import dialect
    from memory.db import _db
    import struct

    d = dialect()
    mid = f"seed-{uuid.uuid4().hex[:10]}"
    vec = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
    with _db() as conn:
        seed_memory_row(conn, d, mid=mid, type="belief", title="seed",
                        content="seeded row", importance=0.7)
        seed_embedding_row(conn, d, mid=mid, embedding=vec, dim=4,
                           embed_model="test-model")
        conn.commit()
        p = d.param()
        mrow = conn.execute(
            f"SELECT type, title FROM memory_items WHERE id = {p}", (mid,)
        ).fetchone()
        erow = conn.execute(
            f"SELECT id, memory_id FROM memory_embeddings WHERE memory_id = {p}",
            (mid,)
        ).fetchone()
    assert mrow is not None and mrow["type"] == "belief"
    assert erow is not None and erow["id"] == f"emb-{mid}"
