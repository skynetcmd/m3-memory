"""Backend-aware files-store connection + migration runner (Phase 1 slice 4).

files_memory.db._db/init_db now route through the seam:
  - SQLite: a separate files DB file, schema built by numbered migrations
    (files_memory/migrations/) instead of the old executescript(SCHEMA_V1/2/3).
  - PostgreSQL: the primary DB's `files` schema (single DSN), addressed by
    files_table()-qualified names — no search_path.

The SQLite tests run by default; the PG test is requires_pg (skips without a
reachable cluster). Together they prove the same code path builds and read/writes
the files store on both backends.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

_FILE_NODE_COLS = (
    "uuid,identity_key,filename,filetype,path_absolute,size_bytes,"
    "content_sha256,date_modified,source_host,version_label"
)
_FILE_NODE_VALS = "'u1','k','f.md','markdown','/f.md',10,'sha','2020','h','v1'"


@pytest.fixture()
def sqlite_files(tmp_path, monkeypatch):
    """A fresh SQLite files store wired via M3_FILES_DB_PATH."""
    monkeypatch.setenv("M3_FILES_DB_PATH", str(tmp_path / "files.db"))
    monkeypatch.setenv("M3_CORE_RS_DISABLE", "1")
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    monkeypatch.delenv("M3_PRIMARY_PG_URL", raising=False)
    monkeypatch.delenv("M3_PG_URL", raising=False)
    # The backend selector caches the active backend; a prior PG test may have left
    # it on postgres. Reset so _is_postgres() sees the sqlite env set above —
    # otherwise the migration runner emits PG-qualified `files.` names against a
    # SQLite connection (test-pollution OperationalError, hit 2026-07-24).
    from memory.backends import selector
    selector._reset_for_tests()
    import importlib

    import files_memory.config as fc
    importlib.reload(fc)
    import files_memory.db as fdb
    importlib.reload(fdb)
    fdb._initialized_dbs.clear()
    yield fdb
    # Reset again so we don't leak a sqlite-forced selector into later tests.
    selector._reset_for_tests()


def test_sqlite_init_applies_numbered_migrations(sqlite_files):
    """init_db builds the schema from migrations/ (not the inline blobs) and stamps
    schema_versions with every version (004 is a SQLite no-op FTS marker)."""
    fdb = sqlite_files
    fdb.init_db()
    with fdb._db() as db:
        versions = [r[0] for r in db.execute("SELECT version FROM schema_versions ORDER BY version")]
    assert versions == [1, 2, 3, 4]


def test_sqlite_schema_has_expected_tables_and_fts(sqlite_files):
    """The migrated SQLite schema carries the core tables AND the FTS5 shadows
    (FTS5 is SQLite-only; PG uses a native tsvector column instead)."""
    fdb = sqlite_files
    fdb.init_db()
    path = os.environ["M3_FILES_DB_PATH"]
    conn = sqlite3.connect(path)
    tabs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    for t in ("file_nodes", "ingestion_runs", "leaves", "leaf_embeddings",
              "facts", "promotion_markers", "memory_links", "corpus_settings",
              "extraction_attempts", "fact_hit_stats", "semantic_dedup_candidates"):
        assert t in tabs, f"missing {t}"
    assert "leaves_fts" in tabs and "file_summaries_fts" in tabs


def test_sqlite_init_is_idempotent(sqlite_files):
    """Re-init applies nothing new (all versions already stamped)."""
    fdb = sqlite_files
    fdb.init_db()
    fdb._initialized_dbs.clear()   # force re-entry into init_db
    fdb.init_db()                  # must not raise or double-apply
    with fdb._db() as db:
        assert [r[0] for r in db.execute("SELECT version FROM schema_versions ORDER BY version")] == [1, 2, 3, 4]


def test_sqlite_read_write_through_db(sqlite_files):
    """_db() yields a working connection; a write is visible in the next block."""
    fdb = sqlite_files
    with fdb._db() as db:
        db.execute(f"INSERT INTO file_nodes ({_FILE_NODE_COLS}) VALUES ({_FILE_NODE_VALS})")
    with fdb._db() as db:
        assert db.execute("SELECT COUNT(*) FROM file_nodes").fetchone()[0] == 1


def test_integrity_check_runs_on_sqlite(sqlite_files):
    """The SQLite-only integrity check still runs and reports ok on a fresh DB."""
    fdb = sqlite_files
    out = fdb.integrity_check()
    assert out["ok"] is True
    assert out["sqlite_integrity"] == "ok"
    assert out["fts5_in_sync"] is True


# ── PostgreSQL: files store lives in the `files` schema of the primary DB ──────

pg = pytest.mark.requires_pg


@pytest.fixture()
def pg_files(monkeypatch, pg_url):
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PRIMARY_PG_URL", pg_url)
    monkeypatch.setenv("M3_PG_URL", pg_url)
    monkeypatch.setenv("M3_CORE_RS_DISABLE", "1")
    import psycopg2
    c = psycopg2.connect(pg_url); c.autocommit = True
    c.cursor().execute("DROP SCHEMA IF EXISTS files CASCADE"); c.close()
    from memory.backends import selector
    selector._reset_for_tests()
    import importlib

    import files_memory.config as fc
    importlib.reload(fc)
    import files_memory.db as fdb
    importlib.reload(fdb)
    fdb._initialized_dbs.clear()
    yield fdb
    c = psycopg2.connect(pg_url); c.autocommit = True
    c.cursor().execute("DROP SCHEMA IF EXISTS files CASCADE"); c.close()


@pg
def test_pg_init_creates_files_schema_and_applies_migrations(pg_files):
    """init_db creates the `files` schema and applies pg_ migrations into it."""
    fdb = pg_files
    fdb.init_db()
    from memory.db import _db as seam_db
    with seam_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='files'")
        tabs = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT version FROM files.schema_versions ORDER BY version")
        versions = [r[0] for r in cur.fetchall()]
        # pg_004 adds the tsvector search_vector columns + GIN indexes.
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='files' AND table_name='leaves' "
                    "AND column_name='search_vector'")
        has_search_vector = cur.fetchone() is not None
    assert versions == [1, 2, 3, 4]
    assert has_search_vector, "pg_004 should add files.leaves.search_vector"
    for t in ("file_nodes", "leaves", "facts", "promotion_markers"):
        assert t in tabs
    # FTS5 shadow tables never exist on PG: full-text there is the native
    # tsvector search_vector column (asserted above), not FTS5 virtual tables.
    assert "leaves_fts" not in tabs and "file_summaries_fts" not in tabs


@pg
def test_pg_read_write_routes_to_files_schema(pg_files):
    """files_memory._db() writes/reads the `files` schema via the primary pool."""
    fdb = pg_files
    with fdb._db() as db:
        db.execute(
            f"INSERT INTO files.file_nodes ({_FILE_NODE_COLS}) VALUES ({_FILE_NODE_VALS})"
        )
    with fdb._db() as db:
        r = db.execute("SELECT COUNT(*) AS c FROM files.file_nodes").fetchone()
    assert (r["c"] if hasattr(r, "keys") else r[0]) == 1


@pg
def test_pg_integrity_check_skips_cleanly(pg_files):
    """integrity_check is SQLite-only; on PG it reports a clean skip, never raises."""
    fdb = pg_files
    fdb.init_db()
    out = fdb.integrity_check()
    assert "skipped" in out
    assert out["ok"] is True


def _seed_leaf(db, *, fn_uuid, run_uuid, leaf_uuid, text, div_id):
    """Seed the minimum file_node + ingestion_run + leaf needed for a keyword
    search hit. Qualified files.* names (PG). The generated search_vector column
    populates automatically on INSERT (no trigger)."""
    db.execute(
        "INSERT INTO files.file_nodes "
        "(uuid,identity_key,filename,filetype,path_absolute,size_bytes,"
        " content_sha256,date_modified,source_host,version_label) "
        "VALUES (%s,'k',%s,'markdown',%s,10,'sha','2020','h','v1') "
        "ON CONFLICT (uuid) DO NOTHING",
        (fn_uuid, f"{fn_uuid}.md", f"/{fn_uuid}.md"),
    )
    db.execute(
        "INSERT INTO files.ingestion_runs "
        "(uuid,file_node,run_id,ingester_version,chunker_version,extract_mode) "
        "VALUES (%s,%s,'r1','iv','cv','mode') ON CONFLICT (uuid) DO NOTHING",
        (run_uuid, fn_uuid),
    )
    db.execute(
        "INSERT INTO files.leaves "
        "(uuid,file_node,ingestion_run,division_type,division_id,text,"
        " text_sha256,char_range_start,char_range_end) "
        "VALUES (%s,%s,%s,'heading',%s,%s,'lsha',0,10)",
        (leaf_uuid, fn_uuid, run_uuid, div_id, text),
    )


@pg
def test_pg_files_search_keyword_channel_tsvector(pg_files):
    """files_search's keyword channel runs on PG via the generated tsvector
    column (@@ to_tsquery / ts_rank), NOT FTS5 — the Phase-2 parity payoff. Seeds
    two leaves with distinct text and asserts the query hits the right one. The
    vector channel returns nothing (no embeddings seeded), so a hit here isolates
    the tsvector keyword path."""
    fdb = pg_files
    fdb.init_db()
    import uuid as _uuid
    fn = f"fn-{_uuid.uuid4().hex[:8]}"
    run = f"run-{_uuid.uuid4().hex[:8]}"
    target = f"lf-{_uuid.uuid4().hex[:8]}"
    other = f"lf-{_uuid.uuid4().hex[:8]}"
    with fdb._db() as db:
        _seed_leaf(db, fn_uuid=fn, run_uuid=run, leaf_uuid=target,
                   text="the quick brown fox jumps over the lazy dog", div_id="h1")
        _seed_leaf(db, fn_uuid=fn, run_uuid=run, leaf_uuid=other,
                   text="completely unrelated content about umbrellas", div_id="h2")

    from files_memory.search import files_search
    hits = files_search("brown fox", limit=10)
    hit_ids = {h.leaf_uuid for h in hits}
    assert target in hit_ids, f"tsvector keyword channel should surface the fox leaf; got {hit_ids}"
    assert other not in hit_ids, "the unrelated leaf must not match 'brown fox'"
    # the matching hit carries an fts_rank (came through the keyword channel)
    fox = next(h for h in hits if h.leaf_uuid == target)
    assert fox.fts_rank is not None
