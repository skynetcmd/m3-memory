"""Phase 1b live integration tests for the PostgreSQL backend.

These require a reachable PostgreSQL (the dev throwaway cluster). They SKIP
cleanly when none is configured/reachable, so the suite stays green on machines
and CI without Postgres. To run them, point M3_PG_URL at a throwaway cluster:

    # stand up the dev cluster (WSL), then point M3_PG_URL at it, e.g.:
    export M3_PG_URL="postgresql://<user>:<pass>@127.0.0.1:5433/<db>"
    pytest tests/test_postgres_backend_live.py -q

SAFETY: these tests CREATE and DROP their own temp tables (prefixed `_it_`).
They refuse to run against a production/warehouse hub (configured via
M3_PG_FORBIDDEN_HOSTS) as a guard against a misconfigured DSN.
"""
from __future__ import annotations

import os

import pytest

# Hosts these destructive tests must never touch. The production CDW hub is
# supplied via env (M3_PG_FORBIDDEN_HOSTS, comma-separated) so no internal
# infrastructure address is hardcoded in source. Defaults to empty.
_FORBIDDEN = [
    h.strip() for h in os.environ.get("M3_PG_FORBIDDEN_HOSTS", "").split(",") if h.strip()
]


def _dsn() -> str | None:
    url = (os.environ.get("M3_PG_URL") or os.environ.get("PG_URL") or "").strip()
    return url or None


def _reachable(dsn: str) -> bool:
    try:
        import psycopg2

        conn = psycopg2.connect(dsn, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


_DSN = _dsn()
pytestmark = pytest.mark.skipif(
    _DSN is None or not _reachable(_DSN),
    reason="no reachable PostgreSQL (set M3_PG_URL to a throwaway cluster)",
)


@pytest.fixture()
def backend(monkeypatch):
    """A PostgresBackend bound to the throwaway cluster, pool torn down after."""
    assert _DSN is not None
    for forbidden in _FORBIDDEN:
        if forbidden in _DSN:
            pytest.fail(
                f"refusing to run destructive tests against forbidden host {forbidden}"
            )
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)
    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    b = PostgresBackend(dsn=_DSN)
    yield b
    b.close()


def test_backend_identity_and_dialect(backend):
    assert backend.name == "postgres"
    assert backend.dialect().backend == "postgres"
    assert backend.placeholder(3) == "%s, %s, %s"


def test_connection_commits_on_success(backend):
    with backend.connection() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS _it_commit(id int primary key, v text)")
        cur.execute("INSERT INTO _it_commit(id, v) VALUES (1, 'kept')")
    # separate connection sees the committed row
    with backend.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT v FROM _it_commit WHERE id = 1")
        assert cur.fetchone()[0] == "kept"
        cur.execute("DROP TABLE _it_commit")


def test_connection_rolls_back_on_exception(backend):
    with backend.connection() as conn:
        conn.cursor().execute(
            "CREATE TABLE IF NOT EXISTS _it_rb(id int primary key, v text)"
        )
    with pytest.raises(RuntimeError):
        with backend.connection() as conn:
            conn.cursor().execute("INSERT INTO _it_rb(id, v) VALUES (2, 'doomed')")
            raise RuntimeError("boom")
    with backend.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM _it_rb WHERE id = 2")
        assert cur.fetchone()[0] == 0  # rolled back
        cur.execute("DROP TABLE _it_rb")


def test_pool_serves_concurrent_connections(backend):
    """The pool must hand out multiple simultaneous connections (the whole point
    of PostgreSQL here: 10-100s of concurrent users, not single-connect)."""
    import concurrent.futures as cf

    def one_query(i: int) -> int:
        with backend.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT %s", (i,))
            return cur.fetchone()[0]

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = sorted(ex.map(one_query, range(24)))
    assert results == list(range(24))


def test_capabilities_baseline(backend):
    caps = backend.capabilities()
    assert caps.backend == "postgres"
    assert caps.keyword == "tsvector"
    # pgvector may or may not be present; either is a valid baseline. If absent,
    # the vector accelerator must be 'none' (never an error).
    assert caps.vector_accelerator in ("none", "pgvector")


def test_placeholder_zero_fails_loud(backend):
    with pytest.raises(ValueError):
        backend.placeholder(0)


def test_ensure_schema_creates_primary_schema(backend):
    """ensure_schema() applies pg_primary_v1.sql on a fresh DB (the PG analogue of
    SQLite's lazy _lazy_init) and is idempotent."""
    # Simulate a fresh deployment: drop the core tables.
    with backend.connection() as conn:
        conn.cursor().execute(
            "DROP TABLE IF EXISTS memory_embeddings, memory_relationships, "
            "memory_items, schema_versions CASCADE"
        )
    backend._schema_ready = False  # allow a fresh apply after the drop

    backend.ensure_schema()

    with backend.connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name IN ('memory_items','memory_embeddings',"
            "'memory_relationships','schema_versions')"
        )
        assert cur.fetchone()[0] == 4
        cur.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'memory_items'"
        )
        assert cur.fetchone()[0] == 37  # full primary schema (36 + search_vector)

    # Idempotent: re-applying the SQL (IF NOT EXISTS throughout) must not error.
    backend._schema_ready = False
    backend.ensure_schema()

    # Version stamp: the cumulative schema records baseline version 39, once.
    assert backend.schema_version() == 39
    with backend.connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM schema_versions WHERE version = 39")
        assert cur.fetchone()[0] == 1  # no duplicate on re-apply


def test_schema_version_none_when_uninitialized(backend):
    """schema_version() returns None when schema_versions is absent (fresh DB)."""
    with backend.connection() as conn:
        conn.cursor().execute("DROP TABLE IF EXISTS schema_versions CASCADE")
    assert backend.schema_version() is None
    # restore for other tests
    backend._schema_ready = False
    backend.ensure_schema()


def test_dual_row_positional_and_named_access(backend):
    """Rows from the compat connection support BOTH row[0] and row['col'] like
    sqlite3.Row — the codebase uses both styles; a row that fails one is a latent
    PG bug (regression: RealDictCursor gave dicts, so row[0] raised KeyError)."""
    with backend.connection() as conn:
        cur = conn.execute("SELECT 42 AS answer, 'hi' AS greeting")
        row = cur.fetchone()
        assert row[0] == 42                    # positional
        assert row["answer"] == 42             # named
        assert row[1] == "hi"
        assert row["greeting"] == "hi"
        assert list(row) == [42, "hi"]         # tuple-iterable
        assert len(row) == 2
        assert row.get("missing", "d") == "d"  # sqlite3.Row-like .get


def test_cas_supersede_exactly_one_winner(backend):
    """The CAS close (UPDATE ... WHERE is_deleted=0, guard on rowcount==1) must
    yield exactly ONE winner under concurrent transactions on PG — the invariant
    the contradiction-supersession fix relies on. Without it, N concurrent
    contradiction-writes each close the row and each write a 'supersedes' edge."""
    import concurrent.futures as cf
    import threading

    dsn = _DSN
    import psycopg2

    def setup():
        c = psycopg2.connect(dsn)
        c.autocommit = True
        cur = c.cursor()
        cur.execute("DROP TABLE IF EXISTS cas_probe")
        cur.execute("CREATE TABLE cas_probe(id TEXT PRIMARY KEY, is_deleted INT DEFAULT 0)")
        cur.execute("INSERT INTO cas_probe(id) VALUES ('X')")
        c.close()

    def racer(barrier):
        conn = psycopg2.connect(dsn)
        cur = conn.cursor()
        barrier.wait()
        cur.execute("UPDATE cas_probe SET is_deleted=1 WHERE id='X' AND is_deleted=0")
        won = cur.rowcount == 1
        conn.commit()
        conn.close()
        return won

    for n in (2, 5, 10):
        setup()
        barrier = threading.Barrier(n)
        with cf.ThreadPoolExecutor(max_workers=n) as ex:
            wins = sum(ex.map(lambda _: racer(barrier), range(n)))
        assert wins == 1, f"N={n}: expected exactly 1 CAS winner, got {wins}"

    c = psycopg2.connect(dsn)
    c.autocommit = True
    c.cursor().execute("DROP TABLE IF EXISTS cas_probe")
    c.close()


def test_keyword_search_tsvector(backend):
    """PG keyword_search: tsvector @@ tsquery, title-weighted, lower=better."""
    # Requires the tsvector column; create a minimal memory_items if absent.
    with backend.connection() as conn:
        cur = conn.cursor()
        # Own the schema (live tests share memory_items with varying shapes):
        # drop and build the exact columns this test needs.
        cur.execute("DROP TABLE IF EXISTS memory_items CASCADE")
        cur.execute(
            """
            CREATE TABLE memory_items (
                id TEXT PRIMARY KEY, type TEXT, title TEXT, content TEXT,
                is_deleted INTEGER DEFAULT 0, user_id TEXT DEFAULT '',
                scope TEXT DEFAULT 'agent',
                search_vector tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('english', coalesce(title,'')), 'A') ||
                    setweight(to_tsvector('english', coalesce(content,'')), 'B')
                ) STORED
            )
            """
        )
        for _id, title, content in [
            ("kw_a", "postgresql tuning", "shared buffers"),
            ("kw_b", "database notes", "we evaluated postgresql"),
            ("kw_c", "lunch menu", "tacos and salad"),
        ]:
            # `type` is NOT NULL in the full schema — always supply it.
            cur.execute(
                "INSERT INTO memory_items (id, type, title, content) "
                "VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET title=excluded.title, "
                "content=excluded.content",
                (_id, "note", title, content),
            )

    with backend.connection() as conn:
        hits = backend.keyword_search(conn, "postgresql", limit=10)
    ids = [h.memory_id for h in hits]
    assert set(ids) == {"kw_a", "kw_b"}          # kw_c excluded
    assert ids[0] == "kw_a"                        # title hit ranks first
    assert hits[0].score <= hits[1].score          # lower = better

    with backend.connection() as conn:
        assert backend.keyword_search(conn, "!!!", limit=10) == []  # empty compile
        conn.cursor().execute(
            "DELETE FROM memory_items WHERE id IN ('kw_a','kw_b','kw_c')"
        )


def test_vector_search_matches_sqlite(backend):
    """PG vector_search (BYTEA + Rust cosine) must produce IDENTICAL ordering and
    scores to SQLite for the same vectors — the seam invariant."""
    import sqlite3
    import struct

    dim = 4
    def blob(v):
        return struct.pack(f"{len(v)}f", *v)

    query = [1.0, 0.0, 0.0, 0.0]
    cands = [
        ("vp_near", [0.9, 0.1, 0.0, 0.0]),
        ("vp_mid", [0.5, 0.5, 0.5, 0.5]),
        ("vp_neg", [-1.0, 0.0, 0.0, 0.0]),
    ]

    # SQLite reference
    from memory.backends.sqlite_backend import SqliteBackend

    sconn = sqlite3.connect(":memory:")
    sconn.executescript(
        "CREATE TABLE memory_items(id TEXT PRIMARY KEY, is_deleted INTEGER DEFAULT 0, user_id TEXT DEFAULT '');"
        "CREATE TABLE memory_embeddings(memory_id TEXT, embedding BLOB, dim INTEGER, embed_model TEXT);"
    )
    for mid, vec in cands:
        sconn.execute("INSERT INTO memory_items(id) VALUES (?)", (mid,))
        sconn.execute(
            "INSERT INTO memory_embeddings(memory_id,embedding,dim,embed_model) VALUES (?,?,?,?)",
            (mid, blob(vec), dim, "m"),
        )
    sconn.commit()
    s_hits = SqliteBackend().vector_search(
        sconn, query, limit=10, dim=dim, embed_models=("m",)
    )

    # Postgres under test
    with backend.connection() as conn:
        cur = conn.cursor()
        cur.execute("DROP TABLE IF EXISTS memory_embeddings CASCADE")
        cur.execute("DROP TABLE IF EXISTS memory_items CASCADE")
        cur.execute(
            "CREATE TABLE memory_items(id TEXT PRIMARY KEY, type TEXT DEFAULT 'note', "
            "is_deleted INTEGER DEFAULT 0, user_id TEXT DEFAULT '')"
        )
        cur.execute(
            "CREATE TABLE memory_embeddings(memory_id TEXT, embedding BYTEA, dim BIGINT, embed_model TEXT)"
        )
        for mid, vec in cands:
            cur.execute("INSERT INTO memory_items(id) VALUES (%s)", (mid,))
            cur.execute(
                "INSERT INTO memory_embeddings(memory_id,embedding,dim,embed_model) VALUES (%s,%s,%s,%s)",
                (mid, blob(vec), dim, "m"),
            )
    with backend.connection() as conn:
        p_hits = backend.vector_search(conn, query, limit=10, dim=dim, embed_models=("m",))

    s = [(h.memory_id, round(h.score, 5)) for h in s_hits]
    p = [(h.memory_id, round(h.score, 5)) for h in p_hits]
    assert p == s, f"parity mismatch: sqlite={s} postgres={p}"
    assert p[0][0] == "vp_near"

    with backend.connection() as conn:
        conn.cursor().execute("DROP TABLE memory_embeddings CASCADE")
        conn.cursor().execute("DROP TABLE memory_items CASCADE")
