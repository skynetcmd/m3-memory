"""Tests for the split-brain agent-config self-heal (A+C+B).

Covers the historical bug where an already-present but STALE ``memory`` MCP
entry (dead bridge/root paths from a moved install) survived re-registration,
plus the new ``doctor --fix`` self-heal and the canonical-config single source
of truth.
"""
import json

import pytest

from m3_memory import installer as I


@pytest.fixture
def canonical(tmp_path, monkeypatch):
    """Point the installer at a fake live install under tmp_path so the canonical
    config is deterministic and bridge resolution succeeds."""
    repo = tmp_path / ".m3" / "repo"
    (repo / "bin").mkdir(parents=True)
    bridge = repo / "bin" / "memory_bridge.py"
    bridge.write_text("# fake bridge\n", encoding="utf-8")
    state = tmp_path / ".m3"
    (state / "engine").mkdir(parents=True, exist_ok=True)
    (state / "config").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("M3_MEMORY_ROOT", str(state))
    monkeypatch.setenv("M3_ENGINE_ROOT", str(state / "engine"))
    monkeypatch.setenv("M3_CONFIG_ROOT", str(state / "config"))
    # M3_PATH_BIN (a bin/ DIRECTORY) replaces the removed M3_BRIDGE_PATH file-var.
    # bin_dir() honors it (must_contain=memory_bridge.py), so find_bridge()
    # resolves to the fake bridge instead of the real dev-checkout bin.
    monkeypatch.setenv("M3_PATH_BIN", str(bridge.parent))
    monkeypatch.delenv("M3_BRIDGE_PATH", raising=False)
    monkeypatch.delenv("M3_EMBED_GGUF", raising=False)
    monkeypatch.setattr(I, "config_dir", lambda: state)
    return {"bridge": bridge, "state": state}


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def test_stale_memory_entry_is_repointed(canonical, tmp_path):
    """The core bug: a dead bridge path must be repointed, not skipped."""
    cfg = tmp_path / "host" / "settings.json"
    _write(cfg, {"mcpServers": {"memory": {
        "command": "/old/python",
        "args": ["/nonexistent/old-install/bin/memory_bridge.py"],
        "env": {"M3_BRIDGE_PATH": "/nonexistent/old-install/bin/memory_bridge.py"},
    }}})

    msg = I._heal_agent_settings(cfg)
    assert msg and "repointed" in msg

    after = json.loads(cfg.read_text())["mcpServers"]["memory"]
    assert after["args"] == [str(canonical["bridge"]).replace("\\", "/")]
    # M3_BRIDGE_PATH is removed project-wide; the canonical env now pins the
    # bin/ DIRECTORY via M3_PATH_BIN (non-packaged install -> pin is written).
    assert after["env"]["M3_PATH_BIN"] == str(canonical["bridge"].parent).replace("\\", "/")
    assert "M3_BRIDGE_PATH" not in after["env"]


def test_healthy_entry_is_left_untouched(canonical, tmp_path):
    cfg = tmp_path / "host" / "settings.json"
    _write(cfg, {"mcpServers": {"memory": I._canonical_memory_server()}})
    assert I._heal_agent_settings(cfg) is None  # no-op


def test_foreign_servers_and_keys_preserved(canonical, tmp_path):
    cfg = tmp_path / "host" / "settings.json"
    _write(cfg, {
        "mcpServers": {
            "memory": {"command": "x", "args": ["/dead/path.py"]},
            "other": {"command": "keep"},
        },
        "topLevel": {"untouched": True},
    })
    I._heal_agent_settings(cfg)
    d = json.loads(cfg.read_text())
    assert d["mcpServers"]["other"] == {"command": "keep"}
    assert d["topLevel"] == {"untouched": True}


def test_heal_is_idempotent(canonical, tmp_path):
    cfg = tmp_path / "host" / "settings.json"
    _write(cfg, {"mcpServers": {"memory": {"args": ["/dead.py"]}}})
    first = I._heal_agent_settings(cfg)
    assert first and "repointed" in first
    assert I._heal_agent_settings(cfg) is None  # second run no-op


def test_backup_written_before_repoint(canonical, tmp_path):
    cfg = tmp_path / "host" / "settings.json"
    _write(cfg, {"mcpServers": {"memory": {"args": ["/dead.py"]}}})
    I._heal_agent_settings(cfg)
    bak = cfg.with_suffix(cfg.suffix + ".m3bak")
    assert bak.is_file()
    assert "/dead.py" in bak.read_text()  # the prior (broken) config is preserved


def test_missing_memory_entry_is_added(canonical, tmp_path):
    cfg = tmp_path / "host" / "settings.json"
    _write(cfg, {"mcpServers": {"other": {"command": "keep"}}})
    msg = I._heal_agent_settings(cfg)
    assert msg and "registered" in msg
    d = json.loads(cfg.read_text())
    assert "memory" in d["mcpServers"]
    assert d["mcpServers"]["other"] == {"command": "keep"}


def test_unreadable_config_is_not_clobbered(canonical, tmp_path):
    cfg = tmp_path / "host" / "settings.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{ this is not json", encoding="utf-8")
    msg = I._heal_agent_settings(cfg)
    assert msg and "unreadable" in msg
    assert cfg.read_text() == "{ this is not json"  # untouched


def test_path_is_stale_ignores_bare_console_scripts():
    assert I._path_is_stale("mcp-memory") is False
    assert I._path_is_stale("python") is False
    assert I._path_is_stale("/definitely/not/here.py") is True


def test_scan_reports_dead_paths(canonical, tmp_path, monkeypatch):
    gemini = tmp_path / ".gemini" / "settings.json"
    _write(gemini, {"mcpServers": {"memory": {"args": ["/dead/bridge.py"]}}})
    monkeypatch.setattr(
        I, "_known_agent_settings", lambda: [("Gemini CLI", gemini)]
    )
    scanned = I._scan_agent_configs()
    assert scanned == [("Gemini CLI", gemini, True)]


def test_heal_all_agents_counts_changes(canonical, tmp_path, monkeypatch):
    g = tmp_path / ".gemini" / "settings.json"
    _write(g, {"mcpServers": {"memory": {"args": ["/dead.py"]}}})
    h = tmp_path / ".healthy" / "settings.json"
    _write(h, {"mcpServers": {"memory": I._canonical_memory_server()}})
    monkeypatch.setattr(
        I, "_known_agent_settings",
        lambda: [("Gemini CLI", g), ("Healthy", h)],
    )
    assert I._heal_all_agents() == 1  # only the broken one changes


def test_doctor_fix_flag_runs(canonical, tmp_path, monkeypatch, capsys):
    g = tmp_path / ".gemini" / "settings.json"
    _write(g, {"mcpServers": {"memory": {"args": ["/dead.py"]}}})
    monkeypatch.setattr(I, "_known_agent_settings", lambda: [("Gemini CLI", g)])
    I.doctor(fix=True)
    out = capsys.readouterr().out
    assert "repointed" in out
    after = json.loads(g.read_text())["mcpServers"]["memory"]
    assert after["args"] == [str(canonical["bridge"]).replace("\\", "/")]


# ── OpenCode self-heal (mcp.memory schema, list command) ──────────────────────
# OpenCode uses a DIFFERENT config shape than the mcpServers hosts:
# {"mcp": {"memory": {"command": [interp, script], ...}}}. Its wiring historically
# SKIPPED an already-present entry, so a dead-path entry survived a moved install.
# _wire_opencode now self-heals it. These test the OpenCode-specific logic.
from m3_memory import setup_wizard as W  # noqa: E402


def test_opencode_stale_detector_flags_dead_list_command(tmp_path):
    dead = str(tmp_path / "gone" / "python.exe")  # absolute path, does not exist
    entry = {"type": "local", "command": [dead, "bin/memory_bridge.py"]}
    assert W._opencode_entry_is_stale(entry) is True


def test_opencode_stale_detector_ignores_bare_cli(tmp_path):
    # command: ["m3"] is a bare console script — no path elements — never stale.
    assert W._opencode_entry_is_stale({"type": "local", "command": ["m3"]}) is False


def test_opencode_stale_detector_non_dict_is_stale():
    assert W._opencode_entry_is_stale(None) is True
    assert W._opencode_entry_is_stale("weird") is True


def test_wire_opencode_repoints_a_dead_entry(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "opencode" / "opencode.json"
    cfg.parent.mkdir(parents=True)
    dead = str(tmp_path / "old-repo" / ".venv" / "python.exe")
    _write(cfg, {"mcp": {"memory": {"type": "local",
                                    "command": [dead, "bin/memory_bridge.py"]}}})
    monkeypatch.setattr(W, "_opencode_config_paths", lambda: [cfg])
    monkeypatch.setattr(W, "_say", lambda m: None)
    monkeypatch.setattr(W, "_warn", lambda m: None)

    assert W._wire_opencode() is True
    after = json.loads(cfg.read_text())["mcp"]["memory"]
    assert after["command"] == ["m3"]              # repointed to the relocation-proof CLI
    assert cfg.with_suffix(".json.m3bak").is_file()  # backed up first


def test_wire_opencode_leaves_a_healthy_entry(tmp_path, monkeypatch):
    cfg = tmp_path / ".config" / "opencode" / "opencode.json"
    cfg.parent.mkdir(parents=True)
    _write(cfg, {"mcp": {"memory": {"type": "local", "command": ["m3"], "enabled": True}}})
    before = cfg.read_text()
    monkeypatch.setattr(W, "_opencode_config_paths", lambda: [cfg])
    monkeypatch.setattr(W, "_say", lambda m: None)
    monkeypatch.setattr(W, "_warn", lambda m: None)

    W._wire_opencode()
    assert cfg.read_text() == before  # no rewrite of a healthy entry
    assert not cfg.with_suffix(".json.m3bak").exists()
