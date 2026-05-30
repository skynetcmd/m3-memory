"""Tests for memory_supersede_impl.

Locks the contract for the explicit-supersede path:
- happy path: new row created, old row closed (is_deleted + valid_to),
  a `supersedes` edge written new -> old, a history event recorded
- old memory still retrievable by id after supersede (non-destructive)
- field inheritance: omitted type/title/importance/scope come from the old row
- explicit fields override inheritance
- error paths leave the old row untouched: unknown old_id, already-superseded
  old_id, oversized content

Mirrors the tmp-DB harness from test_memory_write_from_file.py — the
`_initialized_dbs` patch short-circuits the migration runner, and embed=False
on every call so no LM Studio sidecar / embeddings write path is needed.
memory_relationships is added to the schema since supersede writes an edge.
"""

import asyncio
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


_SCHEMA = """
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
    is_deleted INTEGER DEFAULT 0,
    expires_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    valid_from TEXT DEFAULT '',
    valid_to TEXT DEFAULT '',
    conversation_id TEXT,
    refresh_on TEXT,
    refresh_reason TEXT,
    content_hash TEXT,
    variant TEXT
);

CREATE TABLE IF NOT EXISTS memory_history (
    id TEXT PRIMARY KEY,
    memory_id TEXT,
    event TEXT,
    prev_value TEXT,
    new_value TEXT,
    field TEXT,
    actor_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    id TEXT PRIMARY KEY,
    memory_id TEXT,
    embedding BLOB,
    embed_model TEXT,
    dim INTEGER,
    created_at TEXT,
    content_hash TEXT
);

CREATE TABLE IF NOT EXISTS memory_relationships (
    id TEXT PRIMARY KEY,
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS chroma_sync_queue (
    memory_id TEXT,
    operation TEXT
);
"""


def _init_schema(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "agent_memory.db"
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    _init_schema(db_path)

    import memory_core

    memory_core._initialized_dbs.add(str(db_path))
    yield db_path
    memory_core._initialized_dbs.discard(str(db_path))


def _seed_memory(db_path, *, type="note", title="original title",
                 content="original content", importance=0.5, scope="agent"):
    """Insert one active memory directly and return its id."""
    import uuid
    mem_id = str(uuid.uuid4())
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO memory_items "
            "(id, type, title, content, importance, scope, is_deleted, "
            " valid_from, valid_to) "
            "VALUES (?,?,?,?,?,?,0,'','')",
            (mem_id, type, title, content, importance, scope),
        )
        conn.commit()
    finally:
        conn.close()
    return mem_id


def _row(db_path, mem_id):
    conn = _connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM memory_items WHERE id = ?", (mem_id,)
        ).fetchone()
    finally:
        conn.close()


def _new_id_from_result(result):
    """Extract the new memory id from a 'Superseded X -> Created: Y' string."""
    assert result.startswith("Superseded "), result
    return result.split("Created:", 1)[1].strip().split()[0]


def test_happy_path_creates_new_closes_old_and_links(isolated_db):
    from memory_core import memory_supersede_impl

    old_id = _seed_memory(isolated_db)

    result = asyncio.run(
        memory_supersede_impl(
            old_id=old_id,
            content="updated content",
            embed=False,
        )
    )
    new_id = _new_id_from_result(result)
    assert new_id != old_id

    # New row exists, active, carries the new content.
    new = _row(isolated_db, new_id)
    assert new is not None
    assert new["content"] == "updated content"
    assert not new["is_deleted"]

    # Old row retained (non-destructive) but closed.
    old = _row(isolated_db, old_id)
    assert old is not None, "supersede must NOT delete the old row"
    assert old["is_deleted"] == 1
    assert old["valid_to"], "valid_to must be set (interval closed)"

    # A `supersedes` edge new -> old was written.
    conn = _connect(isolated_db)
    try:
        edge = conn.execute(
            "SELECT from_id, to_id FROM memory_relationships "
            "WHERE relationship_type = 'supersedes'"
        ).fetchone()
    finally:
        conn.close()
    assert edge is not None
    assert edge["from_id"] == new_id
    assert edge["to_id"] == old_id

    # A history event was recorded against the old memory.
    conn = _connect(isolated_db)
    try:
        hist = conn.execute(
            "SELECT event, new_value FROM memory_history "
            "WHERE memory_id = ? AND event = 'supersede'",
            (old_id,),
        ).fetchone()
    finally:
        conn.close()
    assert hist is not None
    assert hist["new_value"] == new_id


def test_omitted_fields_inherit_from_old_memory(isolated_db):
    from memory_core import memory_supersede_impl

    old_id = _seed_memory(
        isolated_db, type="reference", title="inherited title",
        importance=0.9, scope="user",
    )

    result = asyncio.run(
        memory_supersede_impl(old_id=old_id, content="new body", embed=False)
    )
    new = _row(isolated_db, _new_id_from_result(result))
    # type / title / importance / scope all carried over from the old row.
    assert new["type"] == "reference"
    assert new["title"] == "inherited title"
    assert new["importance"] == 0.9
    assert new["scope"] == "user"


def test_explicit_fields_override_inheritance(isolated_db):
    from memory_core import memory_supersede_impl

    old_id = _seed_memory(
        isolated_db, type="note", title="old", importance=0.3,
    )

    result = asyncio.run(
        memory_supersede_impl(
            old_id=old_id,
            content="new body",
            type="reference",
            title="explicit new title",
            importance=0.8,
            embed=False,
        )
    )
    new = _row(isolated_db, _new_id_from_result(result))
    assert new["type"] == "reference"
    assert new["title"] == "explicit new title"
    assert new["importance"] == 0.8


def test_unknown_old_id_errors_and_writes_nothing(isolated_db):
    from memory_core import memory_supersede_impl

    result = asyncio.run(
        memory_supersede_impl(
            old_id="00000000-0000-0000-0000-000000000000",
            content="whatever",
            embed=False,
        )
    )
    assert result.startswith("Error:")
    assert "not found" in result
    conn = _connect(isolated_db)
    try:
        count = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
    finally:
        conn.close()
    assert count == 0, "a failed supersede must not create any row"


def test_already_superseded_old_id_errors(isolated_db):
    from memory_core import memory_supersede_impl

    old_id = _seed_memory(isolated_db)
    # First supersede succeeds and closes old_id.
    first = asyncio.run(
        memory_supersede_impl(old_id=old_id, content="v2", embed=False)
    )
    assert first.startswith("Superseded ")

    # Second supersede of the now-deleted old_id is rejected — idempotency
    # guard: re-running is a clear error, not a silent double-supersede.
    second = asyncio.run(
        memory_supersede_impl(old_id=old_id, content="v3", embed=False)
    )
    assert second.startswith("Error:")
    assert "already deleted" in second or "already" in second


def test_oversized_content_errors_and_leaves_old_untouched(isolated_db):
    from memory_core import memory_supersede_impl

    old_id = _seed_memory(isolated_db, content="original content")
    huge = "x" * 50_001  # over the 50000-char memory_write cap

    result = asyncio.run(
        memory_supersede_impl(old_id=old_id, content=huge, embed=False)
    )
    assert result.startswith("Error:")

    # Old memory must be completely untouched — still active, original content.
    old = _row(isolated_db, old_id)
    assert old["is_deleted"] == 0
    assert old["content"] == "original content"
    assert not old["valid_to"]


def test_concurrent_close_loses_race_and_rolls_back_orphan(isolated_db, monkeypatch):
    """If old_id is closed AFTER the step-1 read but BEFORE the step-4 UPDATE,
    the conditional `WHERE is_deleted = 0` finds no row, the supersede is
    rejected, and the replacement created in step 3 is rolled back.

    The race is reproduced deterministically: a memory_write_impl wrapper that
    closes old_id as a side effect, which is exactly the interleaving a
    concurrent supersede/delete would produce."""
    import memory.write as write_mod
    from memory_core import memory_supersede_impl

    old_id = _seed_memory(isolated_db, content="original content")
    real_write = write_mod.memory_write_impl

    async def racing_write(**kwargs):
        # Simulate a concurrent supersede/delete closing old_id mid-call.
        conn = _connect(isolated_db)
        try:
            conn.execute(
                "UPDATE memory_items SET is_deleted = 1 WHERE id = ?", (old_id,)
            )
            conn.commit()
        finally:
            conn.close()
        return await real_write(**kwargs)

    import sys
    for name, module in list(sys.modules.items()):
        if name.endswith("memory.write") and hasattr(module, "memory_write_impl"):
            monkeypatch.setattr(module, "memory_write_impl", racing_write)

    result = asyncio.run(
        memory_supersede_impl(old_id=old_id, content="v2", embed=False)
    )
    assert result.startswith("Error:")
    assert "concurrent" in result

    # No `supersedes` edge written — the losing caller must not link.
    conn = _connect(isolated_db)
    try:
        edges = conn.execute(
            "SELECT COUNT(*) FROM memory_relationships "
            "WHERE relationship_type = 'supersedes'"
        ).fetchone()[0]
        # Exactly one non-deleted memory remains: neither the orphaned
        # replacement nor old_id is active.
        active = conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE is_deleted = 0"
        ).fetchone()[0]
    finally:
        conn.close()
    assert edges == 0, "a lost race must not write a supersedes edge"
    assert active == 0, "orphaned replacement must be rolled back (soft-deleted)"
