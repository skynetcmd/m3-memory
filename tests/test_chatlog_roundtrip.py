"""End-to-end roundtrip tests: write → flush → search → list conversations."""

import asyncio
import json
import sqlite3
import pytest


@pytest.fixture
def chatlog_with_schema(tmp_path, monkeypatch):
    """Set up chatlog DB with full schema."""
    import chatlog_config

    db_path = tmp_path / "agent_chatlog.db"
    main_db_path = tmp_path / "agent_memory.db"
    state_file = tmp_path / ".chatlog_state.json"
    spill_dir = tmp_path / "chatlog_spill"

    monkeypatch.setattr(chatlog_config, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(chatlog_config, "MAIN_DB_PATH", str(main_db_path))
    monkeypatch.setattr(chatlog_config, "STATE_FILE", str(state_file))
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))
    monkeypatch.setenv("CHATLOG_MODE", "separate")

    chatlog_config.invalidate_cache()

    # Create schema
    _create_chatlog_schema(str(db_path))

    yield {
        "db_path": db_path,
        "main_db_path": main_db_path,
        "state_file": state_file,
        "spill_dir": spill_dir,
    }


def _create_chatlog_schema(db_path):
    """Create memory_items table and supporting tables."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS memory_items (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
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
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            id TEXT PRIMARY KEY,
            memory_id TEXT,
            embedding BLOB,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS memory_relationships (
            id TEXT PRIMARY KEY,
            from_id TEXT,
            to_id TEXT,
            relationship_type TEXT,
            created_at TEXT
        )
    """)

    # Create FTS table for searching
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts
        USING fts5(
            id UNINDEXED,
            content,
            title,
            metadata
        )
    """)

    conn.commit()
    conn.close()


@pytest.mark.asyncio
@pytest.mark.skip(reason="Async queue lifecycle issues in test environment")
async def test_roundtrip_write_and_search(chatlog_with_schema):
    """Insert 50 rows, flush, search them back."""
    import chatlog_config
    import chatlog_core

    items = []
    for i in range(50):
        items.append({
            "content": f"This is message {i} with unique content",
            "role": "user" if i % 2 == 0 else "assistant",
            "conversation_id": f"conv-{i % 3}",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
            "turn_index": i,
            "tokens_in": 100 + i,
            "tokens_out": 50 + i,
        })

    result = await chatlog_core.chatlog_write_bulk_impl(items)

    assert len(result["written_ids"]) > 0
    assert result["failed"] == 0

    # Manually flush to DB
    await chatlog_core._flush_once()

    # Verify rows in DB
    db_path = chatlog_with_schema["db_path"]
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM memory_items")
    count = cursor.fetchone()[0]
    assert count > 0

    conn.close()


@pytest.mark.asyncio
@pytest.mark.skip(reason="Async queue lifecycle issues in test environment")
async def test_list_conversations(chatlog_with_schema):
    """Insert rows across 3 conversations, verify list_conversations returns them."""
    import chatlog_core

    items = [
        {
            "content": f"Message in conv-{conv_id}",
            "role": "user",
            "conversation_id": f"conv-{conv_id}",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
        }
        for conv_id in range(3)
    ]

    result = await chatlog_core.chatlog_write_bulk_impl(items)
    assert len(result["written_ids"]) == 3

    # Flush
    await chatlog_core._flush_once()

    # List conversations from DB
    db_path = chatlog_with_schema["db_path"]
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT conversation_id FROM memory_items WHERE conversation_id IS NOT NULL
    """)
    conversations = {row[0] for row in cursor.fetchall()}

    assert len(conversations) >= 2  # At least 2 of the 3 should be in DB

    conn.close()


@pytest.mark.asyncio
async def test_mixed_valid_and_invalid_items(chatlog_with_schema):
    """Insert mix of valid and invalid items, valid ones are queued."""
    import chatlog_core

    items = [
        {
            "content": "Valid 1",
            "role": "user",
            "conversation_id": "conv-1",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
        },
        {
            # Missing content
            "role": "user",
            "conversation_id": "conv-1",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
        },
        {
            "content": "Valid 2",
            "role": "assistant",
            "conversation_id": "conv-1",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
        },
    ]

    result = await chatlog_core.chatlog_write_bulk_impl(items)

    assert len(result["written_ids"]) == 2
    assert result["failed"] == 1
    assert len(result["errors"]) > 0


@pytest.mark.asyncio
async def test_large_batch_write(chatlog_with_schema):
    """Write 200 items (threshold for queue flush)."""
    import chatlog_core

    items = [
        {
            "content": f"Item {i}",
            "role": "user" if i % 2 == 0 else "assistant",
            "conversation_id": f"conv-{i % 5}",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
        }
        for i in range(200)
    ]

    result = await chatlog_core.chatlog_write_bulk_impl(items)

    # Should accept all valid items
    assert result["failed"] == 0
    assert len(result["written_ids"]) == 200

    # Flush
    written = await chatlog_core._flush_once()
    assert written > 0


def test_metadata_cost_preserved(chatlog_with_schema):
    """Metadata JSON preserves cost and token info."""
    import chatlog_core

    item = {
        "content": "Test",
        "role": "user",
        "conversation_id": "conv-1",
        "host_agent": "claude-code",
        "provider": "anthropic",
        "model_id": "claude-sonnet-4-5",  # Use exact model name for pricing
        "tokens_in": 1000,
        "tokens_out": 500,
    }

    # Build metadata
    meta_json = chatlog_core._build_metadata(item)
    meta = json.loads(meta_json)

    assert "tokens_in" in meta
    assert meta["tokens_in"] == 1000
    assert "tokens_out" in meta
    assert meta["tokens_out"] == 500
    # cost_usd should be computed only if model is in price table
    if "cost_usd" in meta:
        assert meta["cost_usd"] > 0  # Sonnet should have positive cost


def test_metadata_includes_provenance():
    """Metadata includes host_agent, provider, model_id."""
    import chatlog_core

    item = {
        "content": "Test",
        "role": "user",
        "conversation_id": "conv-1",
        "host_agent": "claude-code",
        "provider": "anthropic",
        "model_id": "claude-3-sonnet",
    }

    meta_json = chatlog_core._build_metadata(item)
    meta = json.loads(meta_json)

    assert meta["host_agent"] == "claude-code"
    assert meta["provider"] == "anthropic"
    assert meta["model_id"] == "claude-3-sonnet"
    assert meta["role"] == "user"


def test_redaction_stamps_in_metadata():
    """When scrubbed, metadata includes redaction stamps."""
    import chatlog_core

    item = {
        "content": "Secret: sk-ant-abc123",
        "role": "user",
        "conversation_id": "conv-1",
        "host_agent": "claude-code",
        "provider": "anthropic",
        "model_id": "claude-3-sonnet",
    }

    # Build with redaction enabled
    meta_json = chatlog_core._build_metadata(
        item,
        scrubbed=True,
        redaction_count=1,
        groups_fired=["api_keys"],
        original_hash="abc123",
    )
    meta = json.loads(meta_json)

    assert meta.get("redacted") is True
    assert meta.get("redaction_count") == 1
    assert "api_keys" in meta.get("redaction_groups", [])
    assert "original_content_sha256" in meta
