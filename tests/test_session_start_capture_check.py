"""Tests for the SessionStart chatlog-capture-check hook's DB resolution.

The hook must check the SAME agent-memory DB the running MCP server writes to,
resolved via the canonical m3_sdk path logic (honoring M3_ENGINE_ROOT /
M3_MEMORY_ROOT). A prior version resolved the DB from M3_HOME/engine, which
diverges from the server's M3_ENGINE_ROOT on Homecoming-migrated installs — the
"split-brain" that produced a false "chatlog NOT writing" alarm against a stale
pre-migration copy (documented in CLAUDE.md).
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "bin" / "hooks" / "chatlog" / "session_start_capture_check.py"
)


def _load_hook():
    spec = importlib.util.spec_from_file_location("sscc_under_test", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_db_uses_canonical_engine_root(monkeypatch, tmp_path):
    """With M3_ENGINE_ROOT set, the hook resolves to <engine_root>/agent_memory.db
    — the same path the server uses — not the repo-relative engine/ copy."""
    engine = tmp_path / "engine"
    engine.mkdir()
    monkeypatch.setenv("M3_ENGINE_ROOT", str(engine))
    monkeypatch.delenv("M3_DATABASE", raising=False)
    monkeypatch.delenv("M3_DB_PATH", raising=False)
    monkeypatch.delenv("M3_MEMORY_ROOT", raising=False)

    mod = _load_hook()
    db = mod._resolve_db()

    assert os.path.normcase(db) == os.path.normcase(
        str(engine / "agent_memory.db")
    ), db
    # Must NOT fall back to the repo-relative stale copy.
    assert "hooks" not in db


def test_resolve_db_never_returns_repo_stale_when_engine_root_set(monkeypatch, tmp_path):
    """The repo-relative fallback must lose to the canonical engine root."""
    engine = tmp_path / "eng2"
    engine.mkdir()
    monkeypatch.setenv("M3_ENGINE_ROOT", str(engine))
    monkeypatch.delenv("M3_DATABASE", raising=False)

    mod = _load_hook()
    db = Path(mod._resolve_db()).resolve()

    repo_relative = (_HOOK.parents[3] / "engine" / "agent_memory.db").resolve()
    assert db != repo_relative, "resolved the stale repo copy instead of engine root"


def test_resolve_db_honors_m3_database_override(monkeypatch, tmp_path):
    """An explicit M3_DATABASE wins (canonical resolver precedence)."""
    explicit = tmp_path / "explicit.db"
    monkeypatch.setenv("M3_DATABASE", str(explicit))

    mod = _load_hook()
    db = Path(mod._resolve_db()).resolve()

    assert db == explicit.resolve(), db
