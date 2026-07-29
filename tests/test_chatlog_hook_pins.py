"""chatlog_init's Claude/Gemini capture hooks must inline-pin the data roots.

Regression: _build_claude_hook_command wrote `/bin/sh {sh}` with no
M3_ENGINE_ROOT/M3_CONFIG_ROOT prefix. A capture hook inherits the agent's PROCESS
env (not the MCP server env block), so unpinned hooks diverge the two chatlog
halves (split-brain) — and this writer would revert the pinned hooks that
generate_configs / `m3 doctor --fix --fix-hooks` install.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import chatlog_init as ci  # noqa: E402


def test_root_env_prefix_contains_both_pins(monkeypatch):
    monkeypatch.setenv("M3_MEMORY_ROOT", "/data/.m3")
    monkeypatch.setenv("M3_ENGINE_ROOT", "/data/.m3/engine")
    monkeypatch.setenv("M3_CONFIG_ROOT", "/data/.m3/config")
    p = ci._root_env_prefix()
    assert "M3_ENGINE_ROOT=" in p and "M3_CONFIG_ROOT=" in p
    assert p.endswith(" ")  # prefixes the command directly


def test_apply_claude_settings_delegates_to_canonical_writer(monkeypatch):
    """apply_claude_settings (the setup capture-wiring writer) must delegate to
    generate_configs.install_claude_settings — the single canonical Claude writer
    — so `m3 setup` produces the same pinned .py hooks + roots the doctor writes,
    and stops emitting its own /bin/sh add-only hooks."""
    import types

    called = {"install": 0}
    import generate_configs
    monkeypatch.setattr(
        generate_configs, "install_claude_settings",
        lambda **k: called.__setitem__("install", called["install"] + 1) or {"changed": True},
    )
    # npm-PATH side-fix must stay best-effort and not block.
    import m3_memory.installer as _inst
    monkeypatch.setattr(_inst, "_fix_npm_global_path", lambda: "")

    cfg = types.SimpleNamespace(host_agents={"claude-code": types.SimpleNamespace(stop_hook=True)})
    changed, msg = ci.apply_claude_settings(cfg)
    assert called["install"] == 1
    assert changed is True
    assert "mcpServers" in msg  # wrote the full config, not just hooks

    # The old /bin/sh hook builders are gone.
    assert not hasattr(ci, "_build_claude_hook_command")
    assert not hasattr(ci, "_build_claude_settings_patch")
