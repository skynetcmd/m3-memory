"""Hook wiring must be verified against disk, not trusted from config.

The chatlog hooks bake absolute paths into the pipx venv -- interpreter AND
script -- and every reinstall churns that directory. A stale path makes the hook
fail per-turn with no surfaced error: conversation capture silently dies. That is
the 48-hour context-loss failure in CLAUDE.md, and no probe covered it
(agent_paths_probe reads MCP *server* configs, never the `hooks` block).
"""

import os
import sys

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)


@pytest.fixture
def probe():
    from doctor import environment_probe
    return environment_probe


def _settings(commands):
    """commands: {event: command_string}"""
    return {
        "hooks": {
            evt: [{"hooks": [{"type": "command", "command": cmd}]}]
            for evt, cmd in commands.items()
        }
    }


def _pin(path, script):
    return (
        'set "M3_ENGINE_ROOT=C:/roots/engine" && '
        'set "M3_CONFIG_ROOT=C:/roots/config" && '
        f"{path} {script}"
    )


@pytest.fixture
def wire(probe, monkeypatch, tmp_path):
    """Install a fake settings.json + a payload root under tmp_path."""
    def _apply(commands, payload=None):
        monkeypatch.setattr(probe, "_load_settings", lambda: _settings(commands))
        monkeypatch.setattr(probe, "_payload_root",
                            lambda: payload or str(tmp_path / "venv" / "Lib"
                                                   / "site-packages" / "m3_memory"))
    return _apply


def _make(tmp_path, rel):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    return str(p).replace("\\", "/")


def test_healthy_wiring_reports_ok(probe, wire, tmp_path):
    py = _make(tmp_path, "venv/Scripts/python.exe")
    sc = _make(tmp_path, "venv/Lib/site-packages/m3_memory/bin/hooks/chatlog/cc.py")
    wire({"PreCompact": _pin(py, sc), "Stop": _pin(py, sc)})

    res = probe.check()
    assert res["status"] == "ok", res["findings"]
    assert res["hooks_seen"] == 2


def test_dead_script_path_is_caught(probe, wire, tmp_path):
    """The exact reinstall failure: the script the hook points at is gone."""
    py = _make(tmp_path, "venv/Scripts/python.exe")
    gone = str(tmp_path / "venv/Lib/site-packages/m3_memory/bin/hooks/chatlog/gone.py")
    wire({"PreCompact": _pin(py, gone.replace("\\", "/"))})

    res = probe.check()
    assert res["status"] == "issues"
    assert any(f["kind"] == "dead_path" for f in res["findings"]), res["findings"]
    assert probe.run(brief=True) == 1, "dead capture must fail the doctor"


def test_foreign_interpreter_is_caught(probe, wire, tmp_path):
    """A hook pinned to a DIFFERENT venv runs different m3 code than `m3` does."""
    other = _make(tmp_path, "other-venv/Scripts/python.exe")
    sc = _make(tmp_path, "venv/Lib/site-packages/m3_memory/bin/hooks/chatlog/cc.py")
    wire({"PreCompact": _pin(other, sc)})

    res = probe.check()
    assert any(f["kind"] == "foreign_interpreter" for f in res["findings"]), res["findings"]


def test_missing_root_pins_are_caught(probe, wire, tmp_path, monkeypatch):
    """CLAUDE.md split-brain: hooks reload live, server env only on restart.

    POSIX only — the inline `VAR=val` prefix is POSIX shell syntax. See
    test_posix_prefix_on_windows_is_the_finding for the inverted Windows rule.
    """
    monkeypatch.setattr(probe.os, "name", "posix")
    py = _make(tmp_path, "venv/Scripts/python.exe")
    sc = _make(tmp_path, "venv/Lib/site-packages/m3_memory/bin/hooks/chatlog/cc.py")
    wire({"PreCompact": f"{py} {sc}"})  # no set M3_ENGINE_ROOT/M3_CONFIG_ROOT

    res = probe.check()
    hits = [f for f in res["findings"] if f["kind"] == "unpinned_roots"]
    assert hits, res["findings"]
    assert "M3_ENGINE_ROOT" in hits[0]["detail"]


def test_unpinned_roots_is_not_a_finding_on_windows(probe, wire, tmp_path, monkeypatch):
    """A Windows hook WITHOUT the pins is correct, and must not be flagged.

    cmd.exe cannot parse `VAR=val cmd`, so the config writers deliberately emit
    no prefix there. Reporting "CAPTURE MAY BE DEAD" would send the user to
    `--fix-hooks` to reinstate the very thing that breaks capture — the probe
    causing the outage it exists to detect (§5).
    """
    monkeypatch.setattr(probe.os, "name", "nt")
    py = _make(tmp_path, "venv/Scripts/python.exe")
    sc = _make(tmp_path, "venv/Lib/site-packages/m3_memory/bin/hooks/chatlog/cc.py")
    wire({"PreCompact": f"{py} {sc}"})

    res = probe.check()
    assert not [f for f in res["findings"] if f["kind"] == "unpinned_roots"], res["findings"]


def test_posix_prefix_on_windows_is_the_finding(probe, wire, tmp_path, monkeypatch):
    """On Windows the FATAL shape is a POSIX prefix PRESENT, not absent.

    Reproduces the 2026-08-09..11 outage: cmd.exe tries to execute a program
    named `M3_ENGINE_ROOT=...`, the hook dies before it starts, and capture is
    silently dead with only a stalled last_write_at as evidence. This is the
    check that would have caught it on day one.
    """
    monkeypatch.setattr(probe.os, "name", "nt")
    py = _make(tmp_path, "venv/Scripts/python.exe")
    sc = _make(tmp_path, "venv/Lib/site-packages/m3_memory/bin/hooks/chatlog/cc.py")
    wire({"PreCompact": f"M3_ENGINE_ROOT=C:/x/engine M3_CONFIG_ROOT=C:/x/config {py} {sc}"})

    res = probe.check()
    hits = [f for f in res["findings"] if f["kind"] == "posix_prefix_on_windows"]
    assert hits, res["findings"]
    assert "cmd.exe cannot parse" in hits[0]["detail"]


def test_absent_settings_reports_unknown_not_ok(probe, monkeypatch, capsys):
    """Nothing was verified. Reporting OK here is the exact lie this probe exists
    to catch."""
    monkeypatch.setattr(probe, "_load_settings", lambda: None)
    assert probe.check()["status"] == "unknown"
    assert probe.run(brief=True) == 0
    assert "SKIPPED" in capsys.readouterr().out


def test_user_hooks_are_not_claimed(probe, wire, tmp_path):
    """Only entries carrying the ownership marker are m3's. A user's own hook in
    the same file must be invisible to detection -- and untouched by repair."""
    wire({"PreToolUse": "C:/user/tools/my_own_hook.py"})
    assert probe.check()["hooks_seen"] == 0


def test_repair_delegates_to_install_claude_settings(probe, tmp_path, monkeypatch):
    """The non-dry-run repair writes via the SINGLE canonical writer
    generate_configs.install_claude_settings (pinned .py hooks + roots + mcpServers)
    — NOT the old chatlog_init.apply_claude_settings (/bin/sh, add-only). This is
    what makes `m3 doctor --fix --fix-hooks` use one writer / one format."""
    monkeypatch.setattr(probe, "check", lambda: {"findings": [{"kind": "dead_path"}]})
    sp = tmp_path / "settings.json"
    sp.write_text("{}")
    monkeypatch.setattr(probe, "_claude_settings_path", lambda: str(sp))

    called = {"install": 0}
    import generate_configs
    monkeypatch.setattr(
        generate_configs, "install_claude_settings",
        lambda **k: called.__setitem__("install", called["install"] + 1) or {"changed": True},
    )
    import chatlog_init

    def _boom(*a, **k):
        raise AssertionError("repair must not call apply_claude_settings anymore")

    monkeypatch.setattr(chatlog_init, "apply_claude_settings", _boom)

    res = probe.repair(dry_run=False)
    assert called["install"] == 1
    assert any("install_claude_settings" in a.get("detail", "") for a in res["actions"])


def test_repair_is_dry_run_by_default(probe, wire, tmp_path):
    py = _make(tmp_path, "venv/Scripts/python.exe")
    gone = str(tmp_path / "nope.py").replace("\\", "/")
    wire({"PreCompact": _pin(py, gone)})

    res = probe.repair()
    assert res["dry_run"] is True
    assert res["backup"] is None, "dry run must not write a backup"
    assert all(a["status"] == "skipped" for a in res["actions"])


def test_probe_never_raises(probe, monkeypatch):
    def boom():
        raise RuntimeError("settings exploded")
    monkeypatch.setattr(probe, "_load_settings", boom)
    assert probe.run(brief=True) == 0


def test_repo_checkout_does_not_flag_installed_hooks(probe, wire, tmp_path):
    """Running the doctor FROM SOURCE must not flag correctly-wired hooks.

    From a repo checkout the payload root has no site-packages, and the hooks
    legitimately point at the installed venv -- a different tree by design. An
    unconditional same-install comparison reported foreign_interpreter for every
    hook on every dev machine (caught on the real box, not in fixtures).
    """
    py = _make(tmp_path, "pipx/venvs/m3-memory/Scripts/python.exe")
    sc = _make(tmp_path, "pipx/venvs/m3-memory/Lib/site-packages/m3_memory/"
                         "bin/hooks/chatlog/cc.py")
    # payload root = a REPO CHECKOUT, not an installed layout
    wire({"PreCompact": _pin(py, sc), "Stop": _pin(py, sc)},
         payload=str(tmp_path / "src" / "m3-memory"))

    res = probe.check()
    assert res["status"] == "ok", res["findings"]


def test_installed_layout_still_catches_a_foreign_venv(probe, wire, tmp_path):
    """The skip above must not disable the check for real installs."""
    other = _make(tmp_path, "pipx/venvs/OTHER/Scripts/python.exe")
    sc = _make(tmp_path, "pipx/venvs/m3-memory/Lib/site-packages/m3_memory/"
                         "bin/hooks/chatlog/cc.py")
    wire({"PreCompact": _pin(other, sc)},
         payload=str(tmp_path / "pipx/venvs/m3-memory/Lib/site-packages/m3_memory"))

    res = probe.check()
    assert any(f["kind"] == "foreign_interpreter" for f in res["findings"]), res["findings"]
