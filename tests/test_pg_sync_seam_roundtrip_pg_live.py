"""The ported sync path, exercised against a LIVE PostgreSQL warehouse.

Phase 3 replaced pg_sync's hand-written SQL with seam primitives — placeholders
from the dialect, merges through `bulk_upsert`, error classification by meaning
rather than exception class. The existing unit tests drive that code with fakes
and in-memory SQLite, which proves the shapes but not that rows actually land.

This proves rows land, in both directions, against a real cluster:

  * PUSH — a local row reaches the warehouse.
  * PULL — a remote row reaches the local store.
  * The last-write-wins guard still discriminates after the conversion. A
    `bulk_upsert` that silently dropped `guard_sql` would turn every conditional
    merge into an unconditional overwrite, pass every shape test, and quietly
    lose data — which is exactly why the guard gets its own assertions here.
  * The manual/system protection survives, since it rides in the same fragment.

Skips cleanly without a reachable cluster. DSN from M3_PRIMARY_PG_URL/M3_PG_URL
(never PG_URL -- that is the warehouse var).
"""
from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

pytestmark = pytest.mark.requires_pg

_ITEM_COLS = (
    "id, type, title, content, metadata_json, agent_id, model_id, change_agent, "
    "importance, source, origin_device, is_deleted, expires_at, decay_rate, "
    "created_at, updated_at, user_id, scope, valid_from, valid_to, content_hash"
)


def _local_store(tmp_path):
    """A SQLite store with just the columns sync_memory_items touches."""
    conn = sqlite3.connect(tmp_path / "local.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        f"CREATE TABLE memory_items ({_ITEM_COLS.replace(', ', ' TEXT, ')} TEXT, "
        "PRIMARY KEY (id))"
    )
    conn.execute(
        "CREATE TABLE sync_watermarks "
        "(direction TEXT PRIMARY KEY, last_synced_at TEXT)"
    )
    conn.commit()
    return conn


@pytest.fixture()
def env(pg_url, tmp_path):
    """A live PG schema standing in for the warehouse, plus a local SQLite store."""
    import psycopg2

    pg = psycopg2.connect(pg_url)
    pg.autocommit = True
    ns = "t" + uuid.uuid4().hex[:8]
    cur = pg.cursor()
    cur.execute(f"CREATE SCHEMA {ns}")
    cur.execute(f"SET search_path TO {ns}")
    cur.execute(
        f"CREATE TABLE memory_items ({_ITEM_COLS.replace(', ', ' TEXT, ')} TEXT, "
        "PRIMARY KEY (id))"
    )
    local = _local_store(tmp_path)
    try:
        yield local, pg, ns
    finally:
        local.close()
        cur.execute(f"DROP SCHEMA IF EXISTS {ns} CASCADE")
        pg.close()


def _row(**kw):
    """One memory_items row as a 21-tuple, with sane defaults."""
    d = {
        "id": "i1", "type": "note", "title": "t", "content": "c",
        "metadata_json": "{}", "agent_id": "a", "model_id": "m",
        "change_agent": "auto", "importance": "1", "source": "s",
        "origin_device": "d", "is_deleted": "0", "expires_at": None,
        "decay_rate": None, "created_at": "2026-01-01", "updated_at": "2026-01-01",
        "user_id": "u", "scope": "sc", "valid_from": None, "valid_to": None,
        "content_hash": "h",
    }
    d.update(kw)
    return tuple(d[c.strip()] for c in _ITEM_COLS.split(","))


def test_push_reaches_the_warehouse(env):
    """A local row lands remotely — the converted push path really moves data."""
    import pg_sync

    local, pg, ns = env
    ph = ", ".join(["?"] * 21)
    local.execute(f"INSERT INTO memory_items VALUES ({ph})", _row(id="pushed"))
    local.commit()

    cur = pg.cursor()
    cur.execute(f"SET search_path TO {ns}")
    pg_sync.sync_memory_items(local.cursor(), cur, local, "main")

    cur.execute("SELECT id, title FROM memory_items WHERE id = 'pushed'")
    assert cur.fetchone() == ("pushed", "t")


def test_pull_reaches_the_local_store(env):
    """A remote row lands locally — via bulk_upsert, the converted merge."""
    import pg_sync

    local, pg, ns = env
    cur = pg.cursor()
    cur.execute(f"SET search_path TO {ns}")
    ph = ", ".join(["%s"] * 21)
    cur.execute(f"INSERT INTO memory_items VALUES ({ph})", _row(id="pulled"))

    pg_sync.sync_memory_items(local.cursor(), cur, local, "main")

    got = local.execute(
        "SELECT id, title FROM memory_items WHERE id = 'pulled'"
    ).fetchone()
    assert got is not None and got["id"] == "pulled"


def test_pull_guard_rejects_an_older_remote_row(env):
    """Last-write-wins survives the bulk_upsert conversion.

    THE failure this test exists for: a `bulk_upsert` that dropped `guard_sql`
    would overwrite the newer local row with the older remote one, pass every
    shape assertion, and lose the edit silently.

    ⚠ The obvious way to write this DOES NOT TEST THE GUARD. sync_memory_items
    PUSHES before it PULLS, so a local row seeded before the call is sent to the
    warehouse first and overwrites the remote row — the pull then finds nothing
    older to reject, and the assertion passes whether or not the guard exists.
    Confirmed by counter-test: with `guard_sql` removed, that version still
    passed.

    So the pull is driven directly, with the local row written AFTER the push
    would have run. That isolates the merge, which is what is under test.
    """
    import pg_sync

    local, pg, ns = env
    cur = pg.cursor()
    cur.execute(f"SET search_path TO {ns}")
    pgph = ", ".join(["%s"] * 21)
    cur.execute(
        f"INSERT INTO memory_items VALUES ({pgph})",
        _row(id="x", title="REMOTE-OLD", updated_at="2025-01-01"),
    )

    # Local row is NEWER and already present when the merge happens.
    ph = ", ".join(["?"] * 21)
    local.execute(
        f"INSERT INTO memory_items VALUES ({ph})",
        _row(id="x", title="LOCAL-NEW", updated_at="2026-06-06"),
    )
    local.commit()

    # Drive the PULL half only: mark the push watermark as current so the push
    # finds nothing to send, leaving the merge as the only thing exercised.
    pg_sync._set_watermark(local.cursor(), "pg_push", "2099-01-01", "main")
    local.commit()
    pg_sync.sync_memory_items(local.cursor(), cur, local, "main")

    kept = local.execute("SELECT title FROM memory_items WHERE id='x'").fetchone()
    assert kept["title"] == "LOCAL-NEW", "an older remote row must not win"


def test_pull_guard_accepts_a_newer_remote_row(env):
    """The other half — the guard must not block legitimate updates.

    Asserted alongside the rejection case: a guard that rejected EVERYTHING would
    pass the test above while breaking sync entirely.
    """
    import pg_sync

    local, pg, ns = env
    ph = ", ".join(["?"] * 21)
    local.execute(
        f"INSERT INTO memory_items VALUES ({ph})",
        _row(id="y", title="LOCAL-OLD", updated_at="2025-01-01"),
    )
    local.commit()

    cur = pg.cursor()
    cur.execute(f"SET search_path TO {ns}")
    pgph = ", ".join(["%s"] * 21)
    cur.execute(
        f"INSERT INTO memory_items VALUES ({pgph})",
        _row(id="y", title="REMOTE-NEW", updated_at="2026-06-06"),
    )

    pg_sync.sync_memory_items(local.cursor(), cur, local, "main")

    got = local.execute("SELECT title FROM memory_items WHERE id='y'").fetchone()
    assert got["title"] == "REMOTE-NEW"


def test_manual_local_row_is_protected_from_an_automated_remote_row(env):
    """The manual/system clause rides in the same guard fragment.

    A curated local row must not be overwritten by an automated remote one, even
    when the remote is newer — the one place LWW is deliberately not the whole
    rule.
    """
    import pg_sync

    local, pg, ns = env
    ph = ", ".join(["?"] * 21)
    local.execute(
        f"INSERT INTO memory_items VALUES ({ph})",
        _row(id="z", title="CURATED", change_agent="manual", updated_at="2025-01-01"),
    )
    local.commit()

    cur = pg.cursor()
    cur.execute(f"SET search_path TO {ns}")
    pgph = ", ".join(["%s"] * 21)
    cur.execute(
        f"INSERT INTO memory_items VALUES ({pgph})",
        _row(id="z", title="AUTOMATED", change_agent="auto", updated_at="2026-06-06"),
    )

    pg_sync.sync_memory_items(local.cursor(), cur, local, "main")

    got = local.execute("SELECT title FROM memory_items WHERE id='z'").fetchone()
    assert got["title"] == "CURATED", "an automated remote row must not clobber a manual local one"
