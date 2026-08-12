"""chatlog_init's Claude/Gemini capture hooks must inline-pin the data roots.

Regression: _build_claude_hook_command wrote `/bin/sh {sh}` with no
M3_ENGINE_ROOT/M3_CONFIG_ROOT prefix. A capture hook inherits the agent's PROCESS
env (not the MCP server env block), so unpinned hooks diverge the two chatlog
halves (split-brain) — and this writer would revert the pinned hooks that
generate_configs / `m3 doctor --fix --fix-hooks` install.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import chatlog_init as ci  # noqa: E402


def test_root_env_prefix_contains_both_pins(monkeypatch):
    """POSIX: both roots inline-pinned, trailing space so it prefixes directly."""
    monkeypatch.setattr(ci.os, "name", "posix")
    monkeypatch.setenv("M3_MEMORY_ROOT", "/data/.m3")
    monkeypatch.setenv("M3_ENGINE_ROOT", "/data/.m3/engine")
    monkeypatch.setenv("M3_CONFIG_ROOT", "/data/.m3/config")
    p = ci._root_env_prefix()
    assert "M3_ENGINE_ROOT=" in p and "M3_CONFIG_ROOT=" in p
    assert p.endswith(" ")  # prefixes the command directly


def test_root_env_prefix_is_empty_on_windows(monkeypatch):
    """Windows: NO inline env prefix — `VAR=val cmd` is POSIX shell syntax.

    Claude Code runs hooks through cmd.exe, which has no such construct: it
    tries to execute a program literally named `M3_ENGINE_ROOT=C:/...` and the
    hook dies before it starts, with capture failing silently. This killed
    chatlog capture for ~2 days on a live install (2026-08-09..11). The roots
    stay correct via .chatlog_config.json's db_path and the shared ~/.m3
    defaults, so omitting the prefix is safe, not a downgrade.
    """
    monkeypatch.setattr(ci.os, "name", "nt")
    monkeypatch.setenv("M3_ENGINE_ROOT", "C:/data/.m3/engine")
    monkeypatch.setenv("M3_CONFIG_ROOT", "C:/data/.m3/config")
    assert ci._root_env_prefix() == ""


def test_generate_configs_hook_prefix_is_os_aware(monkeypatch):
    """The canonical Claude writer must make the same OS split.

    Both writers emit hook commands; if only one is fixed the other reinstates
    the broken form on the next `m3 setup` / `m3 doctor --fix --fix-hooks`.
    """
    import generate_configs as gc

    monkeypatch.setattr(gc.os, "name", "posix")
    posix = gc._hook_env_prefix("/data/.m3/engine", "/data/.m3/config")
    assert posix.startswith("M3_ENGINE_ROOT=") and posix.endswith(" ")

    monkeypatch.setattr(gc.os, "name", "nt")
    assert gc._hook_env_prefix("C:/data/.m3/engine", "C:/data/.m3/config") == ""


def test_windows_hook_command_starts_with_the_interpreter(monkeypatch):
    """End-to-end shape check: on Windows the command must begin with a real
    executable path, never a `VAR=` token that cmd.exe would try to run."""
    import generate_configs as gc

    monkeypatch.setattr(gc.os, "name", "nt")
    prefix = gc._hook_env_prefix("C:/x/engine", "C:/x/config")
    cmd = f"{prefix}C:/venv/Scripts/python.exe C:/m3/bin/hooks/chatlog/x.py"
    first = cmd.split(" ", 1)[0]
    assert "=" not in first, f"hook command starts with an env assignment: {first!r}"
    assert first.endswith("python.exe")


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


def test_apply_gemini_settings_upgrades_stale_onexit_hook(monkeypatch, tmp_path):
    """apply_gemini_settings must UPGRADE a stale/unpinned Gemini SessionEnd hook
    (not skip it add-only) and preserve the user's own hooks — the Gemini analog
    of the Claude onExit unification. The memory entry gets the canonical
    roots-bearing shape too."""
    # Pin POSIX: the assertion below checks the inline root pins, which are
    # deliberately absent on Windows (see test_root_env_prefix_is_empty_on_windows).
    monkeypatch.setattr(ci.os, "name", "posix")
    monkeypatch.setattr(ci.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("M3_MEMORY_ROOT", "/data/.m3")
    monkeypatch.setenv("M3_ENGINE_ROOT", "/data/.m3/engine")
    monkeypatch.setenv("M3_CONFIG_ROOT", "/data/.m3/config")
    import m3_memory.installer as inst
    monkeypatch.setattr(inst, "_canonical_memory_server",
                        lambda: {"command": "/py", "args": ["/b.py"],
                                 "env": {"M3_ENGINE_ROOT": "/data/.m3/engine"}})
    monkeypatch.setattr(inst, "_fix_npm_global_path", lambda: "")

    gdir = tmp_path / ".gemini"
    gdir.mkdir()
    stale = {
        "mcpServers": {"memory": {"command": "mcp-memory"}},  # roots-less stub
        "hooks": {"SessionEnd": [
            {"hooks": [{"type": "command",
                        "command": "/bin/sh /old/repo/bin/hooks/chatlog/gemini_cli_onexit.sh"}]},
            {"hooks": [{"type": "command", "command": "/usr/bin/user_hook"}]},
        ]},
    }
    (gdir / "settings.json").write_text(json.dumps(stale))

    changed, _msg = ci.apply_gemini_settings()
    assert changed is True
    d = json.loads((gdir / "settings.json").read_text())
    se = d["hooks"]["SessionEnd"]
    m3 = [e for e in se if "chatlog" in json.dumps(e).lower()][0]["hooks"][0]["command"]
    assert "M3_ENGINE_ROOT=" in m3 and "M3_CONFIG_ROOT=" in m3   # now pinned
    assert "/old/repo" not in m3                                 # upgraded, not stale
    assert any("user_hook" in json.dumps(e) for e in se)        # user hook preserved
    assert "env" in d["mcpServers"]["memory"]                   # memory entry has roots


def test_apply_gemini_settings_idempotent_when_current(monkeypatch, tmp_path):
    """A second run with the config already correct reports no change."""
    monkeypatch.setattr(ci.Path, "home", classmethod(lambda cls: tmp_path))
    import m3_memory.installer as inst
    monkeypatch.setattr(inst, "_canonical_memory_server", lambda: {"command": "mcp-memory"})
    monkeypatch.setattr(inst, "_fix_npm_global_path", lambda: "")
    (tmp_path / ".gemini").mkdir()

    first_changed, _ = ci.apply_gemini_settings()
    assert first_changed is True
    second_changed, msg = ci.apply_gemini_settings()
    assert second_changed is False
    assert "no change" in msg
