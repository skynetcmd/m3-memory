"""Tests for bin/chatlog_config.py — configuration resolution and caching.

The three-mode (integrated/separate/hybrid) system was removed in the
2026-04-21 DB-parameter refactor. The chatlog DB path now resolves via:
    CHATLOG_DB_PATH env > active_database() ContextVar > a db_path pinned in
    .chatlog_config.json > M3_DATABASE env > default (agent_chatlog.db).
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


def test_pinned_chatlog_path_beats_m3_database(tmp_path, monkeypatch):
    """A split install pins its chatlog in the config file. M3_DATABASE set for
    one process (the cognitive loop, a launchd/systemd job) names the MAIN store
    and must not pull the chatlog onto it: sync then drops the chatlog target
    as "the same file" and the scheduled job never replicates it."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    config_path.write_text(json.dumps({"db_path": "/engine/agent_chatlog.db"}))
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)
    monkeypatch.delenv("M3_CHATLOG_DB_PATH", raising=False)
    monkeypatch.setenv("M3_DATABASE", "/engine/agent_memory.db")
    chatlog_config.invalidate_cache()

    assert chatlog_config.chatlog_db_path() == "/engine/agent_chatlog.db"
    assert chatlog_config.resolve_config().db_path == "/engine/agent_chatlog.db"


def _pin_env(monkeypatch, tmp_path, file_data=None):
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    if file_data is not None:
        config_path.write_text(json.dumps(file_data))
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.setattr(chatlog_config, "DEFAULT_DB_PATH", "/engine/agent_chatlog.db")
    for k in ("CHATLOG_DB_PATH", "M3_CHATLOG_DB_PATH", "M3_DATABASE"):
        monkeypatch.delenv(k, raising=False)
    chatlog_config.invalidate_cache()
    return chatlog_config, config_path


def test_setup_pins_the_split_path_when_nothing_pins_one(tmp_path, monkeypatch):
    cc, path = _pin_env(monkeypatch, tmp_path, {"capture_mode": "both"})
    assert cc.pin_split_path() == "/engine/agent_chatlog.db"
    assert json.loads(path.read_text()) == {"capture_mode": "both",
                                            "db_path": "/engine/agent_chatlog.db"}
    assert cc.pin_split_path() is None          # idempotent


def test_pin_leaves_a_unified_or_explicit_choice_alone(tmp_path, monkeypatch):
    cc, path = _pin_env(monkeypatch, tmp_path)
    monkeypatch.setenv("M3_DATABASE", "/unified/main.db")   # documented unify
    assert cc.pin_split_path() is None and not path.exists()
    monkeypatch.delenv("M3_DATABASE")
    monkeypatch.setenv("M3_CHATLOG_DB_PATH", "/explicit/chat.db")
    assert cc.pin_split_path() is None and not path.exists()


def test_pin_keeps_an_existing_pin(tmp_path, monkeypatch):
    cc, path = _pin_env(monkeypatch, tmp_path, {"db_path": "/custom/chat.db"})
    assert cc.pin_split_path() is None
    assert json.loads(path.read_text())["db_path"] == "/custom/chat.db"
