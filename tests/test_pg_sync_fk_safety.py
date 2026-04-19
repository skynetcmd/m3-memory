"""Regression test for pg_sync FK-safety pre-filter (commit 30dc3f3).

The bug: memory_embeddings.memory_id has a FK to memory_items.id. When an
embedding is pushed to PG but its parent memory_item hasn't synced yet, the
FK fires and the whole execute_values batch rolls back. The fix pre-filters
embeddings against memory_items IDs present in PG before the push.
"""

from __future__ import annotations

import logging
import pathlib
import sqlite3
import sys
import types

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bin"))

# Stub psycopg2 so pg_sync import doesn't require it installed at test time.
if "psycopg2" not in sys.modules:
    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.Binary = bytes  # Binary(x) is used as a no-op wrapper
    extras = types.ModuleType("psycopg2.extras")
    def _execute_values(cur, sql, values):  # minimal shim
        cur.execute(sql, values)
    extras.execute_values = _execute_values
    psycopg2.extras = extras
    sys.modules["psycopg2"] = psycopg2
    sys.modules["psycopg2.extras"] = extras


class FakePgCursor:
    """Minimal PG cursor. Routes the schema-exists probe (returns True),
    the FK-filter SELECT (returns subset of queried IDs against
    present_ids), the full-pull SELECT (empty), and captures INSERT
    batches."""

    def __init__(self, present_ids: set[str]):
        self.present_ids = present_ids
        self._next_one: tuple | None = None
        self._next_all: list[tuple] = []
        self.inserted_batches: list[list[tuple]] = []

    def execute(self, sql: str, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT EXISTS"):
            self._next_one = (True,)  # pretend table exists
        elif s.startswith("SELECT id FROM memory_items WHERE id = ANY"):
            queried = params[0] if params else []
            self._next_all = [(i,) for i in queried if i in self.present_ids]
        elif s.startswith("SELECT me.id, me.memory_id") or \
             s.startswith("SELECT id, memory_id, embedding"):
            self._next_all = []  # no remote rows to pull
        elif s.startswith("INSERT INTO memory_embeddings"):
            self.inserted_batches.append(list(params) if params else [])
        else:
            self._next_one = None
            self._next_all = []

    def fetchone(self):
        return self._next_one

    def fetchall(self):
        return self._next_all


def _make_sqlite() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY,
            updated_at TEXT,
            created_at TEXT
        );
        CREATE TABLE memory_embeddings (
            id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            embedding BLOB NOT NULL,
            embed_model TEXT,
            dim INTEGER
        );
        CREATE TABLE sync_watermarks (
            direction TEXT PRIMARY KEY,
            last_synced_at TEXT NOT NULL
        );
    """)
    return conn


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []
    def emit(self, record):
        self.messages.append(record.getMessage())


def _attach_capture():
    import pg_sync
    h = _CaptureHandler()
    h.setLevel(logging.DEBUG)
    pg_sync.logger.addHandler(h)
    pg_sync.logger.setLevel(logging.DEBUG)
    return h, pg_sync


def test_skips_embedding_when_parent_not_in_pg():
    """Two embeddings: one parent present in PG, one missing.
    Only the present-parent embedding is pushed; the orphan is deferred."""
    h, pg_sync = _attach_capture()
    try:
        conn = _make_sqlite()
        cur = conn.cursor()
        cur.execute("INSERT INTO memory_items (id, created_at) VALUES (?, ?)",
                    ("mem-parent-present", "2026-04-19T00:00:00Z"))
        cur.execute("INSERT INTO memory_items (id, created_at) VALUES (?, ?)",
                    ("mem-parent-missing", "2026-04-19T00:00:00Z"))
        cur.execute("INSERT INTO memory_embeddings VALUES (?,?,?,?,?)",
                    ("emb-1", "mem-parent-present", b"\x01" * 8, "test-model", 8))
        cur.execute("INSERT INTO memory_embeddings VALUES (?,?,?,?,?)",
                    ("emb-2", "mem-parent-missing", b"\x02" * 8, "test-model", 8))
        conn.commit()

        pg_cur = FakePgCursor(present_ids={"mem-parent-present"})
        pg_sync.sync_memory_embeddings(cur, pg_cur, conn)

        deferred = [m for m in h.messages if "deferred for missing parent" in m]
        assert deferred, f"summary log should mention deferred count; got {h.messages}"
        assert "1 deferred for missing parent" in deferred[-1], deferred[-1]

        pushed_ids: set[str] = set()
        for batch in pg_cur.inserted_batches:
            for row in batch:
                pushed_ids.add(row[1])
        assert pushed_ids == {"mem-parent-present"}, (
            f"orphan embedding must not be pushed; got {pushed_ids}")
    finally:
        pg_sync.logger.removeHandler(h)


def test_all_parents_present_pushes_everything():
    """Sanity: when every embedding has a present parent, skipped_fk=0 and
    all rows are pushed."""
    h, pg_sync = _attach_capture()
    try:
        conn = _make_sqlite()
        cur = conn.cursor()
        for mid in ("a", "b", "c"):
            cur.execute("INSERT INTO memory_items (id, created_at) VALUES (?, ?)",
                        (mid, "2026-04-19T00:00:00Z"))
            cur.execute("INSERT INTO memory_embeddings VALUES (?,?,?,?,?)",
                        (f"emb-{mid}", mid, b"\xab" * 4, "m", 4))
        conn.commit()

        pg_cur = FakePgCursor(present_ids={"a", "b", "c"})
        pg_sync.sync_memory_embeddings(cur, pg_cur, conn)

        summary = [m for m in h.messages if "Pushed" in m and "deferred" in m]
        assert summary, f"expected push summary log; got {h.messages}"
        assert "0 deferred for missing parent" in summary[-1]

        pushed_ids = {row[1] for batch in pg_cur.inserted_batches for row in batch}
        assert pushed_ids == {"a", "b", "c"}
    finally:
        pg_sync.logger.removeHandler(h)


def test_all_parents_missing_pushes_nothing():
    """All orphans → skipped_fk = N, nothing pushed, no exception."""
    import pg_sync

    conn = _make_sqlite()
    cur = conn.cursor()
    for mid in ("x", "y"):
        cur.execute("INSERT INTO memory_items (id, created_at) VALUES (?, ?)",
                    (mid, "2026-04-19T00:00:00Z"))
        cur.execute("INSERT INTO memory_embeddings VALUES (?,?,?,?,?)",
                    (f"emb-{mid}", mid, b"\xcd" * 4, "m", 4))
    conn.commit()

    pg_cur = FakePgCursor(present_ids=set())
    pg_sync.sync_memory_embeddings(cur, pg_cur, conn)

    assert pg_cur.inserted_batches == [], (
        "no rows should be inserted when every parent is missing")
