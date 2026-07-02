"""Regression tests for the duplicate-MCP-registration bug (2h43m hang, 2026-07-02).

Two guards:
  1. generate_configs must NOT write the m3 servers into BOTH claude-settings.json
     and .mcp.json — .mcp.json's mcpServers must be empty (single registration
     source). Writing to both double-launched the memory bridge -> a write routed
     to the redundant bridge hung with no timeout.
  2. `m3 doctor --fix` (via installer._dedupe_mcp_registration) must AUTO-REMOVE a
     same-client duplicate, keeping the complete (env-carrying) def.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from m3_memory import installer as I  # noqa: E402


# ── Guard 1: generator never double-registers ────────────────────────────────
def test_generate_configs_writes_empty_mcp_json(tmp_path, monkeypatch):
    """generate_configs must emit a .mcp.json whose mcpServers is empty, so the
    only Claude Code registration lives in settings.json."""
    import generate_configs as gc

    written: dict[str, dict] = {}

    def fake_write(path, data):
        written[Path(path).name] = data

    monkeypatch.setattr(gc, "_write_json", fake_write, raising=False)
    # Neutralize the live-merge side effect if present.
    monkeypatch.setattr(gc, "install_claude_settings", lambda *a, **k: None, raising=False)

    # Call the generator; signature-tolerant (it takes repo root / python cmd).
    try:
        gc.generate_configs()
    except TypeError:
        gc.generate_configs(str(tmp_path))

    assert ".mcp.json" in written, "generate_configs did not write .mcp.json"
    mcp_servers = written[".mcp.json"].get("mcpServers", {})
    assert mcp_servers == {}, f".mcp.json must have NO servers, got: {list(mcp_servers)}"

    # And the servers ARE present in the claude settings (single source).
    claude = written.get("claude-settings.json", {})
    assert "memory" in claude.get("mcpServers", {}), "memory missing from claude settings"


# ── Guard 2: doctor --fix auto-removes a same-client duplicate ────────────────
def _mk(path: Path, servers: dict) -> None:
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_dedupe_removes_bare_keeps_complete(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    mcp = tmp_path / ".mcp.json"
    _mk(settings, {"memory": {"command": "py", "args": ["b"], "env": {"M3_ENGINE_ROOT": "x"}}})
    _mk(mcp, {"memory": {"command": "py", "args": ["b"]}})  # bare dup
    monkeypatch.setattr(I, "_client_config_sources",
                        lambda: {"Claude Code": [settings, mcp]})

    actions = I._dedupe_mcp_registration(apply=True)
    assert any("removed" in a and "memory" in a for a in actions)

    assert "memory" not in json.loads(mcp.read_text())["mcpServers"]
    kept = json.loads(settings.read_text())["mcpServers"]["memory"]
    assert kept.get("env"), "the complete (env) def must be the one kept"


def test_dedupe_is_idempotent_and_noop_when_clean(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    mcp = tmp_path / ".mcp.json"
    _mk(settings, {"memory": {"command": "py", "env": {"M3_ENGINE_ROOT": "x"}}})
    _mk(mcp, {})  # already clean
    monkeypatch.setattr(I, "_client_config_sources",
                        lambda: {"Claude Code": [settings, mcp]})
    assert I._dedupe_mcp_registration(apply=True) == []


def test_dedupe_dry_run_does_not_write(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    mcp = tmp_path / ".mcp.json"
    _mk(settings, {"memory": {"command": "py", "env": {"M3_ENGINE_ROOT": "x"}}})
    _mk(mcp, {"memory": {"command": "py"}})
    monkeypatch.setattr(I, "_client_config_sources",
                        lambda: {"Claude Code": [settings, mcp]})
    before = mcp.read_text()
    actions = I._dedupe_mcp_registration(apply=False)
    assert actions and "would remove" in actions[0]
    assert mcp.read_text() == before, "dry-run must not modify files"
