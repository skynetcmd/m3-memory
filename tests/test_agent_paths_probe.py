"""doctor's cross-agent dead-path probe.

Relocating m3 breaks every path-based agent integration (Gemini/OpenCode/Hermes)
silently. This probe scans ALL wired hosts — including OpenCode's own schema and
Hermes's .pth, which the installer's mcpServers-only scan misses — and flags dead
paths (bumping the exit code). Report-only: healing lives in `m3 setup`.
"""
import json
import sys
from pathlib import Path

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from doctor import agent_paths_probe as P  # noqa: E402


def test_path_dead_distinguishes_kinds(tmp_path):
    dead = str(tmp_path / "gone.py")
    live = str(tmp_path / "here.py")
    (tmp_path / "here.py").write_text("x")
    assert P._path_dead(dead) is True
    assert P._path_dead(live) is False
    assert P._path_dead("m3") is False        # bare console script — no separator
    assert P._path_dead("python") is False
    assert P._path_dead(None) is False


def test_opencode_dead_list_command_is_flagged(tmp_path, monkeypatch):
    cfg = tmp_path / "opencode.json"
    dead = str(tmp_path / "old" / "python.exe")
    cfg.write_text(json.dumps({"mcp": {"memory": {"command": [dead, "bin/memory_bridge.py"]}}}))
    monkeypatch.setattr(P, "_opencode_configs", lambda: [cfg])
    rows = P._scan_opencode()
    assert rows == [("OpenCode", str(cfg), True)]


def test_opencode_bare_cli_is_healthy(tmp_path, monkeypatch):
    cfg = tmp_path / "opencode.json"
    cfg.write_text(json.dumps({"mcp": {"memory": {"command": ["m3"], "enabled": True}}}))
    monkeypatch.setattr(P, "_opencode_configs", lambda: [cfg])
    rows = P._scan_opencode()
    assert rows == [("OpenCode", str(cfg), False)]


def test_hermes_scan_flags_dead_pth(tmp_path, monkeypatch):
    # Build a fake hermes tree under a fake HOME so _scan_hermes finds the .pth,
    # and point it at a missing bin dir -> flagged dead.
    home = tmp_path / "home"
    site = home / "AppData" / "Local" / "hermes" / "venv" / "site-packages"
    site.mkdir(parents=True)
    (site / "m3-memory-bin.pth").write_text(str(tmp_path / "gone" / "bin") + "\n")
    monkeypatch.setattr(P.Path, "home", classmethod(lambda cls: home))
    rows = P._scan_hermes()
    assert rows and all(lbl == "Hermes" for lbl, _, _ in rows)
    assert any(dead for _, _, dead in rows)

    # Now repoint at a live dir -> healthy.
    live = tmp_path / "live-bin"
    live.mkdir()
    (site / "m3-memory-bin.pth").write_text(str(live) + "\n")
    rows2 = P._scan_hermes()
    assert rows2 and not any(dead for _, _, dead in rows2)


def test_all_healthy_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(P, "_scan_mcpservers_hosts",
                        lambda: [("Gemini CLI", "/g.json", False)])
    monkeypatch.setattr(P, "_scan_opencode", lambda: [("OpenCode", "/o.json", False)])
    monkeypatch.setattr(P, "_scan_hermes", lambda: [])
    assert P.run(brief=False) == 0
    assert "OK" in capsys.readouterr().out


def test_dead_path_bumps_exit_code_and_names_hosts(monkeypatch, capsys):
    monkeypatch.setattr(P, "_scan_mcpservers_hosts",
                        lambda: [("Gemini CLI", "/g.json", True)])
    monkeypatch.setattr(P, "_scan_opencode", lambda: [("OpenCode", "/o.json", True)])
    monkeypatch.setattr(P, "_scan_hermes", lambda: [])
    assert P.run(brief=False) == 1
    out = capsys.readouterr().out
    assert "DEAD" in out and "m3 setup" in out


def test_brief_glyphs(monkeypatch, capsys):
    monkeypatch.setattr(P, "_scan_mcpservers_hosts", lambda: [("Gemini CLI", "/g.json", False)])
    monkeypatch.setattr(P, "_scan_opencode", lambda: [])
    monkeypatch.setattr(P, "_scan_hermes", lambda: [])
    assert P.run(brief=True) == 0
    assert "✅" in capsys.readouterr().out

    monkeypatch.setattr(P, "_scan_mcpservers_hosts", lambda: [("Gemini CLI", "/g.json", True)])
    assert P.run(brief=True) == 1
    out = capsys.readouterr().out
    assert "⚠️" in out and "Gemini" in out


def test_no_wired_hosts_is_benign(monkeypatch, capsys):
    monkeypatch.setattr(P, "_scan_mcpservers_hosts", lambda: [])
    monkeypatch.setattr(P, "_scan_opencode", lambda: [])
    monkeypatch.setattr(P, "_scan_hermes", lambda: [])
    assert P.run(brief=False) == 0
    assert "nothing to check" in capsys.readouterr().out.lower()
