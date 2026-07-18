"""Live-PG test for m3_enrich's ported DB paths (de-gated for PostgreSQL).

Skips cleanly without a reachable cluster. Exercises the durable enrichment state
machine end to end on PG through m3_enrich._open_state_conn (the _PgStateConn
pooled adapter) — start_run, enroll_group (RETURNING id), claim_group
(attempts+1), mark_success (partial_failure_chunks/content_size_k), end_run,
run_total_cost_usd — plus the reflector read (HAVING COUNT(*), json_extract). The
extractor/reflector LLM paths are unchanged and not exercised.

DSN from M3_PRIMARY_PG_URL/M3_PG_URL (never PG_URL).
"""
from __future__ import annotations

import sys
import uuid
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
        migrate_pg.run_pending_pg_migrations(c)  # baseline + pg_040/041/042
    yield b
    b.close()


def test_open_state_conn_returns_pg_adapter(pg):
    import m3_enrich

    sc = m3_enrich._open_state_conn(Path("ignored_on_pg"))
    try:
        assert type(sc).__name__ == "_PgStateConn"
        # execute returns a cursor; commit/rollback exist
        cur = sc.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
    finally:
        sc.close()  # returns to pool, must not raise


def test_enrichment_state_machine_on_pg(pg):
    import enrichment_state as es
    import m3_enrich

    sv, tv = f"sv-{uuid.uuid4().hex[:6]}", "tv"
    sc = m3_enrich._open_state_conn(Path("ignored_on_pg"))
    try:
        assert es.has_state_tables(sc)
        rid = es.start_run(
            sc, profile="p", model="m", source_variant=sv,
            target_variant=tv, db_path="pg", concurrency=1,
        )
        gid, action = es.enroll_group(
            sc, source_variant=sv, target_variant=tv, group_key="g1",
            user_id="u1", db_path="pg", turn_count=2, source_content_hash="h1",
            content_size_k=1, enrich_run_id=rid,
        )
        sc.commit()
        assert isinstance(gid, int) and action == "inserted"  # RETURNING id
        tok = es.claim_group(sc, gid, enrich_run_id=rid)
        assert tok  # claimed (attempts+1)
        es.mark_success(sc, gid, obs_emitted=3, cost_usd=0.05, partial_failure_chunks=1)
        sc.commit()
        es.end_run(sc, rid, status="completed")
        sc.commit()
        assert abs(es.run_total_cost_usd(sc, rid) - 0.05) < 1e-9
    finally:
        # cleanup + release
        try:
            sc.execute("DELETE FROM enrichment_groups WHERE source_variant=%s", (sv,))
            sc.execute("DELETE FROM enrichment_runs WHERE source_variant=%s", (sv,))
            sc.commit()
        finally:
            sc.close()


def test_reflector_read_having_count_on_pg(pg):
    """The reflector eligibility read uses HAVING COUNT(*) (PG rejects a column
    alias in HAVING) + json_extract on metadata_json."""
    import memory_core as mc
    from memory.backends import active_backend

    _d = active_backend().dialect()
    p = _d.param()
    je = _d.json_extract_text("metadata_json", "conversation_id")
    conv = f"cv-{uuid.uuid4().hex[:6]}"
    with pg.connection() as c:
        cur = c.cursor()
        for _ in range(3):
            cur.execute(
                "INSERT INTO memory_items (id,type,content,user_id,scope,metadata_json) "
                "VALUES (%s,'observation','o','u1','agent',%s)",
                (str(uuid.uuid4()), '{"conversation_id":"%s"}' % conv),
            )
    try:
        with mc._db() as conn:
            rows = conn.execute(
                f"SELECT COALESCE(user_id,'') AS uid, {je} AS cid, COUNT(*) AS n "
                f"FROM memory_items WHERE type='observation' AND COALESCE(is_deleted,0)=0 "
                f"GROUP BY uid, cid HAVING COUNT(*) >= {p}",
                (2,),
            ).fetchall()
        hit = [r for r in rows if r[1] == conv]
        assert hit and hit[0][2] == 3
    finally:
        with pg.connection() as c:
            c.cursor().execute(
                "DELETE FROM memory_items WHERE metadata_json->>'conversation_id'=%s",
                (conv,),
            )
