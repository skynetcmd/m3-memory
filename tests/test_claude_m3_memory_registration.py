"""Claude MCP registration = the direct `m3_memory` server, plugin kept off.

The design (best-of-both): Claude Code does NOT read MCP server definitions from
settings.json, so the memory server is registered via `claude mcp add m3_memory`
(clean `mcp__m3_memory__`, silent/scriptable, roots pinned). The m3 plugin stays
as the marketplace-discovery option, but its own memory server is kept DISABLED so
a user never runs two. This suite pins that behavior:

  * install_claude_settings PRUNES the dead m3-owned settings.json mcpServers
    (Claude ignores them) and never re-adds them, while preserving foreign servers.
  * it merges `plugin:m3:memory` (+ the pre-rename `plugin:m3:m3`) into
    disabledMcpServers without clobbering the user's own disables.
  * _wire_claude registers `m3_memory` (not `memory`), removes the legacy `memory`
    entry (migration), refreshes root pins (remove-then-add), and prints the loud
    "direct server, plugin disabled" notice.
  * the plugin manifest declares its server under key `memory`
    (-> mcp__plugin_m3_memory__), keeps the "m3" display name.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


# ── generate_configs.install_claude_settings ────────────────────────────────

def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_prunes_m3_mcpservers_and_disables_plugin(monkeypatch):
    import generate_configs as gc

    d = tempfile.mkdtemp()
    sp = os.path.join(d, "settings.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump({
            "mcpServers": {
                "memory": {"command": "python", "args": ["memory_bridge.py"]},
                "web_research": {"command": "python", "args": ["web_research_bridge.py"]},
                "someones_tool": {"command": "othertool"},
            },
            "disabledMcpServers": ["user:already-off"],
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/usr/bin/user_hook"}]}]},
        }, f)

    res = gc.install_claude_settings(settings_path=sp, assume_yes=True)
    assert res["changed"] is True
    out = _load(sp)

    mcp = out.get("mcpServers", {})
    assert "memory" not in mcp and "web_research" not in mcp   # m3-owned pruned
    assert mcp.get("someones_tool")                            # foreign preserved

    dis = out["disabledMcpServers"]
    assert "plugin:m3:memory" in dis and "plugin:m3:m3" in dis  # plugin off (both keys)
    assert "user:already-off" in dis                           # user disable kept

    assert any("user_hook" in json.dumps(e) for e in out["hooks"]["Stop"])  # hook kept


def test_disabled_list_is_idempotent(monkeypatch):
    import generate_configs as gc

    d = tempfile.mkdtemp()
    sp = os.path.join(d, "settings.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump({}, f)
    gc.install_claude_settings(settings_path=sp, assume_yes=True)
    first = _load(sp)["disabledMcpServers"]
    gc.install_claude_settings(settings_path=sp, assume_yes=True)
    second = _load(sp)["disabledMcpServers"]
    # No duplicate plugin ids on a second run.
    assert second.count("plugin:m3:memory") == 1
    assert first == second


# ── plugin manifest ─────────────────────────────────────────────────────────

def test_plugin_declares_memory_server_keeps_m3_display():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    manifest = _load(os.path.join(repo, ".claude-plugin", "plugin.json"))
    # Server key `memory` -> mcp__plugin_m3_memory__ (cleanest plugin namespace).
    assert list(manifest["mcpServers"].keys()) == ["memory"]
    # User-facing branding stays "m3".
    assert manifest["name"] == "m3"


# ── _wire_claude registration ───────────────────────────────────────────────

def test_wire_claude_registers_m3_memory_and_migrates_old(monkeypatch):
    """_wire_claude must `claude mcp add m3_memory` (not `memory`), remove the
    legacy `memory` entry, and remove-then-add m3_memory to refresh root pins."""
    from types import SimpleNamespace

    from m3_memory import setup_wizard as sw

    monkeypatch.setattr(sw.shutil, "which", lambda name: "/usr/bin/claude")
    # Skip the real hook subprocess.
    monkeypatch.setattr(sw, "_wire_claude_hooks", lambda mode: True)
    # Deterministic roots.
    import m3_memory.installer as inst
    monkeypatch.setattr(inst, "_canonical_memory_env",
                        lambda: {"M3_ENGINE_ROOT": "/data/e", "M3_CONFIG_ROOT": "/data/c"})

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(sw.subprocess, "run", fake_run)
    assert sw._wire_claude("both") is True

    joined = [" ".join(c) for c in calls]
    # Legacy `memory` removed (migration) + m3_memory removed (refresh) + added.
    assert any("mcp remove --scope user memory" in j for j in joined)
    assert any("mcp remove --scope user m3_memory" in j for j in joined)
    add = [c for c in calls if "add" in c][0]
    assert add[-2:] == ["m3_memory", "m3"]                 # name m3_memory, cmd m3
    assert "--env" in add and "M3_ENGINE_ROOT=/data/e" in add and "M3_CONFIG_ROOT=/data/c" in add
    # Never registers the old bare `memory` name.
    assert not any(c[-2:] == ["memory", "m3"] for c in calls if "add" in c)


def test_wire_claude_prints_loud_plugin_default_notice(monkeypatch, capsys):
    from types import SimpleNamespace

    from m3_memory import setup_wizard as sw

    monkeypatch.setattr(sw.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(sw, "_wire_claude_hooks", lambda mode: True)
    import m3_memory.installer as inst
    monkeypatch.setattr(inst, "_canonical_memory_env", lambda: {})
    monkeypatch.setattr(sw.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    sw._wire_claude("both")
    out = capsys.readouterr().out
    assert "mcp__m3_memory__" in out
    assert "plugin:m3:memory" in out and "disabledMcpServers" in out
