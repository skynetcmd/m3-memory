"""Claude single-server hardening: exactly ONE m3 memory server runs.

Two m3 servers can end up live at once — the direct `mcp__m3_memory__` server
(from `claude mcp add`) AND the plugin's `mcp__plugin_m3_memory__` server. `m3
setup` disables the plugin's server so only the direct one runs, but a LATER
`/plugin install m3` re-enables it and nothing else notices. This suite pins:

  * claude_mcp_probe DETECTS the double-run (and the legacy roots-less `memory`
    entry / two-direct-servers), report-only + exit-code-neutral.
  * its --fix path (behind `m3 doctor --fix --fix-hooks`) converges to one server:
    disables the plugin server only when a direct server exists to take over, and
    never removes the user's only working server.
  * setup_wizard._disable_claude_plugin_server is the guaranteed floor — the
    direct server is never registered without the plugin's server disabled in the
    same setup, even if the canonical settings writer no-ops/fails.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── ownership test ──────────────────────────────────────────────────────────

def test_looks_m3_distinguishes_m3_from_foreign():
    from doctor import claude_mcp_probe as p
    assert p._looks_m3({"command": "m3", "args": [], "env": {}}) is True
    assert p._looks_m3({"command": "/opt/bin/m3", "args": []}) is True
    assert p._looks_m3({"command": "python", "args": ["x/memory_bridge.py"]}) is True
    assert p._looks_m3({"command": "python", "env": {"M3_ENGINE_ROOT": "/e"}}) is True
    # A foreign server that merely shares the name `memory` is NOT m3's.
    assert p._looks_m3({"command": "some-other-tool", "args": []}) is False
    assert p._looks_m3(None) is False


def test_has_root_pins_treats_empty_string_as_unset():
    from doctor import claude_mcp_probe as p
    assert p._has_root_pins({"env": {"M3_ENGINE_ROOT": "/e"}}) is True
    assert p._has_root_pins({"env": {"M3_ENGINE_ROOT": ""}}) is False  # empty == unset
    assert p._has_root_pins({"env": {}}) is False


# ── assessment ──────────────────────────────────────────────────────────────

def _plugin(installed=False, enabled=False, server_disabled=False):
    return {"installed": installed, "enabled": enabled,
            "server_disabled": server_disabled,
            "active": installed and enabled and not server_disabled}


def test_assess_flags_double_run(monkeypatch):
    from doctor import claude_mcp_probe as p
    monkeypatch.setattr(p, "_direct_servers",
                        lambda: {"m3_memory": {"command": "m3", "env": {"M3_ENGINE_ROOT": "/e"}}})
    monkeypatch.setattr(p, "_plugin_state", lambda: _plugin(installed=True, enabled=True))
    a = p._assess()
    assert a["double_run"] is True
    assert a["direct_present"] is True
    assert a["problem"] is True


def test_assess_single_direct_is_clean(monkeypatch):
    from doctor import claude_mcp_probe as p
    monkeypatch.setattr(p, "_direct_servers",
                        lambda: {"m3_memory": {"command": "m3", "env": {"M3_ENGINE_ROOT": "/e"}}})
    # Plugin installed but its server disabled -> not active -> single server.
    monkeypatch.setattr(p, "_plugin_state",
                        lambda: _plugin(installed=True, enabled=True, server_disabled=True))
    a = p._assess()
    assert a["double_run"] is False
    assert a["problem"] is False


def test_assess_flags_legacy_rootless_and_two_direct(monkeypatch):
    from doctor import claude_mcp_probe as p
    monkeypatch.setattr(p, "_direct_servers", lambda: {
        "m3_memory": {"command": "m3", "env": {"M3_ENGINE_ROOT": "/e"}},
        "memory": {"command": "m3", "env": {}},  # legacy, roots-less
    })
    monkeypatch.setattr(p, "_plugin_state", lambda: _plugin())
    a = p._assess()
    assert a["two_direct"] is True
    assert a["legacy_rootless"] is True
    assert a["problem"] is True


def test_assess_plugin_only_is_supported(monkeypatch):
    from doctor import claude_mcp_probe as p
    monkeypatch.setattr(p, "_direct_servers", lambda: {})
    monkeypatch.setattr(p, "_plugin_state", lambda: _plugin(installed=True, enabled=True))
    a = p._assess()
    assert a["double_run"] is False   # no direct server -> no double-run
    assert a["problem"] is False


# ── fix: disable plugin server in settings.json ─────────────────────────────

def test_disable_plugin_server_merges_and_backs_up(monkeypatch):
    from doctor import claude_mcp_probe as p
    d = tempfile.mkdtemp()
    monkeypatch.setattr(p, "_claude_dir", lambda: d)
    sp = os.path.join(d, "settings.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump({"disabledMcpServers": ["user:already-off"]}, f)

    changed, backup, _ = p._disable_plugin_server_in_settings()
    assert changed is True
    assert backup and os.path.isfile(backup)                 # backed up first
    dis = _load(sp)["disabledMcpServers"]
    assert "plugin:m3:memory" in dis and "plugin:m3:m3" in dis
    assert "user:already-off" in dis                         # user disable preserved

    # Idempotent: a second call writes nothing new.
    changed2, backup2, _ = p._disable_plugin_server_in_settings()
    assert changed2 is False and backup2 is None
    assert _load(sp)["disabledMcpServers"].count("plugin:m3:memory") == 1


def test_fix_never_leaves_zero_servers(monkeypatch, capsys):
    """Legacy `memory` is the ONLY m3 server (no direct, no active plugin) — _fix
    must NOT remove it (that would leave zero servers); it nags `m3 setup`."""
    from doctor import claude_mcp_probe as p
    removed = []
    monkeypatch.setattr(p, "_claude_mcp_remove",
                        lambda name: (removed.append(name), (True, "done"))[1])
    a = {
        "direct": {}, "direct_present": False, "legacy_present": True,
        "plugin": _plugin(), "direct_active": True,
        "double_run": False, "two_direct": False, "legacy_rootless": True,
        "problem": True, "none": False,
    }
    p._fix(a)
    assert removed == []                                     # nothing removed
    assert "m3 setup" in capsys.readouterr().out


def test_fix_disables_plugin_when_direct_present(monkeypatch, capsys):
    from doctor import claude_mcp_probe as p
    calls = []
    monkeypatch.setattr(p, "_disable_plugin_server_in_settings",
                        lambda: (calls.append("disable"), (True, "/bak", "disabled x"))[1])
    monkeypatch.setattr(p, "_claude_mcp_remove", lambda name: (True, "done"))
    a = {
        "direct": {"m3_memory": {}}, "direct_present": True, "legacy_present": False,
        "plugin": _plugin(installed=True, enabled=True), "direct_active": True,
        "double_run": True, "two_direct": False, "legacy_rootless": False,
        "problem": True, "none": False,
    }
    p._fix(a)
    assert "disable" in calls


# ── report-only never bumps exit code ───────────────────────────────────────

def test_run_report_is_exit_code_neutral(monkeypatch):
    from doctor import claude_mcp_probe as p
    monkeypatch.setattr(p, "_direct_servers",
                        lambda: {"memory": {"command": "m3", "env": {}}})
    monkeypatch.setattr(p, "_plugin_state", lambda: _plugin(installed=True, enabled=True))
    assert p.run(brief=False) == 0    # double-run present, but never fails doctor
    assert p.run(brief=True) == 0


# ── setup-side guaranteed disable ───────────────────────────────────────────

def test_setup_disable_plugin_server_is_guaranteed(monkeypatch):
    from m3_memory import setup_wizard as sw
    home = tempfile.mkdtemp()
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)  # Windows expanduser
    cfg = os.path.join(home, ".claude")
    os.makedirs(cfg)
    sp = os.path.join(cfg, "settings.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump({"disabledMcpServers": ["user:keep"]}, f)

    sw._disable_claude_plugin_server()
    dis = _load(sp)["disabledMcpServers"]
    assert "plugin:m3:memory" in dis and "plugin:m3:m3" in dis
    assert "user:keep" in dis                                # user disable preserved

    # Idempotent.
    sw._disable_claude_plugin_server()
    assert _load(sp)["disabledMcpServers"].count("plugin:m3:memory") == 1


def test_setup_disable_creates_settings_when_absent(monkeypatch):
    from m3_memory import setup_wizard as sw
    home = tempfile.mkdtemp()
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    sw._disable_claude_plugin_server()   # no ~/.claude yet
    sp = os.path.join(home, ".claude", "settings.json")
    assert os.path.isfile(sp)
    assert "plugin:m3:memory" in _load(sp)["disabledMcpServers"]


# ── doctor CLI wiring ───────────────────────────────────────────────────────

def test_doctor_exposes_skip_claude_mcp_flag():
    import importlib
    mod = importlib.import_module("memory_doctor")
    src = mod.__file__
    with open(src, encoding="utf-8") as f:
        text = f.read()
    assert "--skip-claude-mcp" in text
    assert "claude_mcp_probe" in text
