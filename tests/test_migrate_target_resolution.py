"""Phase F1/F2 tests: hardened migrate_memory.targets() target resolution.

Cover:
- _classify_db on main / chatlog / empty / unknown DB shapes
- targets() refuses to attach main migrations to a chatlog-shaped DB
- targets() recovery path when M3_DATABASE points at the chatlog DB
- _ensure_sync_tables passes --target chatlog when the active DB is chatlog
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import unittest.mock as mock

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN_DIR = os.path.join(REPO_ROOT, "bin")
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

import migrate_memory  # noqa: E402


def _make_main_db(path: str) -> None:
    """Build a minimal DB with main-only signatures."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE memory_items (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, filename TEXT)")
    conn.commit()
    conn.close()


def _make_chatlog_db(path: str) -> None:
    """Build a minimal DB with chatlog storage but no main signatures."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memory_items (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE memory_embeddings (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE memory_relationships (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE schema_versions (version INTEGER PRIMARY KEY, filename TEXT)")
    conn.commit()
    conn.close()


# ── _classify_db ────────────────────────────────────────────────────────────

def test_classify_main_db(tmp_path):
    p = tmp_path / "main.db"
    _make_main_db(str(p))
    assert migrate_memory._classify_db(str(p)) == "main"


def test_classify_chatlog_db(tmp_path):
    p = tmp_path / "chatlog.db"
    _make_chatlog_db(str(p))
    assert migrate_memory._classify_db(str(p)) == "chatlog"


def test_classify_missing_file(tmp_path):
    assert migrate_memory._classify_db(str(tmp_path / "nope.db")) == "empty"


def test_classify_empty_db(tmp_path):
    p = tmp_path / "empty.db"
    sqlite3.connect(str(p)).close()
    assert migrate_memory._classify_db(str(p)) == "empty"


def test_classify_unknown_db(tmp_path):
    """A DB with arbitrary user tables but no main / chatlog signatures."""
    p = tmp_path / "weird.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE arbitrary_thing (id INTEGER)")
    conn.commit()
    conn.close()
    assert migrate_memory._classify_db(str(p)) == "unknown"


# ── targets() hardening ─────────────────────────────────────────────────────

def test_targets_refuses_main_when_path_is_chatlog(tmp_path, monkeypatch):
    """Core F1 invariant: if M3_DATABASE points at a chatlog-shaped DB, the
    main target is dropped — never apply main-stack DDL to a chatlog DB.
    """
    chatlog_path = tmp_path / "fake_chatlog.db"
    _make_chatlog_db(str(chatlog_path))
    monkeypatch.setenv("M3_DATABASE", str(chatlog_path))

    # Force re-resolution
    import importlib
    importlib.reload(migrate_memory)

    ts = migrate_memory.targets("main")
    assert ts == [], "main target must be empty when M3_DATABASE points at a chatlog DB"


def test_targets_recovery_routes_chatlog_migrations(tmp_path, monkeypatch):
    """Recovery: --target chatlog still gets a target even when M3_DATABASE
    is misconfigured at the chatlog file."""
    chatlog_path = tmp_path / "fake_chatlog.db"
    _make_chatlog_db(str(chatlog_path))
    monkeypatch.setenv("M3_DATABASE", str(chatlog_path))
    monkeypatch.setenv("CHATLOG_DB_PATH", str(chatlog_path))

    import importlib
    importlib.reload(migrate_memory)

    ts = migrate_memory.targets("chatlog")
    assert len(ts) == 1
    assert ts[0].name == "chatlog"
    assert os.path.abspath(ts[0].db_path) == os.path.abspath(str(chatlog_path))
    assert ts[0].migrations_dir.endswith("chatlog_migrations")


def test_targets_clean_env_returns_main_and_chatlog(monkeypatch):
    """Negative control: with no M3_DATABASE override, targets('all') returns
    one main and one chatlog target with the correct migrations dirs."""
    monkeypatch.delenv("M3_DATABASE", raising=False)
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)

    import importlib
    importlib.reload(migrate_memory)

    ts = migrate_memory.targets("all")
    names = {t.name for t in ts}
    # main is always present; chatlog only if its file passes classification.
    assert "main" in names
    for t in ts:
        if t.name == "main":
            assert t.migrations_dir.endswith("migrations")
            assert not t.migrations_dir.endswith("chatlog_migrations")
        elif t.name == "chatlog":
            assert t.migrations_dir.endswith("chatlog_migrations")


# ── _ensure_sync_tables guard ───────────────────────────────────────────────

def test_ensure_sync_tables_passes_target_chatlog_for_chatlog_path(tmp_path, monkeypatch):
    """When the active DB is a chatlog DB, _ensure_sync_tables must invoke
    migrate_memory.py with --target chatlog so the runner doesn't even
    consider the main migration stack."""
    chatlog_path = tmp_path / "fake_chatlog.db"
    _make_chatlog_db(str(chatlog_path))

    # Wire chatlog_config to point at our temp DB.
    monkeypatch.setenv("CHATLOG_DB_PATH", str(chatlog_path))

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env", {})
        return mock.Mock(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Reload memory_core's _ensure_sync_tables module-level constants if needed
    import memory_core  # noqa: WPS433
    memory_core._ensure_sync_tables(str(chatlog_path))

    assert "cmd" in captured, "_ensure_sync_tables did not call subprocess.run"
    assert "--target" in captured["cmd"], (
        f"expected --target flag in subprocess args, got {captured['cmd']}"
    )
    target_idx = captured["cmd"].index("--target")
    assert captured["cmd"][target_idx + 1] == "chatlog"
    # And M3_DATABASE in the child env points at the chatlog path
    assert os.path.abspath(captured["env"].get("M3_DATABASE", "")) == os.path.abspath(str(chatlog_path))


def test_ensure_sync_tables_no_target_for_main_path(tmp_path, monkeypatch):
    """For a main-shaped active DB, no --target flag is added — the runner
    handles all targets in 'all' mode."""
    main_path = tmp_path / "fake_main.db"
    _make_main_db(str(main_path))

    monkeypatch.setenv("M3_DATABASE", str(main_path))
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return mock.Mock(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    import memory_core
    memory_core._ensure_sync_tables(str(main_path))

    assert "cmd" in captured
    assert "--target" not in captured["cmd"], (
        f"expected NO --target flag, got {captured['cmd']}"
    )
