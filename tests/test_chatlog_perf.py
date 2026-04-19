"""Performance tests for chatlog subsystem (marked slow)."""

import asyncio
import time
import pytest
import sqlite3

from conftest import isolate_chatlog_env, create_memory_items_schema


@pytest.fixture
def perf_test_env(tmp_path, monkeypatch):
    """Isolated chatlog env for perf tests. See conftest.isolate_chatlog_env."""
    paths = isolate_chatlog_env(monkeypatch, tmp_path)
    create_memory_items_schema(paths["db_path"])
    yield paths


@pytest.mark.slow
@pytest.mark.asyncio
async def test_chatlog_perf_10k_enqueue(perf_test_env):
    """Enqueue 10k items via write_bulk_impl, measure throughput."""
    import chatlog_core

    items = []
    for i in range(10000):
        items.append({
            "content": f"Message {i} with some content",
            "role": "user" if i % 2 == 0 else "assistant",
            "conversation_id": f"conv-{i % 100}",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
            "turn_index": i,
            "variant": "test",
        })

    start = time.time()
    result = await chatlog_core.chatlog_write_bulk_impl(items)
    enqueue_time_ms = (time.time() - start) * 1000

    assert result["failed"] == 0
    assert len(result["written_ids"]) == 10000

    # Enqueue should be fast (< 200ms for 10k items)
    assert enqueue_time_ms < 200, f"Enqueue took {enqueue_time_ms:.1f}ms"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_chatlog_perf_flush_throughput(perf_test_env):
    """Write 10k items and flush, measure flush throughput."""
    import chatlog_core

    items = [
        {
            "content": f"Item {i}",
            "role": "user" if i % 2 == 0 else "assistant",
            "conversation_id": f"conv-{i % 100}",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
            "variant": "test",
        }
        for i in range(10000)
    ]

    await chatlog_core.chatlog_write_bulk_impl(items)

    start = time.time()
    written = await chatlog_core._flush_once()
    flush_time_ms = (time.time() - start) * 1000

    # Flush of 10k items should be reasonably fast
    # (This is generous; adjust downward as optimization improves)
    assert flush_time_ms < 5000, f"Flush took {flush_time_ms:.1f}ms"
    assert written > 0


@pytest.mark.slow
@pytest.mark.asyncio
async def test_chatlog_perf_batch_write_latency(perf_test_env):
    """Measure p95 latency of individual writes in a batch."""
    import chatlog_core

    latencies = []

    for i in range(1000):
        item = {
            "content": f"Message {i}",
            "role": "user",
            "conversation_id": f"conv-{i % 10}",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
            "variant": "test",
        }

        start = time.time()
        result = await chatlog_core.chatlog_write_bulk_impl([item])
        latency_ms = (time.time() - start) * 1000
        latencies.append(latency_ms)

        assert len(result["written_ids"]) == 1

    # Compute p95
    latencies.sort()
    p95_idx = int(len(latencies) * 0.95)
    p95_latency = latencies[p95_idx]

    # p95 write latency should be < 5ms (lenient)
    assert p95_latency < 5, f"p95 latency is {p95_latency:.2f}ms"


@pytest.mark.slow
def test_chatlog_perf_metadata_construction():
    """Measure metadata_json construction for 1000 items."""
    import chatlog_core
    import time

    start = time.time()

    for i in range(1000):
        item = {
            "content": f"Content {i}",
            "role": "user",
            "conversation_id": f"conv-{i}",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
            "tokens_in": 1000 + i,
            "tokens_out": 500 + i,
        }

        meta_json = chatlog_core._build_metadata(item)
        assert isinstance(meta_json, str)
        assert "anthropic" in meta_json

    elapsed_ms = (time.time() - start) * 1000

    # Should complete in < 100ms
    assert elapsed_ms < 100, f"Metadata construction took {elapsed_ms:.1f}ms"


@pytest.mark.slow
def test_chatlog_perf_validation():
    """Measure validation overhead for 1000 items."""
    import chatlog_core
    import time

    start = time.time()

    for i in range(1000):
        item = {
            "content": f"Content {i}",
            "role": "user" if i % 2 == 0 else "assistant",
            "conversation_id": f"conv-{i}",
            "host_agent": "claude-code",
            "provider": "anthropic",
            "model_id": "claude-3-sonnet",
        }

        try:
            chatlog_core._validate_write(item)
        except ValueError:
            pass  # Expected for some items

    elapsed_ms = (time.time() - start) * 1000

    # Validation should be fast (< 50ms for 1000 items)
    assert elapsed_ms < 50, f"Validation took {elapsed_ms:.1f}ms"
