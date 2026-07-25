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


def _build_via_seam(admission_gate: bool = False):
    """Build the vault through m3's backend seam, exactly as `m3 wiki generate`
    does on a PG deployment (memory read via _db(); no files corpus).

    admission_gate defaults OFF: these tests validate the PG-specific read paths
    (dialect placeholders, _has_column, co-mention) on a tiny 4-memory fixture.
    The gate demotes weakly-anchored clusters, and a 4-node fixture cluster is
    exactly that — with the gate ON it lands every member in orphans, masking the
    clustering/co-mention behaviour under test. The gate has its own suite
    (test_wiki_admission.py); mirror the SQLite determinism tests, which also
    build with admission_gate=False for the same reason."""
    from memory.db import _db
    from wiki.build import WikiOptions, build_wiki

    with _db() as mem_conn:
        return build_wiki(
            mem_conn, None,
            WikiOptions(importance_threshold=0.6, include_files=False,
                        entity_comention=True, admission_gate=admission_gate),
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


def _seed_files_schema(pg):
    """Create the `files` schema (via the files-store seam) and seed the minimal
    chain — file_node → ingestion_run → leaf → fact — so both the file_node read
    and the _load_facts read are exercised on PG. Returns the seeded file uuid."""
    from files_memory import db as fdb
    fdb._initialized_dbs.clear()
    fdb.init_db()  # creates the `files` schema + applies pg_ files migrations
    fn = f"fn-{uuid.uuid4().hex[:8]}"
    run = f"run-{uuid.uuid4().hex[:8]}"
    leaf = f"lf-{uuid.uuid4().hex[:8]}"
    with fdb._db() as c:
        c.execute(
            "INSERT INTO files.file_nodes "
            "(uuid,identity_key,filename,filetype,path_absolute,size_bytes,"
            " content_sha256,date_modified,source_host,version_label,file_summary) "
            "VALUES (%s,'k','design.md','markdown','/design.md',10,'sha','2026','h','v1',"
            "'A design note about the auth service.')",
            (fn,),
        )
        c.execute(
            "INSERT INTO files.ingestion_runs "
            "(uuid,file_node,run_id,ingester_version,chunker_version,extract_mode) "
            "VALUES (%s,%s,'r1','iv','cv','mode')",
            (run, fn),
        )
        c.execute(
            "INSERT INTO files.leaves "
            "(uuid,file_node,ingestion_run,division_type,division_id,text,"
            " text_sha256,char_range_start,char_range_end) "
            "VALUES (%s,%s,%s,'heading','h1','body','lsha',0,4)",
            (leaf, fn, run),
        )
        c.execute(
            "INSERT INTO files.facts "
            "(uuid,leaf,file_node,statement,source_span_start,source_span_end,"
            " confidence,extraction_run) "
            "VALUES (%s,%s,%s,'The service signs JWTs with RS256.',0,4,0.9,%s)",
            (f"fact-{uuid.uuid4().hex[:8]}", leaf, fn, run),
        )
    return fn


def test_files_corpus_visible_on_postgres(pg):
    """The audit fix: the wiki's files corpus must render on a PG deployment, read
    from the `files` schema via the files-store seam — NOT silently dropped because
    there is no files_database.db sidecar. Seeds a file_node + fact into files.*,
    builds with include_files=True through both seams, and asserts the file surfaces."""
    _seed(pg)
    _seed_files_schema(pg)

    from files_memory.db import _db as _files_seam
    from memory.db import _db as _mem_seam
    from wiki.build import WikiOptions, build_wiki

    with _mem_seam() as mem_conn, _files_seam() as files_conn:
        vault = build_wiki(
            mem_conn, files_conn,
            WikiOptions(importance_threshold=0.6, include_files=True,
                        admission_gate=False),
        )
    blob = "\n".join(vault.values())
    # The file_node's summary became a sources/* page and the fact is evidence.
    assert "design.md" in blob, "files corpus not read from the PG files schema"
    assert "RS256" in blob, "the file's fact was not loaded on PG"


def test_files_layer_reads_pg_schema_directly(pg):
    """Unit-level proof that load_files_layer reads the PG `files` schema through
    the seam (files_table-qualified names + dialect placeholders)."""
    fn = _seed_files_schema(pg)
    from files_memory.db import _db as _files_seam
    from wiki.files_layer import load_files_layer

    with _files_seam() as c:
        layer = load_files_layer(c)
    assert any(f.uuid == fn for f in layer.files), "file_node not loaded from files schema"
    seeded = next(f for f in layer.files if f.uuid == fn)
    assert seeded.filename == "design.md"
    assert any("RS256" in fact.statement for fact in seeded.facts)
