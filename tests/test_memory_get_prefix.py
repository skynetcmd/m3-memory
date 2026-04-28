"""Tests for memory_get_impl prefix-lookup support (migration 027).

memory_get_impl now accepts either a 36-char UUID or an 8-char prefix.
We exercise each branch against an isolated tmp SQLite DB so the live
agent_memory.db is never touched. We hand-build the minimal memory_items
schema rather than going through the migration runner because we only
need the columns memory_get_impl returns.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


_MIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY,
    type TEXT,
    title TEXT,
    content TEXT,
    metadata_json TEXT,
    agent_id TEXT,
    model_id TEXT,
    change_agent TEXT,
    importance REAL,
    source TEXT,
    origin_device TEXT,
    user_id TEXT,
    scope TEXT,
    expires_at TEXT,
    created_at TEXT,
    valid_from TEXT,
    valid_to TEXT,
    conversation_id TEXT,
    refresh_on TEXT,
    refresh_reason TEXT,
    content_hash TEXT,
    variant TEXT
);

-- Mirror table memory_get_impl falls back to on miss.
CREATE TABLE IF NOT EXISTS chroma_mirror (
    id TEXT PRIMARY KEY,
    title TEXT,
    content TEXT
);

-- Same expression index migration 027 installs, so the prefix path is
-- exercised the way it will be in production.
CREATE INDEX IF NOT EXISTS idx_mi_id_prefix8
    ON memory_items(SUBSTR(id, 1, 8));
"""


def _seed(db_path, rows):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_MIN_SCHEMA)
        for r in rows:
            conn.execute(
                "INSERT INTO memory_items (id, type, title, content) VALUES (?, ?, ?, ?)",
                (r["id"], r.get("type", "note"), r.get("title", ""), r.get("content", "")),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """Point memory_core at a tmp DB and short-circuit lazy schema init."""
    db_path = tmp_path / "agent_memory.db"
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    import memory_core

    # Skip the subprocess-based migration runner during _lazy_init — we
    # built exactly the schema this test needs above.
    memory_core._initialized_dbs.add(str(db_path))
    yield db_path
    memory_core._initialized_dbs.discard(str(db_path))


def test_full_uuid_happy_path(isolated_db):
    from memory_core import memory_get_impl

    full_id = "0906f86c-1111-2222-3333-444455556666"
    _seed(isolated_db, [{"id": full_id, "title": "alpha", "content": "hello"}])

    out = memory_get_impl(full_id)
    payload = json.loads(out)
    assert payload["id"] == full_id
    assert payload["title"] == "alpha"


def test_8char_prefix_happy_path(isolated_db):
    from memory_core import memory_get_impl

    full_id = "0906f86c-1111-2222-3333-444455556666"
    _seed(isolated_db, [{"id": full_id, "title": "alpha", "content": "hello"}])

    out = memory_get_impl("0906f86c")
    payload = json.loads(out)
    assert payload["id"] == full_id
    assert payload["title"] == "alpha"


def test_8char_prefix_miss_returns_not_found(isolated_db):
    from memory_core import memory_get_impl

    _seed(isolated_db, [
        {"id": "aaaaaaaa-1111-2222-3333-444455556666", "title": "x"},
    ])

    out = memory_get_impl("deadbeef")
    assert out == "Error: not found"


def test_8char_prefix_ambiguous_lists_all_matches(isolated_db):
    from memory_core import memory_get_impl

    a = "0906f86c-1111-2222-3333-444455556666"
    b = "0906f86c-aaaa-bbbb-cccc-dddddddddddd"
    _seed(isolated_db, [
        {"id": a, "title": "first"},
        {"id": b, "title": "second"},
    ])

    out = memory_get_impl("0906f86c")
    assert out.startswith("Error: ambiguous prefix '0906f86c': matches ")
    assert a in out
    assert b in out


def test_bad_length_returns_length_error(isolated_db):
    from memory_core import memory_get_impl

    out = memory_get_impl("123456789012")  # 12 chars — neither 8 nor 36
    assert out == "Error: id must be 36-char UUID or 8-char prefix"
