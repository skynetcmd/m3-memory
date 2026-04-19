"""Tests for bin/chatlog_core.py — write queue, validation, and flush."""

import asyncio
import json
import os
import pytest
import tempfile
import sqlite3


from conftest import isolate_chatlog_env, create_memory_items_schema


@pytest.fixture
def chatlog_env(tmp_path, monkeypatch):
    """Set up isolated chatlog environment with temp paths."""
    paths = isolate_chatlog_env(monkeypatch, tmp_path)
    create_memory_items_schema(paths["db_path"])
    yield paths


def test_validate_write_missing_content():
    """Missing content raises ValueError."""
    import chatlog_core

    item = {
        "role": "user",
        "conversation_id": "conv-1",
        "host_agent": "claude-code",
        "provider": "anthropic",
        "model_id": "claude-3-sonnet",
    }

    with pytest.raises(ValueError, match="content is required"):
        chatlog_core._validate_write(item)


def test_validate_write_missing_host_agent():
    """Missing host_agent raises ValueError."""
    import chatlog_core

    item = {
        "content": "Hello world",
        "role": "user",
        "conversation_id": "conv-1",
        "provider": "anthropic",
        "model_id": "claude-3-sonnet",
    }

    with pytest.raises(ValueError, match="host_agent"):
        chatlog_core._validate_write(item)


def test_validate_write_missing_provider():
    """Missing provider raises ValueError."""
    import chatlog_core

    item = {
        "content": "Hello world",
        "role": "user",
        "conversation_id": "conv-1",
        "host_agent": "claude-code",
        "model_id": "claude-3-sonnet",
    }

    with pytest.raises(ValueError, match="provider"):
        chatlog_core._validate_write(item)


def test_validate_write_missing_model_id():
    """Missing model_id raises ValueError."""
    import chatlog_core

    item = {
        "content": "Hello world",
        "role": "user",
        "conversation_id": "conv-1",
        "host_agent": "claude-code",
        "provider": "anthropic",
    }

    with pytest.raises(ValueError, match="model_id"):
        chatlog_core._validate_write(item)


def test_validate_write_missing_conversation_id():
    """Missing conversation_id raises ValueError."""
    import chatlog_core

    item = {
        "content": "Hello world",
        "role": "user",
        "host_agent": "claude-code",
        "provider": "anthropic",
        "model_id": "claude-3-sonnet",
    }

    with pytest.raises(ValueError, match="conversation_id"):
        chatlog_core._validate_write(item)


def test_validate_write_invalid_role():
    """Invalid role raises ValueError."""
    import chatlog_core

    item = {
        "content": "Hello world",
        "role": "invalid_role",
        "conversation_id": "conv-1",
        "host_agent": "claude-code",
        "provider": "anthropic",
        "model_id": "claude-3-sonnet",
    }

    with pytest.raises(ValueError, match="role"):
        chatlog_core._validate_write(item)


def test_validate_write_valid():
    """Valid item passes validation."""
    import chatlog_core

    item = {
        "content": "Hello world",
        "role": "user",
        "conversation_id": "conv-1",
        "host_agent": "claude-code",
        "provider": "anthropic",
        "model_id": "claude-3-sonnet",
    }

    # Should not raise
    chatlog_core._validate_write(item)


def test_validate_write_content_too_long():
    """Content exceeding MAX_CONTENT_LEN raises ValueError."""
    import chatlog_core

    item = {
        "content": "x" * (chatlog_core.MAX_CONTENT_LEN + 1),
        "role": "user",
        "conversation_id": "conv-1",
        "host_agent": "claude-code",
        "provider": "anthropic",
        "model_id": "claude-3-sonnet",
    }

    with pytest.raises(ValueError, match="exceeds"):
        chatlog_core._validate_write(item)


@pytest.mark.asyncio
async def test_chatlog_write_bulk_impl_invalid_items_counted(chatlog_env):
    """chatlog_write_bulk_impl counts invalid items in 'failed' not 'written_ids'."""
    import chatlog_core

    items = [
        {
            "content": "Valid message",
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
            # Invalid host_agent
            "content": "Another message",
            "role": "user",
            "conversation_id": "conv-1",
            "host_agent": "invalid_agent",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
            "variant": "test",
        },
    ]

    result = await chatlog_core.chatlog_write_bulk_impl(items)

    assert "written_ids" in result
    assert "failed" in result
    assert "errors" in result
    assert len(result["written_ids"]) == 1
    assert result["failed"] == 2
    assert len(result["errors"]) > 0


@pytest.mark.asyncio
async def test_chatlog_write_bulk_impl_returns_dict(chatlog_env):
    """chatlog_write_bulk_impl returns expected dict structure."""
    import chatlog_core

    items = [
        {
            "content": "Message 1",
            "role": "user",
            "conversation_id": "conv-1",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
            "variant": "test",
        },
    ]

    result = await chatlog_core.chatlog_write_bulk_impl(items)

    assert isinstance(result, dict)
    assert "written_ids" in result
    assert "failed" in result
    assert "errors" in result
    assert isinstance(result["written_ids"], list)
    assert isinstance(result["failed"], int)
    assert isinstance(result["errors"], list)


def test_spill_batch_creates_file(tmp_path, monkeypatch):
    """_spill_batch creates a dated JSONL file in SPILL_DIR."""
    import chatlog_config
    import chatlog_core

    spill_dir = tmp_path / "spill"
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))

    batch = [
        {
            "_id": "id-1",
            "_title": "Test",
            "_content": "Content 1",
            "_metadata_json": "{}",
            "_created_at": "2024-01-01T00:00:00Z",
            "conversation_id": "conv-1",
            "model_id": "test",
        },
    ]

    chatlog_core._spill_batch(batch)

    # Check that a file was created
    assert spill_dir.exists()
    files = list(spill_dir.glob("*.jsonl"))
    assert len(files) > 0

    # Verify content
    with open(files[0], "r") as f:
        line = f.readline()
        obj = json.loads(line)
        assert obj["_id"] == "id-1"


def test_compute_cost_usd_anthropic_sonnet():
    """compute_cost_usd calculates cost for Anthropic models."""
    import chatlog_core

    cost = chatlog_core.compute_cost_usd(
        "anthropic",
        "claude-sonnet-4-5",
        tokens_in=1_000_000,
        tokens_out=500_000,
    )

    assert cost is not None
    # Sonnet-4-5: in=3.0, out=15.0 per 1M
    # cost = (1M / 1M) * 3.0 + (0.5M / 1M) * 15.0 = 3.0 + 7.5 = 10.5
    assert cost == 10.5


def test_compute_cost_usd_unknown_model():
    """compute_cost_usd returns None for unknown model."""
    import chatlog_core

    cost = chatlog_core.compute_cost_usd(
        "unknown_provider",
        "unknown_model",
        tokens_in=1_000_000,
        tokens_out=500_000,
    )

    assert cost is None


def test_compute_cost_usd_missing_tokens():
    """compute_cost_usd returns None if tokens missing."""
    import chatlog_core

    cost = chatlog_core.compute_cost_usd(
        "anthropic",
        "claude-sonnet-4-5",
        tokens_in=None,
        tokens_out=500_000,
    )

    assert cost is None


def test_compute_cost_usd_only_input_tokens():
    """compute_cost_usd handles only input tokens."""
    import chatlog_core

    cost = chatlog_core.compute_cost_usd(
        "anthropic",
        "claude-haiku-4-5",
        tokens_in=1_000_000,
        tokens_out=None,
    )

    assert cost is not None
    # Haiku: in=1.0 per 1M
    assert cost == 1.0
