"""Tests for memory_write_from_file_impl (Phase K).

Locks the contract for the file-backed memory write path:
- happy-path file → row + tempfile deletion
- missing / empty path errors
- delete_after_read=False preserves source
- leak gate (window:* + variant=None) preserves source
- >200_000 byte files rejected before read

Mirrors the tmp-DB harness from test_memory_get_prefix.py — _initialized_dbs
patch short-circuits the migration runner. embed=False on every call so we
don't need an LM Studio sidecar or memory_embeddings/chroma_sync_queue
write path. memory_history is still touched on success, so we include it
in the schema.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile

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

CREATE TABLE IF NOT EXISTS chroma_sync_queue (
    memory_id TEXT,
    operation TEXT
);

CREATE TABLE IF NOT EXISTS chroma_mirror (
    id TEXT PRIMARY KEY,
    title TEXT,
    content TEXT
);
"""


def _init_schema(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_MIN_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _row_count(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
    finally:
        conn.close()


def _fetch_one(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT type, title, content FROM memory_items LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "agent_memory.db"
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    _init_schema(db_path)

    import memory_core

    memory_core._initialized_dbs.add(str(db_path))
    yield db_path
    memory_core._initialized_dbs.discard(str(db_path))


def _make_tempfile(body, suffix=".md"):
    tf = tempfile.NamedTemporaryFile(
        mode="w", delete=False, suffix=suffix, encoding="utf-8"
    )
    try:
        tf.write(body)
    finally:
        tf.close()
    return tf.name


def test_happy_path_writes_row_and_deletes_tempfile(isolated_db):
    from memory_core import memory_write_from_file_impl

    body = "# Hello\n\nA small markdown body."
    path = _make_tempfile(body)

    out = asyncio.run(
        memory_write_from_file_impl(
            path=path, type="note", title="test title", embed=False
        )
    )

    assert isinstance(out, str)
    assert out.startswith("Created:"), f"unexpected result: {out!r}"
    assert _row_count(isolated_db) == 1
    row = _fetch_one(isolated_db)
    assert row[0] == "note"
    assert row[1] == "test title"
    assert row[2] == body
    assert not os.path.exists(path), "tempfile should be deleted on success"


def test_missing_path_returns_error_and_writes_no_row(isolated_db):
    from memory_core import memory_write_from_file_impl

    bogus = os.path.join(
        os.path.dirname(str(isolated_db)), "nonexistent_does_not_exist.md"
    )
    out = asyncio.run(
        memory_write_from_file_impl(path=bogus, type="note", embed=False)
    )

    assert isinstance(out, str)
    assert out.startswith("Error: file not found:"), f"unexpected: {out!r}"
    assert _row_count(isolated_db) == 0


def test_empty_path_returns_required_error(isolated_db):
    from memory_core import memory_write_from_file_impl

    out = asyncio.run(
        memory_write_from_file_impl(path="", type="note", embed=False)
    )

    assert out == "Error: path is required"
    assert _row_count(isolated_db) == 0


def test_delete_after_read_false_preserves_source(isolated_db):
    from memory_core import memory_write_from_file_impl

    body = "preserve me on disk"
    path = _make_tempfile(body)
    try:
        out = asyncio.run(
            memory_write_from_file_impl(
                path=path,
                type="note",
                title="keep",
                embed=False,
                delete_after_read=False,
            )
        )

        assert out.startswith("Created:"), f"unexpected: {out!r}"
        assert _row_count(isolated_db) == 1
        assert os.path.exists(path), "tempfile must survive when delete_after_read=False"
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_leak_gate_fires_and_preserves_source(isolated_db):
    from memory_core import memory_write_from_file_impl

    # Leak gate triggers for type='summary' with title='window:*' and no variant.
    # Brief said type='note' but the impl only gates summary rows — adjusting
    # to match the actual gate.
    body = "window summary body"
    path = _make_tempfile(body)
    try:
        out = asyncio.run(
            memory_write_from_file_impl(
                path=path,
                type="summary",
                title="window:abc::1:2",
                embed=False,
                variant=None,
            )
        )

        assert isinstance(out, str)
        assert out.startswith("Error:"), f"expected error, got {out!r}"
        assert "window:" in out or "leak" in out.lower() or "task #189" in out
        assert _row_count(isolated_db) == 0
        assert os.path.exists(path), "tempfile must be preserved on gate rejection"
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_file_too_large_rejected_before_read(isolated_db):
    from memory_core import memory_write_from_file_impl

    # 200_001 bytes — just over the size cap.
    body = "x" * 200_001
    path = _make_tempfile(body, suffix=".txt")
    try:
        out = asyncio.run(
            memory_write_from_file_impl(path=path, type="note", embed=False)
        )

        assert isinstance(out, str)
        assert out.startswith("Error: file too large"), f"unexpected: {out!r}"
        assert _row_count(isolated_db) == 0
        assert os.path.exists(path), "source file must be untouched on size reject"
    finally:
        if os.path.exists(path):
            os.unlink(path)
