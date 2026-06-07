"""End-to-end roundtrip tests: write → flush → search → list conversations."""

import json
import sqlite3

import pytest

from conftest import create_memory_items_schema, isolate_chatlog_env


@pytest.fixture
def chatlog_with_schema(tmp_path, monkeypatch):
    """Set up chatlog DB with full schema (memory_items + embeddings + FTS)."""
    paths = isolate_chatlog_env(monkeypatch, tmp_path)
    create_memory_items_schema(paths["db_path"])
    _create_supporting_tables(str(paths["db_path"]))
    yield paths


def _create_supporting_tables(db_path):
    """Embeddings, relationships, FTS5 — only needed by roundtrip tests."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            id TEXT PRIMARY KEY,
            memory_id TEXT,
            embedding BLOB,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS memory_relationships (
            id TEXT PRIMARY KEY,
            from_id TEXT,
            to_id TEXT,
            relationship_type TEXT,
            created_at TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts
        USING fts5(id UNINDEXED, content, title, metadata);
    """)
    conn.commit()
    conn.close()


@pytest.mark.asyncio
@pytest.mark.skip(reason="Async queue lifecycle issues in test environment")
async def test_roundtrip_write_and_search(chatlog_with_schema):
    """Insert 50 rows, flush, search them back."""
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
            "variant": "test",
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
async def test_search_with_filters_does_not_crash(chatlog_with_schema):
    """chatlog_search_impl must accept its full filter set without raising.

    Regression for a day-one bug (commit 1242f96): the unified-path branch
    (chatlog DB == main DB) forwarded agent_id/since/until to
    memory_search_scored_impl, which only ever accepted agent_filter/as_of —
    so the unified path raised TypeError on every real call. No test exercised
    chatlog_search_impl, so it survived ~7 weeks. This test calls the impl with
    every filter populated and asserts a well-formed structured result.
    """
    import chatlog_core

    # The minimal fixture schema predates the soft-delete column; production
    # memory_items has it and _chatlog_search_separate filters on it. Add it
    # here (scoped to this test) so the query path runs against a faithful
    # schema without disturbing the shared fixture other tests rely on.
    db_path = chatlog_with_schema["db_path"]
    _conn = sqlite3.connect(str(db_path))
    cols = {row[1] for row in _conn.execute("PRAGMA table_info(memory_items)")}
    if "is_deleted" not in cols:
        _conn.execute("ALTER TABLE memory_items ADD COLUMN is_deleted INTEGER DEFAULT 0")
        _conn.commit()
    _conn.close()

    items = [
        {
            "content": f"alpha bravo charlie message {i}",
            "role": "user" if i % 2 == 0 else "assistant",
            "conversation_id": f"conv-{i % 2}",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
            "agent_id": "agent-x",
            "turn_index": i,
            "variant": "test",
        }
        for i in range(6)
    ]
    await chatlog_core.chatlog_write_bulk_impl(items)
    await chatlog_core._flush_once()

    # The exact call that used to TypeError on the unified path: a real query
    # plus every filter the tool exposes (agent_id/since/until + facets).
    out = await chatlog_core.chatlog_search_impl(
        query="bravo",
        k=5,
        conversation_id="conv-0",
        host_agent="claude-code",
        provider="anthropic",
        model_id="claude-3-sonnet",
        agent_id="agent-x",
        since="2000-01-01",
        until="2099-12-31",
    )
    payload = json.loads(out)

    # Structured return, never a bare string or None (DESIGN §3).
    assert isinstance(payload, dict)
    assert isinstance(payload["results"], list)
    assert isinstance(payload["count"], int)
    assert payload["count"] == len(payload["results"])
    assert "db_path" in payload and "unified" in payload
    # conversation_id filter must be honored end-to-end.
    for r in payload["results"]:
        assert r["conversation_id"] == "conv-0"

    # Empty query with filters only (the filter-only branch) returns recent
    # rows for that conversation — a "browse", which should find the 3 conv-1
    # messages written above.
    out2 = await chatlog_core.chatlog_search_impl(
        query="", k=5, conversation_id="conv-1",
    )
    payload2 = json.loads(out2)
    assert isinstance(payload2["results"], list)
    assert payload2["count"] == len(payload2["results"])
    assert payload2["count"] > 0  # blank query = browse, not "no results"

    # Operator-only query ("---") sanitizes to no tokens. The user DID specify a
    # term, so this must return ZERO results — not silently fall through to the
    # browse branch and return the latest rows (a real prior footgun).
    out3 = await chatlog_core.chatlog_search_impl(query="---", k=5)
    payload3 = json.loads(out3)
    assert payload3["count"] == 0, "operator-only query must not browse-fall-through"


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
            "variant": "test",
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
            "variant": "test",
        },
        {
            # Missing content
            "role": "user",
            "conversation_id": "conv-1",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
            "variant": "test",
        },
        {
            "content": "Valid 2",
            "role": "assistant",
            "conversation_id": "conv-1",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
            "variant": "test",
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
            "variant": "test",
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
