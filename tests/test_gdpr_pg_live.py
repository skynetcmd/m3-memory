"""Live-PG tests for the GDPR tools (gdpr_export_impl / gdpr_forget_impl).

These were SQLite-only until the seam+dialect port: the `?` placeholders and
`strftime('now')` in the request-logging INSERT wouldn't run on PostgreSQL, and
the `gdpr_requests` table itself was missing from the PG schema until pg_044.
This proves export + forget work end-to-end on a live PG store, including the
audit-log INSERT into gdpr_requests.

Skips cleanly without a reachable cluster. DSN from M3_PRIMARY_PG_URL/M3_PG_URL
(never PG_URL — that's the warehouse var).
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))


# Gated by the requires_pg marker: conftest's collection hook auto-skips this
# module when no Postgres is reachable, and the pg_dsn() helper centralizes the
# M3_PRIMARY_PG_URL > M3_PG_URL precedence. (Replaces the former per-file
# _dsn()/_reachable()/skipif triplet.)
pytestmark = pytest.mark.requires_pg


@pytest.fixture()
def pg(monkeypatch, pg_url):
    _DSN = pg_url
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)
    monkeypatch.setenv("M3_PRIMARY_PG_URL", _DSN)
    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    b = PostgresBackend(dsn=_DSN)
    # Deterministic schema on the shared cluster: drop every public table, rebuild
    # base + pending migrations (includes pg_044 gdpr_requests + queue.stage).
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


def _seed(pg, user_id, n):
    with pg.connection() as c:
        cur = c.cursor()
        for i in range(n):
            cur.execute(
                "INSERT INTO memory_items (id,type,content,scope,user_id) "
                "VALUES (%s,'note',%s,'user',%s)",
                (str(uuid.uuid4()), f"secret {i} for {user_id}", user_id),
            )


def test_gdpr_export_on_pg(pg):
    import memory_maintenance as mm

    uid = f"subj-{uuid.uuid4()}"
    _seed(pg, uid, 3)
    try:
        out = mm.gdpr_export_impl(uid)
        payload = json.loads(out)
        assert payload["user_id"] == uid
        assert payload["items_count"] == 3
        assert len(payload["items"]) == 3
        # The audit-log INSERT (dialected placeholders + now()) must have landed
        # in gdpr_requests — the table pg_044 added.
        with pg.connection() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT request_type, status, items_affected FROM gdpr_requests "
                "WHERE subject_id=%s",
                (uid,),
            )
            row = cur.fetchone()
        assert row is not None, "gdpr_requests row not written on PG"
        assert row[0] == "export" and row[1] == "completed" and row[2] == 3
    finally:
        with pg.connection() as c:
            cur = c.cursor()
            cur.execute("DELETE FROM memory_items WHERE user_id=%s", (uid,))
            cur.execute("DELETE FROM gdpr_requests WHERE subject_id=%s", (uid,))


def test_gdpr_forget_on_pg(pg):
    import memory_maintenance as mm

    uid = f"subj-{uuid.uuid4()}"
    _seed(pg, uid, 4)
    try:
        out = mm.gdpr_forget_impl(uid)
        assert "4" in out or "forget" in out.lower()  # tolerant of message shape
        # All the user's memory_items must be hard-deleted.
        with pg.connection() as c:
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) FROM memory_items WHERE user_id=%s", (uid,))
            remaining = cur.fetchone()[0]
            cur.execute(
                "SELECT request_type, items_affected FROM gdpr_requests WHERE subject_id=%s",
                (uid,),
            )
            req = cur.fetchone()
        assert remaining == 0, "gdpr_forget left memory_items on PG"
        assert req is not None and req[0] == "forget" and req[1] == 4
    finally:
        with pg.connection() as c:
            cur = c.cursor()
            cur.execute("DELETE FROM memory_items WHERE user_id=%s", (uid,))
            cur.execute("DELETE FROM gdpr_requests WHERE subject_id=%s", (uid,))
