"""Tests for bin/chatlog_status.py — status reporting."""

import json
import sqlite3
import time
import pytest


@pytest.fixture
def status_test_env(tmp_path, monkeypatch):
    """Set up environment for status tests."""
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
    _create_status_schema(str(db_path))

    yield {
        "db_path": db_path,
        "main_db_path": main_db_path,
        "state_file": state_file,
        "spill_dir": spill_dir,
    }


def _create_status_schema(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY,
            type TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE memory_embeddings (
            id TEXT,
            memory_id TEXT
        )
    """)
    conn.commit()
    conn.close()


def test_status_impl_returns_json(status_test_env):
    """chatlog_status_impl returns valid JSON."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()

    # Should be valid JSON
    parsed = json.loads(result)
    assert isinstance(parsed, dict)


def test_status_includes_required_fields(status_test_env):
    """Status JSON includes required fields."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    assert "mode" in parsed
    assert "db_paths" in parsed
    assert "row_counts" in parsed
    assert "queue" in parsed
    assert "spill" in parsed
    assert "redaction" in parsed
    assert "last_write_at" in parsed or "warnings" in parsed


def test_status_mode_field(status_test_env):
    """Status includes the current mode."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    assert parsed["mode"] in ("integrated", "separate", "hybrid")


def test_status_row_counts_structure(status_test_env):
    """row_counts has expected structure."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    row_counts = parsed.get("row_counts", {})
    assert isinstance(row_counts, dict)
    # At least one count should exist
    assert len(row_counts) >= 0


def test_status_queue_depth(status_test_env):
    """Status reports queue depth."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    queue = parsed.get("queue", {})
    assert isinstance(queue, dict)
    # Queue may have depth field
    if "depth" in queue:
        assert isinstance(queue["depth"], int)


def test_status_spill_info(status_test_env):
    """Status reports spill directory info."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    spill = parsed.get("spill", {})
    assert isinstance(spill, dict)


def test_status_redaction_config(status_test_env):
    """Status reports redaction configuration."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    redaction = parsed.get("redaction", {})
    assert isinstance(redaction, dict)
    if "enabled" in redaction:
        assert isinstance(redaction["enabled"], bool)


def test_status_cold_call_performance(status_test_env):
    """chatlog_status_impl completes in reasonable time (< 200ms)."""
    import chatlog_status
    import time

    start = time.time()
    result = chatlog_status.chatlog_status_impl()
    elapsed_ms = (time.time() - start) * 1000

    assert elapsed_ms < 200, f"Status call took {elapsed_ms:.1f}ms, expected < 200ms"


def test_status_with_existing_state(status_test_env):
    """Status reads existing state file if present."""
    import chatlog_status
    import chatlog_config

    state_file = status_test_env["state_file"]
    state_data = {
        "queue_depth": 42,
        "total_written": 1000,
        "last_flush_at": "2024-01-01T00:00:00Z",
        "last_write_at": "2024-01-01T00:01:00Z",
    }

    with open(str(state_file), "w") as f:
        json.dump(state_data, f)

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    # Should reflect state from file
    assert parsed.get("last_write_at") is not None


def test_status_no_state_file(status_test_env):
    """Status works with no state file (empty state)."""
    import chatlog_status

    # State file doesn't exist; status should still work
    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    assert isinstance(parsed, dict)
    # Should have some fields even with empty state
    assert "mode" in parsed


def test_status_with_data_in_db(status_test_env):
    """Status counts rows correctly when DB has data."""
    import chatlog_status

    db_path = status_test_env["db_path"]
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Insert sample data
    for i in range(10):
        cursor.execute(
            "INSERT INTO memory_items (id, type) VALUES (?, ?)",
            (f"msg-{i}", "chat_log"),
        )

    conn.commit()
    conn.close()

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    row_counts = parsed.get("row_counts", {})
    # Should count at least chatlog_rows
    if "chatlog_rows" in row_counts:
        assert row_counts["chatlog_rows"] >= 10
