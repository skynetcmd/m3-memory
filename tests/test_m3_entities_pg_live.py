"""Live-PG test for m3_entities' ported DB paths (de-gated for PostgreSQL).

Skips cleanly without a reachable cluster. Exercises the row-selection query
(columns_of + json_extract + placeholders) and the extraction-queue upsert
(portable ON CONFLICT with table-qualified attempts, now(), status from pg_041) —
directly on PG, without the extractor LLM.

DSN from M3_PRIMARY_PG_URL/M3_PG_URL (never PG_URL).
"""
from __future__ import annotations

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
        migrate_pg.run_pending_pg_migrations(c)  # baseline + pg_040 + pg_041
    yield b
    b.close()


def test_query_eligible_rows_on_pg(pg):
    import m3_entities as me

    conv = f"c-{uuid.uuid4().hex[:8]}"
    mid = str(uuid.uuid4())
    with pg.connection() as c:
        c.cursor().execute(
            "INSERT INTO memory_items (id,type,content,conversation_id,scope,metadata_json) "
            "VALUES (%s,'message','Alice met Bob in Tokyo.',%s,'agent',%s)",
            (mid, conv, '{"conversation_id":"%s"}' % conv),
        )
    try:
        rows = me._query_eligible_rows(
            Path("ignored_on_pg"), ("message",), None, None, True
        )
        assert any(r[0] == mid for r in rows), "seeded row not eligible"
    finally:
        with pg.connection() as c:
            c.cursor().execute("DELETE FROM memory_items WHERE id=%s", (mid,))


def test_extraction_queue_upsert_on_pg(pg):
    """The queue UPSERT both writer closures share: table-qualified attempts must
    increment the EXISTING row (bare 'attempts' is ambiguous → errors on PG)."""
    import memory_core as mc
    import m3_entities as me
    from memory.backends import active_backend

    _d = active_backend().dialect()
    p = _d.param()
    mid = str(uuid.uuid4())
    with pg.connection() as c:
        c.cursor().execute(
            "INSERT INTO memory_items (id,type,content,scope) VALUES (%s,'message','x','agent')",
            (mid,),
        )
    try:
        upsert = (
            f"INSERT INTO entity_extraction_queue "
            f"(memory_id,attempts,last_error,last_attempt_at,status) "
            f"VALUES ({p},1,{p},{_d.now()},'failed') "
            f"ON CONFLICT(memory_id) DO UPDATE SET "
            f"attempts=entity_extraction_queue.attempts+1, "
            f"last_error=excluded.last_error, "
            f"last_attempt_at=excluded.last_attempt_at, status='failed'"
        )
        with mc._db() as qc:
            me._ensure_extraction_status_column(qc)  # no-op on PG
            qc.execute(upsert, (mid, "e1"))
            qc.execute(upsert, (mid, "e2"))  # conflict → attempts increments
            qc.commit()
        with pg.connection() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT attempts, status FROM entity_extraction_queue WHERE memory_id=%s",
                (mid,),
            )
            attempts, status = cur.fetchone()
            assert attempts == 2  # 1 insert + 1 conflict-increment
            assert status == "failed"
    finally:
        with pg.connection() as c:
            cur = c.cursor()
            cur.execute("DELETE FROM entity_extraction_queue WHERE memory_id=%s", (mid,))
            cur.execute("DELETE FROM memory_items WHERE id=%s", (mid,))
