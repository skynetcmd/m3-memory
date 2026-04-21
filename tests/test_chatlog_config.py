"""Tests for bin/chatlog_config.py — configuration resolution and caching.

The three-mode (integrated/separate/hybrid) system was removed in the
2026-04-21 DB-parameter refactor. The chatlog DB path now resolves via:
    CHATLOG_DB_PATH env > active_database() ContextVar > M3_DATABASE env
    > .chatlog_config.json db_path > default (agent_chatlog.db).
The legacy ``mode`` field in .chatlog_config.json and the CHATLOG_MODE env
var are silently ignored (a one-time warning is emitted for CHATLOG_MODE).
"""

import json


def test_env_overrides_file(tmp_path, monkeypatch):
    """CHATLOG_DB_PATH env overrides file + defaults."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    config_path.write_text(json.dumps({"db_path": "/file/path.db"}))

    monkeypatch.setenv("CHATLOG_DB_PATH", "/env/path.db")
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    chatlog_config.invalidate_cache()

    assert chatlog_config.chatlog_db_path() == "/env/path.db"


def test_m3_database_env_flows_into_chatlog(tmp_path, monkeypatch):
    """M3_DATABASE env unifies chatlog with main when CHATLOG_DB_PATH is unset."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)
    monkeypatch.setenv("M3_DATABASE", "/unified/main.db")
    chatlog_config.invalidate_cache()

    assert chatlog_config.chatlog_db_path() == "/unified/main.db"


def test_chatlog_db_path_env_beats_m3_database(tmp_path, monkeypatch):
    """CHATLOG_DB_PATH wins over M3_DATABASE (explicit chatlog override)."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.setenv("M3_DATABASE", "/main.db")
    monkeypatch.setenv("CHATLOG_DB_PATH", "/chatlog.db")
    chatlog_config.invalidate_cache()

    assert chatlog_config.chatlog_db_path() == "/chatlog.db"


def test_active_database_contextvar_beats_m3_database_env(tmp_path, monkeypatch):
    """active_database() ContextVar wins over M3_DATABASE env (per-call override)."""
    import chatlog_config
    from m3_sdk import active_database

    config_path = tmp_path / ".chatlog_config.json"
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)
    monkeypatch.setenv("M3_DATABASE", "/main.db")
    chatlog_config.invalidate_cache()

    with active_database("/per-call.db"):
        assert chatlog_config.chatlog_db_path().endswith("per-call.db")


def test_chatlog_db_path_env_beats_contextvar(tmp_path, monkeypatch):
    """Explicit CHATLOG_DB_PATH still wins even when a ContextVar override is active."""
    import chatlog_config
    from m3_sdk import active_database

    config_path = tmp_path / ".chatlog_config.json"
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.setenv("CHATLOG_DB_PATH", "/chatlog.db")
    chatlog_config.invalidate_cache()

    with active_database("/per-call.db"):
        assert chatlog_config.chatlog_db_path() == "/chatlog.db"


def test_file_overrides_defaults(tmp_path, monkeypatch):
    """File config loads and overrides queue/redaction defaults (non-path fields)."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    config_path.write_text(json.dumps({
        "db_path": "/file/path.db",
        "queue_flush_rows": 500,
        "queue_max_depth": 50000,
    }))

    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)
    monkeypatch.delenv("M3_DATABASE", raising=False)
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    chatlog_config.invalidate_cache()

    cfg = chatlog_config.resolve_config()
    assert cfg.db_path == "/file/path.db"
    assert cfg.queue_flush_rows == 500
    assert cfg.queue_max_depth == 50000


def test_invalidate_cache_forces_reread(tmp_path, monkeypatch):
    """invalidate_cache() forces re-resolve on next call."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    config_path.write_text(json.dumps({"db_path": "/first.db"}))

    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)
    monkeypatch.delenv("M3_DATABASE", raising=False)
    chatlog_config.invalidate_cache()

    cfg1 = chatlog_config.resolve_config()
    assert cfg1.db_path == "/first.db"

    config_path.write_text(json.dumps({"db_path": "/second.db"}))
    chatlog_config.invalidate_cache()
    cfg2 = chatlog_config.resolve_config()
    assert cfg2.db_path == "/second.db"


def test_legacy_mode_field_ignored(tmp_path, monkeypatch):
    """A stale `mode` key in .chatlog_config.json is silently ignored."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    config_path.write_text(json.dumps({
        "mode": "hybrid",      # ignored
        "db_path": "/real.db",
    }))
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)
    monkeypatch.delenv("M3_DATABASE", raising=False)
    chatlog_config.invalidate_cache()

    cfg = chatlog_config.resolve_config()
    assert cfg.db_path == "/real.db"
    # Old dataclass field gone — accessing it should raise
    assert not hasattr(cfg, "mode")


def test_chatlog_mode_env_deprecation_does_not_raise(tmp_path, monkeypatch, caplog):
    """CHATLOG_MODE env is ignored with a one-time warning, not an error."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.setenv("CHATLOG_MODE", "separate")
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)
    monkeypatch.delenv("M3_DATABASE", raising=False)
    # Force re-emit of the warning for this test
    monkeypatch.setattr(chatlog_config, "_MODE_WARN_EMITTED", False)
    chatlog_config.invalidate_cache()

    # Must not raise
    chatlog_config.resolve_config()


def test_redaction_spec_config(tmp_path, monkeypatch):
    """Redaction spec loads from config with all sub-fields."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    config_path.write_text(json.dumps({
        "redaction": {
            "enabled": True,
            "patterns": ["api_keys", "jwt"],
            "custom_regex": ["^secret:", "^token:"],
            "redact_pii": True,
            "store_original_hash": True,
        }
    }))
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)
    monkeypatch.delenv("M3_DATABASE", raising=False)
    chatlog_config.invalidate_cache()

    cfg = chatlog_config.resolve_config()
    assert cfg.redaction.enabled is True
    assert "api_keys" in cfg.redaction.patterns
    assert "jwt" in cfg.redaction.patterns
    assert cfg.redaction.redact_pii is True
    assert cfg.redaction.store_original_hash is True
    assert len(cfg.redaction.custom_regex) == 2


def test_defaults_when_no_config(tmp_path, monkeypatch):
    """Defaults apply when no config file exists and no env vars set."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)
    monkeypatch.delenv("M3_DATABASE", raising=False)
    chatlog_config.invalidate_cache()

    cfg = chatlog_config.resolve_config()
    # Default path is the dedicated chatlog file
    assert cfg.db_path == chatlog_config.DEFAULT_DB_PATH
    assert cfg.queue_flush_rows == 200
    assert cfg.queue_max_depth == 20_000
    assert cfg.redaction.enabled is False
    assert cfg.cost_tracking.enabled is True
