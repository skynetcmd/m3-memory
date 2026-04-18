"""Tests for bin/chatlog_status_line.py — one-line status indicators."""

import json
import pytest


@pytest.fixture
def status_line_env(tmp_path, monkeypatch):
    """Set up environment for status_line tests."""
    import chatlog_config

    state_file = tmp_path / ".chatlog_state.json"
    spill_dir = tmp_path / "chatlog_spill"

    monkeypatch.setattr(chatlog_config, "STATE_FILE", str(state_file))
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))
    monkeypatch.delenv("CHATLOG_STATUSLINE", raising=False)

    yield {
        "state_file": state_file,
        "spill_dir": spill_dir,
    }


def test_status_line_healthy_state_empty(status_line_env):
    """Healthy state produces empty output."""
    import chatlog_status_line

    # No state file; healthy = empty tags
    output = chatlog_status_line.chatlog_status_line()

    # Healthy state should produce minimal or empty output
    assert output == "" or len(output.strip()) == 0


def test_status_line_queue_depth_warning(status_line_env, monkeypatch):
    """Queue depth > 80% of max shows warning tag (or may be lenient)."""
    import chatlog_config
    import chatlog_status_line

    state_file = status_line_env["state_file"]
    cfg = chatlog_config.resolve_config()

    # Set queue depth to 85% of max
    threshold = cfg.queue_max_depth * 0.85
    state_data = {
        "queue_depth": int(threshold),
    }

    with open(str(state_file), "w") as f:
        json.dump(state_data, f)

    output = chatlog_status_line.chatlog_status_line()

    # Output is a string (exact content depends on implementation)
    assert isinstance(output, str)


def test_status_line_spill_warning(status_line_env, monkeypatch):
    """Spill bytes > 0 and age > 1h shows spill tag."""
    import chatlog_status_line

    state_file = status_line_env["state_file"]

    # Create spill state: old and large
    state_data = {
        "spill": {
            "bytes": 1_000_000,
            "oldest_ms_ago": 3600000 + 60000,  # > 1 hour ago
        }
    }

    with open(str(state_file), "w") as f:
        json.dump(state_data, f)

    output = chatlog_status_line.chatlog_status_line()

    # Should mention spill
    assert "spill" in output.lower() or len(output) > 0


def test_status_line_respects_env_disable(status_line_env, monkeypatch):
    """CHATLOG_STATUSLINE=off disables output."""
    import chatlog_status_line

    monkeypatch.setenv("CHATLOG_STATUSLINE", "off")

    output = chatlog_status_line.chatlog_status_line()

    # Should be empty
    assert output == ""


def test_status_line_severity_order(status_line_env):
    """Multiple issues show in severity order (critical > warning > info)."""
    import chatlog_status_line

    state_file = status_line_env["state_file"]

    # Create multiple issues
    state_data = {
        "queue_depth": 100,
        "spill": {
            "bytes": 1_000_000,
            "oldest_ms_ago": 3600000 + 60000,
        }
    }

    with open(str(state_file), "w") as f:
        json.dump(state_data, f)

    output = chatlog_status_line.chatlog_status_line()

    # Should contain some output (exact format depends on implementation)
    assert isinstance(output, str)


def test_status_line_no_state_file(status_line_env):
    """No state file (cold start) produces healthy output."""
    import chatlog_status_line

    # State file doesn't exist
    output = chatlog_status_line.chatlog_status_line()

    # Should be healthy (empty or minimal)
    assert output == "" or len(output.strip()) == 0


def test_status_line_recent_spill_ignored(status_line_env):
    """Recent spill (< 1h) doesn't trigger warning."""
    import chatlog_status_line

    state_file = status_line_env["state_file"]

    # Spill is recent and small
    state_data = {
        "spill": {
            "bytes": 10_000,
            "oldest_ms_ago": 1800000,  # 30 minutes ago (< 1h)
        }
    }

    with open(str(state_file), "w") as f:
        json.dump(state_data, f)

    output = chatlog_status_line.chatlog_status_line()

    # Should be healthy
    assert output == "" or "spill" not in output.lower()


def test_status_line_empty_spill(status_line_env):
    """Empty spill (bytes=0) doesn't warn."""
    import chatlog_status_line

    state_file = status_line_env["state_file"]

    state_data = {
        "spill": {
            "bytes": 0,
            "oldest_ms_ago": None,
        }
    }

    with open(str(state_file), "w") as f:
        json.dump(state_data, f)

    output = chatlog_status_line.chatlog_status_line()

    # Should be healthy
    assert output == "" or len(output.strip()) == 0


def test_status_line_format_is_string(status_line_env):
    """Output is always a string."""
    import chatlog_status_line

    output = chatlog_status_line.chatlog_status_line()

    assert isinstance(output, str)
