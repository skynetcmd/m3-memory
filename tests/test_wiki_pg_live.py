"""Live-PostgreSQL test for the wiki generator (bin/wiki/).

The wiki reads the memory store through m3's backend seam (`_db()`/`dialect()`),
so it must work on PostgreSQL, not just SQLite. This proves the PG-specific paths
that a SQLite fixture cannot exercise:

  - core-set selection with dialect placeholders (`%s`) and the `pinned = %s`
    predicate (pinned is INTEGER on PG, BOOLEAN-looking literals fail);
  - `_has_column` via `information_schema.columns` — a metadata query that does
    NOT abort the transaction (a `SELECT missingcol …` probe would poison the PG
    connection for every later query in the same build);
  - entity co-mention clustering guarded by `_has_column` (its query runs on the
    same connection, so a poisoning probe upstream would break it);
  - name-addressable rows on PG (`_DualRow`) so `row["col"]` works unchanged.

Skips cleanly without a reachable cluster (the `requires_pg` marker). To run:
stand up a throwaway PG and export M3_PRIMARY_PG_URL — see the m3 RUNBOOK
"stand up a throwaway local PostgreSQL to test m3 on the PG backend".
"""
from __future__ import annotations

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
    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    b = PostgresBackend(dsn=_DSN)
    # Deterministic schema on the shared cluster: drop every public table, rebuild.
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


def _seed(pg):
    """4 core memories, 2 explicit edges, and a shared entity between an
    edge-orphan (m-ddd) and m-bbb — so co-mention clustering has something to do.
    Note: `pinned` is INTEGER on PG (pass int, not bool); memory_relationships.id
    is NOT-NULL with no default (supply a uuid)."""
    rows = [
        ("m-aaa", "belief", "Alpha Root", "Alpha body", 0.9, 0.91, 1),
        ("m-bbb", "belief", "Alpha Detail", "Beta body", 0.8, 0.80, 0),
        ("m-ccc", "procedure", "Alpha Runbook", "Gamma body", 0.7, None, 0),
        ("m-ddd", "reference", "Beta Standalone", "Delta", 0.65, 0.5, 0),
    ]
    with pg.connection() as c:
        cur = c.cursor()
        for r in rows:
            cur.execute(
                "INSERT INTO memory_items (id,type,title,content,importance,"
                "confidence,pinned,is_deleted,valid_from,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s)",
                (r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                 "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"),
            )
        cur.execute(
            "INSERT INTO memory_relationships (id,from_id,to_id,relationship_type,created_at) "
            "VALUES (%s,'m-aaa','m-bbb','consolidates','2026-01-01'),"
            "(%s,'m-aaa','m-ccc','related','2026-01-01')",
            (str(uuid.uuid4()), str(uuid.uuid4())),
        )
        cur.execute(
            "INSERT INTO entities (id,canonical_name,entity_type) "
            "VALUES ('ent-x','AlphaThing','concept') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO memory_item_entities (memory_id,entity_id) "
            "VALUES ('m-ddd','ent-x'),('m-bbb','ent-x') ON CONFLICT DO NOTHING"
        )


def _build_via_seam():
    """Build the vault through m3's backend seam, exactly as `m3 wiki generate`
    does on a PG deployment (memory read via _db(); no files corpus)."""
    from memory.db import _db
    from wiki.build import WikiOptions, build_wiki

    with _db() as mem_conn:
        return build_wiki(
            mem_conn, None,
            WikiOptions(importance_threshold=0.6, include_files=False,
                        use_networkx=False, entity_comention=True),
        )


def test_wiki_generates_on_postgres(pg):
    _seed(pg)
    vault = _build_via_seam()

    # Core structural pages exist — proves the whole pipeline ran on PG.
    assert "index.md" in vault
    assert "overview.md" in vault
    # All 4 core memories were selected (dialect placeholders + pinned/type query).
    blob = "\n".join(vault.values())
    for mid in ("m-aaa", "m-bbb", "m-ccc", "m-ddd"):
        assert mid in blob, f"{mid} missing from PG-generated vault"


def test_entity_comention_clusters_on_postgres(pg):
    """The path that would crash under a transaction-poisoning column probe:
    _has_column must succeed on PG so the entity-co-mention read runs, clustering
    the edge-orphan m-ddd together with m-bbb via their shared entity."""
    _seed(pg)
    vault = _build_via_seam()

    topic_pages = [t for p, t in vault.items()
                   if p.startswith("topics/") and p != "topics/orphans.md"]
    # m-ddd has NO explicit edge; only the shared entity binds it. If co-mention
    # worked on PG, ddd shares a topic page with the alpha cluster.
    shared = [t for t in topic_pages if "m-ddd" in t and "m-aaa" in t]
    assert shared, "entity co-mention did not cluster m-ddd on PostgreSQL"
    # And m-ddd must NOT be sitting in the orphans appendix.
    assert "m-ddd" not in vault.get("topics/orphans.md", "")


def test_has_column_absent_does_not_poison_txn(pg):
    """A missing column must be reported absent WITHOUT aborting the PG
    transaction — otherwise every later query in the build fails. We drop the
    entity table so _has_column returns False, and assert the build still
    completes and produces pages (proving the connection wasn't poisoned)."""
    _seed(pg)
    with pg.connection() as c:
        c.cursor().execute("DROP TABLE IF EXISTS memory_item_entities CASCADE")

    vault = _build_via_seam()  # must not raise
    assert "index.md" in vault
    # Core memories still present; only the co-mention signal is gone.
    assert "m-aaa" in "\n".join(vault.values())
