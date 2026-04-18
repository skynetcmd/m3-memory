"""Tests for bin/chatlog_config.py — configuration resolution and caching."""

import json
import os
import tempfile
import pytest


def test_env_overrides_file(tmp_path, monkeypatch):
    """CHATLOG_MODE and CHATLOG_DB_PATH from env override file + defaults."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    config_path.write_text(json.dumps({"mode": "separate", "db_path": "/file/path.db"}))

    monkeypatch.setenv("CHATLOG_MODE", "integrated")
    monkeypatch.setenv("CHATLOG_DB_PATH", "/env/path.db")
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    chatlog_config.invalidate_cache()

    cfg = chatlog_config.resolve_config()
    assert cfg.mode == "integrated"
    assert cfg.db_path == "/env/path.db"


def test_file_overrides_defaults(tmp_path, monkeypatch):
    """File config loads and overrides defaults."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    config_path.write_text(json.dumps({
        "mode": "hybrid",
        "queue_flush_rows": 500,
        "queue_max_depth": 50000,
    }))

    monkeypatch.delenv("CHATLOG_MODE", raising=False)
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    chatlog_config.invalidate_cache()

    cfg = chatlog_config.resolve_config()
    assert cfg.mode == "hybrid"
    assert cfg.queue_flush_rows == 500
    assert cfg.queue_max_depth == 50000


def test_invalidate_cache_forces_reread(tmp_path, monkeypatch):
    """invalidate_cache() forces re-resolve on next call."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    config_path.write_text(json.dumps({"mode": "separate"}))

    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_MODE", raising=False)
    chatlog_config.invalidate_cache()

    cfg1 = chatlog_config.resolve_config()
    assert cfg1.mode == "separate"

    # Mutate file
    config_path.write_text(json.dumps({"mode": "hybrid"}))
    chatlog_config.invalidate_cache()
    cfg2 = chatlog_config.resolve_config()
    assert cfg2.mode == "hybrid"


def test_is_integrated_helper(tmp_path, monkeypatch):
    """is_integrated() returns True for integrated mode."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    config_path.write_text(json.dumps({"mode": "integrated"}))
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_MODE", raising=False)
    chatlog_config.invalidate_cache()

    assert chatlog_config.is_integrated() is True
    assert chatlog_config.is_separate_or_hybrid() is False


def test_is_separate_or_hybrid_helpers(tmp_path, monkeypatch):
    """is_separate_or_hybrid() returns True for separate and hybrid modes."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"

    # Test separate
    config_path.write_text(json.dumps({"mode": "separate"}))
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_MODE", raising=False)
    chatlog_config.invalidate_cache()
    assert chatlog_config.is_separate_or_hybrid() is True
    assert chatlog_config.is_integrated() is False

    # Test hybrid
    config_path.write_text(json.dumps({"mode": "hybrid"}))
    chatlog_config.invalidate_cache()
    assert chatlog_config.is_separate_or_hybrid() is True
    assert chatlog_config.is_integrated() is False


def test_effective_db_path_integrated_mode(tmp_path, monkeypatch):
    """In integrated mode, effective_db_path() returns MAIN_DB_PATH."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    config_path.write_text(json.dumps({
        "mode": "integrated",
        "db_path": "/ignored/path.db"
    }))
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_MODE", raising=False)
    chatlog_config.invalidate_cache()

    cfg = chatlog_config.resolve_config()
    assert cfg.effective_db_path() == chatlog_config.MAIN_DB_PATH


def test_effective_db_path_separate_mode(tmp_path, monkeypatch):
    """In separate/hybrid mode, effective_db_path() returns the configured db_path."""
    import chatlog_config

    alt_db = str(tmp_path / "alt.db")
    config_path = tmp_path / ".chatlog_config.json"
    config_path.write_text(json.dumps({
        "mode": "separate",
        "db_path": alt_db
    }))
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_MODE", raising=False)
    chatlog_config.invalidate_cache()

    cfg = chatlog_config.resolve_config()
    assert cfg.effective_db_path() == alt_db


def test_invalid_mode_fallback_to_default(tmp_path, monkeypatch):
    """Invalid mode in env/file falls back to default (separate)."""
    import chatlog_config

    config_path = tmp_path / ".chatlog_config.json"
    config_path.write_text(json.dumps({"mode": "invalid"}))
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_MODE", raising=False)
    chatlog_config.invalidate_cache()

    cfg = chatlog_config.resolve_config()
    assert cfg.mode == "separate"


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
    monkeypatch.delenv("CHATLOG_MODE", raising=False)
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
    # File doesn't exist
    monkeypatch.setattr(chatlog_config, "CONFIG_PATH", str(config_path))
    monkeypatch.delenv("CHATLOG_MODE", raising=False)
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)
    chatlog_config.invalidate_cache()

    cfg = chatlog_config.resolve_config()
    assert cfg.mode == "separate"
    assert cfg.queue_flush_rows == 200
    assert cfg.queue_max_depth == 20_000
    assert cfg.redaction.enabled is False
    assert cfg.cost_tracking.enabled is True
