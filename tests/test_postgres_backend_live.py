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
