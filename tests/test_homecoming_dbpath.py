"""Tests for homecoming.py's chatlog db_path rewrite (Project Homecoming fix).

A verbatim copy of .chatlog_config.json leaves its pinned `db_path` pointing at
the OLD location, so the chatlog falls back there even after the DB is migrated.
homecoming._rewrite_chatlog_db_path must repoint it at the new engine root,
preserve other config keys, and be idempotent.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import homecoming  # noqa: E402


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def test_rewrites_stale_db_path(tmp_path):
    cfg = tmp_path / ".chatlog_config.json"
    _write(cfg, {"db_path": "/old/m3-memory/memory/agent_chatlog.db", "keep": 1})
    engine_root = str(tmp_path / "engine")

    homecoming._rewrite_chatlog_db_path(str(cfg), engine_root)

    out = json.loads(cfg.read_text())
    assert out["db_path"] == os.path.join(engine_root, "agent_chatlog.db")
    assert out["keep"] == 1  # other keys preserved


def test_idempotent(tmp_path):
    cfg = tmp_path / ".chatlog_config.json"
    engine_root = str(tmp_path / "engine")
    correct = os.path.join(engine_root, "agent_chatlog.db")
    _write(cfg, {"db_path": correct})

    homecoming._rewrite_chatlog_db_path(str(cfg), engine_root)
    assert json.loads(cfg.read_text())["db_path"] == correct


def test_missing_db_path_key_is_set(tmp_path):
    # A config with no db_path at all gets one pointing at the engine root.
    cfg = tmp_path / ".chatlog_config.json"
    engine_root = str(tmp_path / "engine")
    _write(cfg, {"unrelated": "x"})

    homecoming._rewrite_chatlog_db_path(str(cfg), engine_root)
    out = json.loads(cfg.read_text())
    assert out["db_path"] == os.path.join(engine_root, "agent_chatlog.db")
    assert out["unrelated"] == "x"


def test_unreadable_config_does_not_raise(tmp_path):
    # A non-existent / unreadable config must be a quiet no-op, not a crash.
    homecoming._rewrite_chatlog_db_path(str(tmp_path / "nope.json"), str(tmp_path))


def test_malformed_json_does_not_raise(tmp_path):
    cfg = tmp_path / ".chatlog_config.json"
    cfg.write_text("{not valid json")
    homecoming._rewrite_chatlog_db_path(str(cfg), str(tmp_path))  # no exception
