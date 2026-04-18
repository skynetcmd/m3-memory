"""Performance tests for chatlog subsystem (marked slow)."""

import asyncio
import time
import pytest
import sqlite3


@pytest.fixture
def perf_test_env(tmp_path, monkeypatch):
    """Set up environment for performance tests."""
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
    # CHATLOG_DB_PATH env is the reliable redirect: the dataclass default
    # `db_path: str = DEFAULT_DB_PATH` is captured at class-definition time,
    # so patching DEFAULT_DB_PATH alone doesn't affect new ChatlogConfig()
    # instances. The env var is applied after construction in resolve_config().
    monkeypatch.setenv("CHATLOG_DB_PATH", str(db_path))
    chatlog_config.invalidate_cache()
    # Reset connection pool so _ensure_pool() rebuilds against the temp DB.
    with chatlog_config._POOL_LOCK:
        chatlog_config._POOL = None
        chatlog_config._POOL_DB_PATH = None

    # Create schema
    _create_perf_schema(str(db_path))

    yield {
        "db_path": db_path,
        "main_db_path": main_db_path,
        "state_file": state_file,
        "spill_dir": spill_dir,
    }


def _create_perf_schema(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE memory_items (
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
        )
    """)
    conn.commit()
    conn.close()


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
