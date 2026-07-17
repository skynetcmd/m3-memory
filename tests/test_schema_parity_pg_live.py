"""Schema parity: the PostgreSQL primary schema must carry the same core tables
and columns as the SQLite schema.

The two schemas are maintained by hand in lock-step — SQLite via
memory/migrations/*.sql, PostgreSQL via memory/migrations/postgres/pg_primary_v1
+ pg_NNN. Nothing ENFORCED that they stay aligned, so a column added to a SQLite
migration but not mirrored into a pg_ migration would ship undetected and only
surface as a runtime error on a PG deployment. This test compares the column
NAME sets of the shared core tables and fails on any divergence.

We compare names, not types: TEXT vs TIMESTAMPTZ / INTEGER vs BIGINT are expected
representation differences, not drift. A missing or extra COLUMN is the drift we
care about. Chatlog `chat_log_*` tables are PG-only (SQLite keeps chatlog in a
separate file with core names), so they're excluded from the comparison set.

Skips cleanly without a reachable cluster. DSN from M3_PRIMARY_PG_URL/M3_PG_URL
(never PG_URL — that's the warehouse var).
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
_MIGRATIONS = Path(__file__).resolve().parents[1] / "memory" / "migrations"
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

# Core tables that both backends are expected to carry with the same columns.
# (Excludes: SQLite-only FTS shadow tables memory_items_fts*, the schema_versions
# bookkeeping table, and PG-only chat_log_* clones.)
_SHARED_CORE_TABLES = {
    "memory_items",
    "memory_embeddings",
    "memory_relationships",
    "memory_history",
    "entities",
    "memory_item_entities",
    "entity_relationships",
    "entity_extraction_queue",
    "observation_queue",
    "gdpr_requests",
}


def _sqlite_columns() -> dict:
    """Build a fresh SQLite schema through the full migration chain and return
    {table: set(column_names)} for the shared core tables."""
    import migrate_memory as m

    d = tempfile.mkdtemp()
    db = os.path.join(d, "parity.db")
    conn = sqlite3.connect(db)
    m.init_migrations_table(conn)
    migs = m.discover_migrations(str(_MIGRATIONS))
    for v in sorted(migs):
        m.apply_migration(conn, v, migs[v]["name"], migs[v]["up"])
    out: dict = {}
    for t in _SHARED_CORE_TABLES:
        rows = conn.execute(f"PRAGMA table_info({t})").fetchall()
        out[t] = {r[1] for r in rows}  # r[1] = column name
    conn.close()
    return out


def _pg_columns(backend) -> dict:
    """{table: set(column_names)} for the shared core tables on the live PG store."""
    out: dict = {}
    with backend.connection() as c:
        cur = c.cursor()
        for t in _SHARED_CORE_TABLES:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=%s",
                (t,),
            )
            out[t] = {r[0] for r in cur.fetchall()}
    return out


@pytest.fixture()
def pg(monkeypatch):
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)
    monkeypatch.setenv("M3_PRIMARY_PG_URL", _DSN)
    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    b = PostgresBackend(dsn=_DSN)
    # Own the schema deterministically on the shared cluster: drop every public
    # table so ensure_schema + the migration runner rebuild from scratch.
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


def test_core_tables_exist_on_both(pg):
    """Every shared core table must exist on PG (SQLite builds them by construction)."""
    pg_cols = _pg_columns(pg)
    missing = sorted(t for t in _SHARED_CORE_TABLES if not pg_cols.get(t))
    assert not missing, f"core tables missing from the PostgreSQL schema: {missing}"


# Columns that legitimately exist on ONE backend only (not drift):
#   - search_vector: PG tsvector FTS column (SQLite uses an FTS5 shadow table).
#   - stage1_kg_done / weight: PG-native accelerator columns with no SQLite
#     analogue (used by PG-only indexes / future KG staging).
# Keyed by table -> {backend: {allowed column names}}.
_ALLOWED_BACKEND_ONLY = {
    "memory_items": {"only_pg": {"search_vector", "stage1_kg_done"}},
    "memory_relationships": {"only_pg": {"weight"}},
}


def test_column_parity_sqlite_vs_pg(pg):
    """Column NAME sets must match per shared core table, except the small set of
    deliberately backend-only columns in _ALLOWED_BACKEND_ONLY. Any OTHER column
    present on one backend but not the other is schema drift — fix the lagging
    migration (this is exactly how the missing gdpr_requests / observation_queue.
    stage on PG were caught before pg_044)."""
    sq = _sqlite_columns()
    pgc = _pg_columns(pg)
    drift: dict = {}
    for t in sorted(_SHARED_CORE_TABLES):
        allow = _ALLOWED_BACKEND_ONLY.get(t, {})
        only_sqlite = sq.get(t, set()) - pgc.get(t, set()) - allow.get("only_sqlite", set())
        only_pg = pgc.get(t, set()) - sq.get(t, set()) - allow.get("only_pg", set())
        if only_sqlite or only_pg:
            drift[t] = {"only_sqlite": sorted(only_sqlite), "only_pg": sorted(only_pg)}
    assert not drift, (
        "schema drift between SQLite and PostgreSQL core tables — mirror the "
        f"missing columns into the lagging backend's migrations:\n{drift}"
    )


def test_chroma_tables_absent_on_both(pg):
    """The retired ChromaDB tables must be gone from BOTH schemas (SQLite migration
    040 / the PG baseline never recreates them)."""
    chroma = {"chroma_sync_queue", "chroma_mirror", "chroma_mirror_embeddings",
              "sync_conflicts", "sync_state"}
    # SQLite
    import migrate_memory as m
    d = tempfile.mkdtemp()
    db = os.path.join(d, "chroma_check.db")
    conn = sqlite3.connect(db)
    m.init_migrations_table(conn)
    migs = m.discover_migrations(str(_MIGRATIONS))
    for v in sorted(migs):
        m.apply_migration(conn, v, migs[v]["name"], migs[v]["up"])
    sq_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert not (chroma & sq_tables), f"chroma tables still in SQLite schema: {chroma & sq_tables}"
    # PG
    with pg.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        pg_tables = {r[0] for r in cur.fetchall()}
    assert not (chroma & pg_tables), f"chroma tables still in PG schema: {chroma & pg_tables}"
