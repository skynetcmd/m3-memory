"""Tests for m3 doctor's self-heal of STALE chatlog hooks in agent configs.

Background: agent configs (Gemini CLI, and any host using the AfterModel/
PreCompress/SessionEnd chatlog hooks) reference bin/hooks/chatlog/<name>.py by
ABSOLUTE path. When the install moves (payload relocation / dev-tree rename),
those paths go dead and chatlog capture silently breaks — but the old
`_heal_agent_settings` only repointed the `memory` MCP entry, so `doctor --fix`
and upgrades left the hooks broken. `_repoint_stale_chatlog_hooks` closes that.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from m3_memory import installer as I  # noqa: E402


def _mk_bridge(tmp_path: Path) -> Path:
    """Create a fake live bridge dir with a real chatlog hook script, so the
    repoint target exists (the fix only rewrites toward a live path)."""
    bin_dir = tmp_path / "payload" / "bin"
    chatlog = bin_dir / "hooks" / "chatlog"
    chatlog.mkdir(parents=True)
    (chatlog / "gemini_cli_onexit.py").write_text("# hook", encoding="utf-8")
    return bin_dir / "memory_bridge.py"


def test_stale_hook_is_repointed(tmp_path, monkeypatch):
    bridge = _mk_bridge(tmp_path)
    monkeypatch.setattr(I, "_canonical_bridge_path", lambda: bridge)

    data = {
        "hooks": {
            "SessionEnd": [{"hooks": [{"type": "command",
                "command": "python C:/dead/old/bin/hooks/chatlog/gemini_cli_onexit.py"}]}],
        }
    }
    fixed = I._repoint_stale_chatlog_hooks(data)
    assert fixed == 1
    cmd = data["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
    assert "gemini_cli_onexit.py" in cmd
    assert str(bridge.parent).replace("\\", "/") in cmd.replace("\\", "/")
    # interpreter token preserved
    assert cmd.startswith("python ")


def test_live_hook_is_left_untouched(tmp_path, monkeypatch):
    """A hook already pointing at an EXISTING script must not be rewritten
    (idempotent — no spurious repoint / backup churn)."""
    bridge = _mk_bridge(tmp_path)
    monkeypatch.setattr(I, "_canonical_bridge_path", lambda: bridge)
    live = str(bridge.parent / "hooks" / "chatlog" / "gemini_cli_onexit.py").replace("\\", "/")

    data = {"hooks": {"SessionEnd": [{"hooks": [{"command": f"python {live}"}]}]}}
    assert I._repoint_stale_chatlog_hooks(data) == 0


def test_multiple_stale_hooks_all_fixed(tmp_path, monkeypatch):
    bridge = _mk_bridge(tmp_path)
    monkeypatch.setattr(I, "_canonical_bridge_path", lambda: bridge)
    dead = "python C:/dead/bin/hooks/chatlog/gemini_cli_onexit.py"
    data = {"hooks": {
        "SessionEnd": [{"hooks": [{"command": dead}]}],
        "AfterModel": [{"hooks": [{"command": dead}]}],
        "PreCompress": [{"hooks": [{"command": dead}]}],
    }}
    assert I._repoint_stale_chatlog_hooks(data) == 3


def test_no_hooks_section_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "_canonical_bridge_path", lambda: _mk_bridge(tmp_path))
    assert I._repoint_stale_chatlog_hooks({"mcpServers": {}}) == 0
    assert I._repoint_stale_chatlog_hooks({"hooks": "not-a-dict"}) == 0


def test_no_bridge_resolvable_is_noop(monkeypatch):
    """If no live bridge can be found (system not installed), don't touch hooks."""
    monkeypatch.setattr(I, "_canonical_bridge_path", lambda: None)
    data = {"hooks": {"SessionEnd": [{"hooks": [{"command": "python /dead/x.py"}]}]}}
    assert I._repoint_stale_chatlog_hooks(data) == 0


def test_non_chatlog_hook_ignored(tmp_path, monkeypatch):
    """A non-chatlog hook command must be left alone (we only own chatlog hooks)."""
    bridge = _mk_bridge(tmp_path)
    monkeypatch.setattr(I, "_canonical_bridge_path", lambda: bridge)
    data = {"hooks": {"SessionEnd": [{"hooks": [{"command": "python C:/other/tool.py"}]}]}}
    assert I._repoint_stale_chatlog_hooks(data) == 0


def test_heal_fixes_hooks_even_when_mcp_entry_healthy(tmp_path, monkeypatch):
    """The load-bearing case: the `memory` MCP entry is HEALTHY but the hooks are
    stale. The old heal returned early (MCP healthy) and left hooks broken; the
    new heal must still repoint the hooks."""
    bridge = _mk_bridge(tmp_path)
    monkeypatch.setattr(I, "_canonical_bridge_path", lambda: bridge)
    # Make the MCP entry already-canonical so it needs no repoint.
    canonical = I._canonical_memory_server()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "mcpServers": {"memory": canonical},
        "hooks": {"SessionEnd": [{"hooks": [{
            "command": "python C:/dead/bin/hooks/chatlog/gemini_cli_onexit.py"}]}]},
    }), encoding="utf-8")

    result = I._heal_agent_settings(settings)
    assert result is not None and "chatlog hook" in result
    healed = json.loads(settings.read_text())
    cmd = healed["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
    assert str(bridge.parent).replace("\\", "/") in cmd.replace("\\", "/")
