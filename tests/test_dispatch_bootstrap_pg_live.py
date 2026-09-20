"""The dispatch schema must bootstrap itself on PostgreSQL.

⚠ WHY THIS EXISTS. `memory/dispatch_migrations/postgres/pg_001_bootstrap.up.sql`
shipped with NO APPLIER anywhere in the tree. `M3Context.get_dispatch_conn`
returns early on a server backend and used to yield the connection untouched,
so the schema was never created and every send on PostgreSQL raised

    relation "m3_dispatch.notification_dispatch" does not exist

while both SQLite arms of the same function had always bootstrapped. That is §3's
"a declared capability with no enforcement site": DDL that ships, is documented,
and never runs.

The failure was durable rather than transient, and it hid well: once any other
path happened to create the schema, the test passed for the wrong reason. These
tests DROP the schema first so they measure the bootstrap, not the leftovers.

Skips cleanly without a reachable cluster (requires_pg). See
~/.m3-private/runbooks/WSL_POSTGRES_TEST_DB_RUNBOOK.md to stand one up.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

pytestmark = pytest.mark.requires_pg


@pytest.fixture()
def pg_no_dispatch(monkeypatch, pg_url):
    """A PG store with the dispatch schema REMOVED.

    Dropping first is the whole point: the schema persists between runs, so a
    test that merely asserts it exists passes on residue from an earlier run
    even with the bootstrap deleted. Measured — that is exactly what happened
    on the first attempt to prove this fix.
    """
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", pg_url)
    monkeypatch.setenv("M3_PRIMARY_PG_URL", pg_url)
    monkeypatch.setenv("M3_CORE_RS_DISABLE", "1")

    from memory.backends import selector as _selector
    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    from m3_core.context import _DISPATCH_PG_READY
    from m3_core.paths import dispatch_pg_schema

    schema = dispatch_pg_schema()
    b = PostgresBackend(dsn=pg_url)
    with b.connection() as c:
        cur = c.cursor()
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        c.commit()
    # The applier memoises per schema name; clear it or the drop is invisible.
    _DISPATCH_PG_READY.discard(schema)

    yield b, schema
    b.close()


def _schema_exists(backend, schema: str) -> bool:
    with backend.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,))
        return cur.fetchone() is not None


def _tables(backend, schema: str) -> set:
    with backend.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = %s",
                    (schema,))
        return {r[0] for r in cur.fetchall()}


def test_opening_a_dispatch_conn_creates_the_schema(pg_no_dispatch):
    """⚠ THE DEFECT. Asking for a dispatch connection must leave a usable store.

    Asserted from a genuinely absent schema, so this measures the bootstrap
    rather than a leftover from an earlier run.
    """
    backend, schema = pg_no_dispatch
    assert not _schema_exists(backend, schema), "fixture failed to drop the schema"

    from m3_core.context import M3Context

    ctx = M3Context.for_db(None)
    with ctx.get_dispatch_conn() as conn:
        assert conn is not None

    assert _schema_exists(backend, schema), (
        f"opening a dispatch connection did not create schema {schema!r} — "
        f"every send on PostgreSQL will raise 'relation does not exist'"
    )
    created = _tables(backend, schema)
    assert "notification_dispatch" in created, (
        f"schema {schema!r} exists but the dispatch table is missing: "
        f"{sorted(created)}"
    )


def test_the_bootstrap_is_idempotent(pg_no_dispatch):
    """It runs on every dispatch connection; a second call must be harmless."""
    from m3_core.context import M3Context

    backend, schema = pg_no_dispatch
    ctx = M3Context.for_db(None)
    for _ in range(3):
        with ctx.get_dispatch_conn() as conn:
            assert conn is not None
    assert "notification_dispatch" in _tables(backend, schema)


def test_the_schema_name_is_configurable(monkeypatch, pg_url):
    """⚠ THE DDL IS UNQUALIFIED ON PURPOSE. Its header says the caller sets
    search_path so one file serves any fleet's schema name. If the applier ever
    hardcodes 'm3_dispatch', several m3 fleets can no longer share one server.
    """
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", pg_url)
    monkeypatch.setenv("M3_PRIMARY_PG_URL", pg_url)
    monkeypatch.setenv("M3_DISPATCH_PG_SCHEMA", "m3_dispatch_alt")

    from memory.backends import selector as _selector
    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    from m3_core.context import _DISPATCH_PG_READY, M3Context
    _DISPATCH_PG_READY.discard("m3_dispatch_alt")

    backend = PostgresBackend(dsn=pg_url)
    try:
        with backend.connection() as c:
            cur = c.cursor()
            cur.execute('DROP SCHEMA IF EXISTS "m3_dispatch_alt" CASCADE')
            c.commit()

        ctx = M3Context.for_db(None)
        with ctx.get_dispatch_conn() as conn:
            assert conn is not None

        assert _schema_exists(backend, "m3_dispatch_alt"), (
            "M3_DISPATCH_PG_SCHEMA was ignored — the applier hardcodes a name"
        )
        with backend.connection() as c:
            cur = c.cursor()
            cur.execute('DROP SCHEMA IF EXISTS "m3_dispatch_alt" CASCADE')
            c.commit()
    finally:
        _DISPATCH_PG_READY.discard("m3_dispatch_alt")
        backend.close()


def test_the_shipped_pg_ddl_has_an_applier():
    """Runs without a cluster: the file must be referenced by real code.

    The original defect was not a wrong query — it was DDL nobody called. A
    grep-level assertion catches that class directly, and would have failed on
    the shipped tree.
    """
    import re

    hits = []
    for path in (_BIN).rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        body = "\n".join(ln for ln in text.splitlines()
                         if not ln.lstrip().startswith("#"))
        if re.search(r"pg_001_bootstrap", body):
            hits.append(path.name)
    assert hits, (
        "memory/dispatch_migrations/postgres/pg_001_bootstrap.up.sql is "
        "referenced by no executable code — shipped DDL with no applier never "
        "runs, which is how the dispatch schema went missing on PostgreSQL"
    )
