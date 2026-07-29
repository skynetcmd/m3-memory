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


def test_claude_hook_command_is_pinned(monkeypatch):
    monkeypatch.setenv("M3_MEMORY_ROOT", "/data/.m3")
    monkeypatch.setenv("M3_ENGINE_ROOT", "/data/.m3/engine")
    monkeypatch.setenv("M3_CONFIG_ROOT", "/data/.m3/config")

    class _CC:
        stop_hook = True

    class _Cfg:
        host_agents = {"claude-code": _CC()}

    cmd, stop = ci._build_claude_hook_command(_Cfg())
    assert "M3_ENGINE_ROOT=" in cmd and "M3_CONFIG_ROOT=" in cmd
    # pins come first, before the interpreter/script
    assert cmd.index("M3_ENGINE_ROOT=") < cmd.index("/bin/sh") if "/bin/sh" in cmd else True
    assert stop is True
