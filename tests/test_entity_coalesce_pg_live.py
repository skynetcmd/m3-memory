"""Live-PG test for entity_coalesce's ported DB paths (de-gated for PostgreSQL).

Skips cleanly without a reachable cluster. Exercises the coalescing pass end to
end THROUGH THE SEAM (no explicit db_path, so it runs on the active PG backend):
detect -> apply (reversible same_as/cluster overlay) -> unapply, plus the review
recorder. This is what proves the dialecting (placeholders, ON CONFLICT upsert,
JSON-in-Python alias mutation, columns-free schema) AND the transaction fix (no
raw BEGIN/COMMIT/ROLLBACK — the seam owns the txn; psycopg2 rejects manual ones)
actually work on Postgres, not just SQLite.

DSN from M3_PRIMARY_PG_URL/M3_PG_URL (never PG_URL).
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

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
        migrate_pg.run_pending_pg_migrations(c)  # baseline + pg_NNN incl pg_048
    yield b
    _selector._reset_for_tests()
    b.close()


def _seed_entities(b, rows):
    """rows: [(id, canonical_name, entity_type, attributes_json)]. Provisional
    'unknown' rows are the coalescing pass's working set."""
    with b.connection() as c:
        cur = c.cursor()
        cur.executemany(
            "INSERT INTO entities(id, canonical_name, entity_type, attributes_json) "
            "VALUES (%s,%s,%s,%s)",
            rows,
        )


def _cleanup(b, ids):
    with b.connection() as c:
        cur = c.cursor()
        qs = ",".join(["%s"] * len(ids))
        cur.execute(f"DELETE FROM entity_relationships WHERE from_entity IN ({qs}) OR to_entity IN ({qs})", ids + ids)
        cur.execute(f"DELETE FROM entity_coalesce_candidates WHERE entity_a IN ({qs}) OR entity_b IN ({qs})", ids + ids)
        cur.execute(f"DELETE FROM entity_coalesce_embeddings WHERE entity_id IN ({qs})", ids)
        cur.execute(f"DELETE FROM entities WHERE id IN ({qs})", ids)


def test_detect_apply_unapply_on_pg(pg):
    """The high-fuzzy dup pair ('small models'/'small model') needs no embedder,
    so this runs without the embed cascade — it exercises detect -> apply ->
    unapply against live PG through the seam."""
    import files_memory.entity_coalesce as ec

    prov = '{"provisional": true, "first_seen_in": "files.db"}'
    a, bid, noise = (f"e-{uuid.uuid4().hex[:10]}" for _ in range(3))
    ids = [a, bid, noise]
    _seed_entities(pg, [
        (a, "small models", "unknown", prov),
        (bid, "small model", "unknown", prov),
        (noise, "$0.25", "unknown", prov),   # quarantined, not a candidate
    ])
    try:
        # DETECT (seam route: no db_path). Writes quarantine flag + candidate row.
        r = ec.coalesce_detect(dry_run=False, max_pairs=50)
        assert "error" not in r, r.get("error")
        assert r["prune"]["quarantined"] >= 1
        cands = [c for c in ec.list_coalesce_candidates(limit=99)
                 if c["entity_a"]["id"] in ids and c["entity_b"]["id"] in ids]
        assert cands, "expected the small model(s) pair as a candidate"
        assert cands[0]["band"] == "merge"

        # APPLY (seam route needs confirm=True — no explicit db_path). Writes the
        # reversible same_as edge + clustered flag + alias (JSON-in-Python).
        res = ec.apply_coalescence(include_auto_merge=True, dry_run=False, confirm=True)
        assert res["applied"] >= 1
        cid = res["clusters_touched"][0]

        with pg.connection() as c:
            cur = c.cursor()
            cur.execute("SELECT count(*) FROM entity_relationships WHERE predicate='same_as' "
                        "AND (from_entity IN %s OR to_entity IN %s)", (tuple(ids), tuple(ids)))
            assert cur.fetchone()[0] >= 1
            # the representative got an alias written via the Python read-modify-write
            cur.execute("SELECT attributes_json FROM entities WHERE cluster_id=%s AND "
                        "attributes_json ->> 'aliases' IS NOT NULL", (cid,))
            assert cur.fetchone() is not None, "apply should have written an alias (JSONB)"

        # UNAPPLY: drops edge, clears cluster, strips alias, tombstones.
        rev = ec.unapply_cluster(cid)
        assert rev["reverted_members"] >= 1
        with pg.connection() as c:
            cur = c.cursor()
            cur.execute("SELECT count(*) FROM entity_relationships WHERE predicate='same_as' "
                        "AND from_entity IN %s", (tuple(ids),))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM entities WHERE cluster_id=%s", (cid,))
            assert cur.fetchone()[0] == 0
    finally:
        _cleanup(pg, ids)


def test_review_records_decision_on_pg(pg):
    """review_coalesce_candidates writes the note via json_bind_value (dict ->
    JSONB) — the write-side JSON path, exercised on live PG."""
    import files_memory.entity_coalesce as ec

    prov = '{"provisional": true}'
    a, bid = (f"e-{uuid.uuid4().hex[:10]}" for _ in range(2))
    ids = [a, bid]
    _seed_entities(pg, [
        (a, "memory item", "unknown", prov),
        (bid, "memory items", "unknown", prov),
    ])
    try:
        ec.coalesce_detect(dry_run=False, max_pairs=50)
        cands = [c for c in ec.list_coalesce_candidates(reviewed=False, limit=99)
                 if c["entity_a"]["id"] in ids and c["entity_b"]["id"] in ids]
        assert cands
        res = ec.review_coalesce_candidates(
            [{"uuid": cands[0]["uuid"], "action": "merge"}], note="pg-live")
        assert res["updated"] == 1
        with pg.connection() as c:
            cur = c.cursor()
            cur.execute("SELECT metadata ->> 'note' FROM entity_coalesce_candidates WHERE uuid=%s",
                        (cands[0]["uuid"],))
            assert cur.fetchone()[0] == "pg-live"
    finally:
        _cleanup(pg, ids)
