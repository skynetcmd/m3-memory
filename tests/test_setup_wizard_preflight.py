"""Tests for B15 setup-wizard preflight (m3_memory.setup_wizard).

Covers the 4 helpers that ship in the preflight step:

  1. _discover_bge_m3_gguf       — GGUF cascade walk
  2. _find_running_mcp_memory_processes — Windows tasklist parser
  3. _kill_process_windows       — taskkill wrapper (skipped on non-Win)
  4. shadowing detection (lives inside _step_preflight, tested via
     direct import + path comparison rather than full step invocation)

These tests are deliberately filesystem-isolated (tmp_path) and never
spawn real processes. The tasklist parser test feeds canned output via
a monkey-patched subprocess.run.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from m3_memory import setup_wizard

# ────────────────────────────────────────────────────────────────────────
# _discover_bge_m3_gguf
# ────────────────────────────────────────────────────────────────────────


def test_discover_returns_none_when_no_dirs_have_gguf(monkeypatch, tmp_path):
    """No candidate dir contains a BGE-M3 file → returns None (not raises)."""
    fake_home = tmp_path / "empty_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert setup_wizard._discover_bge_m3_gguf() is None


def test_discover_finds_gguf_in_lmstudio_dir(monkeypatch, tmp_path):
    """A bge-m3 *.gguf under ~/.lmstudio/models is discovered."""
    home = tmp_path / "home"
    lmstudio = home / ".lmstudio" / "models" / "deepsweet" / "bge-m3-GGUF-Q4_K_M"
    lmstudio.mkdir(parents=True)
    target = lmstudio / "bge-m3-GGUF-Q4_K_M.gguf"
    target.write_bytes(b"fake gguf header")
    monkeypatch.setattr(Path, "home", lambda: home)
    found = setup_wizard._discover_bge_m3_gguf()
    assert found is not None
    assert Path(found) == target


def test_discover_is_case_insensitive(monkeypatch, tmp_path):
    """Matches BGE-M3, bge-m3, Bge-M3 (case-insensitive substring check)."""
    home = tmp_path / "home"
    d = home / ".lmstudio" / "models"
    d.mkdir(parents=True)
    target = d / "BGE-M3-q8_0.gguf"  # caps in name
    target.write_bytes(b"fake")
    monkeypatch.setattr(Path, "home", lambda: home)
    found = setup_wizard._discover_bge_m3_gguf()
    assert found is not None
    assert Path(found).name == "BGE-M3-q8_0.gguf"


def test_discover_ignores_unrelated_gguf(monkeypatch, tmp_path):
    """A non-BGE-M3 GGUF in the cache dirs is NOT returned."""
    home = tmp_path / "home"
    d = home / ".lmstudio" / "models"
    d.mkdir(parents=True)
    (d / "llama-3-8b.gguf").write_bytes(b"fake")
    (d / "qwen-7b.gguf").write_bytes(b"fake")
    monkeypatch.setattr(Path, "home", lambda: home)
    assert setup_wizard._discover_bge_m3_gguf() is None


def test_discover_priority_lmstudio_wins_over_models_dir(monkeypatch, tmp_path):
    """When both ~/.lmstudio/models AND ~/models have a BGE-M3, the
    LM Studio cache wins (it's earlier in the priority list)."""
    home = tmp_path / "home"
    lmstudio = home / ".lmstudio" / "models"
    lmstudio.mkdir(parents=True)
    plain_models = home / "models"
    plain_models.mkdir()

    lmstudio_target = lmstudio / "bge-m3-lmstudio.gguf"
    lmstudio_target.write_bytes(b"a")
    plain_target = plain_models / "bge-m3-plain.gguf"
    plain_target.write_bytes(b"b")

    monkeypatch.setattr(Path, "home", lambda: home)
    found = setup_wizard._discover_bge_m3_gguf()
    assert found is not None
    assert "lmstudio" in found.lower(), (
        f"expected LM Studio path to win priority, got: {found}"
    )


# ────────────────────────────────────────────────────────────────────────
# _find_running_mcp_memory_processes
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="tasklist-based scan is Windows-only by design",
)
def test_find_running_returns_empty_list_on_no_matches(monkeypatch):
    """tasklist returns 'INFO: No tasks…' when nothing matches the filter."""
    fake_result = mock.Mock()
    fake_result.stdout = "INFO: No tasks are running which match the specified criteria.\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_result)
    assert setup_wizard._find_running_mcp_memory_processes() == []


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="tasklist-based scan is Windows-only by design",
)
def test_find_running_parses_csv_pids(monkeypatch):
    """CSV-format tasklist output is parsed into a list of integer PIDs."""
    fake_result = mock.Mock()
    fake_result.stdout = (
        '"mcp-memory.exe","12345","Console","1","123,456 K"\n'
        '"mcp-memory.exe","67890","Console","1","98,765 K"\n'
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_result)
    pids = setup_wizard._find_running_mcp_memory_processes()
    assert pids == [12345, 67890]


def test_find_running_returns_empty_on_non_windows(monkeypatch):
    """On Unix the scan short-circuits to an empty list (rename-in-place
    means a running binary doesn't block reinstall — no scan needed)."""
    monkeypatch.setattr(sys, "platform", "linux")
    assert setup_wizard._find_running_mcp_memory_processes() == []


def test_find_running_handles_subprocess_error(monkeypatch):
    """Tasklist crash or timeout returns [], doesn't raise."""
    if sys.platform != "win32":
        pytest.skip("error-handling path only fires when tasklist would be invoked")

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("tasklist", 10)

    monkeypatch.setattr(subprocess, "run", boom)
    # Should NOT raise
    assert setup_wizard._find_running_mcp_memory_processes() == []


# ────────────────────────────────────────────────────────────────────────
# _kill_process_windows
# ────────────────────────────────────────────────────────────────────────


def test_kill_process_calls_taskkill(monkeypatch):
    """Smoke: kill helper shells out to taskkill /F /PID and reports success."""
    calls: list = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return mock.Mock(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok = setup_wizard._kill_process_windows(99999)
    assert ok is True
    assert len(calls) == 1
    assert "taskkill" in calls[0]
    assert "99999" in calls[0]
    assert "/F" in calls[0]


def test_kill_process_returns_false_on_failure(monkeypatch):
    """Subprocess crash → False (caller can decide whether to retry)."""

    def boom(*a, **kw):
        raise OSError("simulated taskkill failure")

    monkeypatch.setattr(subprocess, "run", boom)
    assert setup_wizard._kill_process_windows(12345) is False


# ────────────────────────────────────────────────────────────────────────
# SetupPlan contract
# ────────────────────────────────────────────────────────────────────────


def test_setup_plan_has_embed_gguf_field():
    """B15 adds SetupPlan.embed_gguf for downstream wiring."""
    import dataclasses
    plan = setup_wizard.SetupPlan()
    fields = {f.name for f in dataclasses.fields(plan)}
    assert "embed_gguf" in fields
    assert plan.embed_gguf is None  # default


# ────────────────────────────────────────────────────────────────────────
# Agent Autodetection & Prompting Validation
# ────────────────────────────────────────────────────────────────────────


def test_detect_agents_none_found(monkeypatch, tmp_path):
    """When no agent binaries or directories are present, all fields are False."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(setup_wizard, "_find_hermes_plugins_dir", lambda: None)
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "")

    targets = setup_wizard._detect_agents()
    assert targets.claude is False
    assert targets.gemini is False
    assert targets.antigravity is False
    assert targets.opencode is False
    assert targets.openclaw is False
    assert targets.hermes is False
    assert targets.any() is False


def test_detect_agents_all_found(monkeypatch, tmp_path):
    """When all agent CLIs, fallbacks, or app-data dirs are mock-present, all fields are True."""
    monkeypatch.setattr("shutil.which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(setup_wizard, "_find_hermes_plugins_dir", lambda: tmp_path / "hermes_plugins")

    targets = setup_wizard._detect_agents()
    assert targets.claude is True
    assert targets.gemini is True
    assert targets.antigravity is True
    assert targets.opencode is True
    assert targets.openclaw is True
    assert targets.hermes is True
    assert targets.any() is True


def test_detect_agents_fallback_paths(monkeypatch, tmp_path):
    """Fallback paths (like ~/.gemini/antigravity-cli or ~/.openclaw) are correctly scanned."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)

    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)

    # 1. Gemini fallback: ~/.npm-global/bin/gemini
    gemini_path = home / ".npm-global" / "bin" / "gemini"
    gemini_path.parent.mkdir(parents=True, exist_ok=True)
    gemini_path.write_text("fake gemini binary", encoding="utf-8")

    # 2. Antigravity fallback: ~/.gemini/antigravity-cli
    agy_dir = home / ".gemini" / "antigravity-cli"
    agy_dir.mkdir(parents=True, exist_ok=True)

    # 3. OpenClaw fallback: ~/.openclaw
    openclaw_dir = home / ".openclaw"
    openclaw_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(setup_wizard, "_find_hermes_plugins_dir", lambda: None)

    targets = setup_wizard._detect_agents()
    assert targets.claude is False
    assert targets.gemini is True
    assert targets.antigravity is True
    assert targets.opencode is False
    assert targets.openclaw is True
    assert targets.hermes is False


def test_find_hermes_plugins_dir_via_env(monkeypatch, tmp_path):
    """HERMES_HOME environment variable takes highest priority for Hermes detection."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom_hermes"))

    pm = tmp_path / "custom_hermes" / "plugins" / "memory"
    pm.mkdir(parents=True)

    found = setup_wizard._find_hermes_plugins_dir()
    assert found is not None
    assert found == pm


def test_find_hermes_plugins_dir_via_localappdata(monkeypatch, tmp_path):
    """LOCALAPPDATA is probed on Windows for %LOCALAPPDATA%/hermes/hermes-agent/plugins/memory."""
    monkeypatch.setenv("HERMES_HOME", "")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))

    pm = tmp_path / "LocalAppData" / "hermes" / "hermes-agent" / "plugins" / "memory"
    pm.mkdir(parents=True)

    found = setup_wizard._find_hermes_plugins_dir()
    assert found is not None
    assert found == pm


def test_gather_plan_interactive_defaults(monkeypatch):
    """When agents are detected, interactive gather_plan defaults are optimal (True)."""
    detected = setup_wizard.AgentTargets(
        claude=True, gemini=True, antigravity=True,
        opencode=True, openclaw=True, hermes=True
    )

    class FakeArgs:
        non_interactive = False
        endpoint = None
        cognitive_loop = False
        agents = None
        capture_mode = None
        install_gpu_embedder = False

    args = FakeArgs()
    questions_asked = []

    def mock_ask_yes_no(question: str, default: bool) -> bool:
        questions_asked.append((question, default))
        return default

    def mock_ask_choice(question: str, choices: list[str], default: str) -> str:
        questions_asked.append((question, default))
        return default

    monkeypatch.setattr(setup_wizard, "_ask_yes_no", mock_ask_yes_no)
    monkeypatch.setattr(setup_wizard, "_ask_choice", mock_ask_choice)

    plan = setup_wizard._gather_plan(detected, args)

    assert plan.targets.claude is True
    assert plan.targets.gemini is True
    assert plan.targets.antigravity is True
    assert plan.targets.opencode is True
    assert plan.targets.openclaw is True
    assert plan.targets.hermes is True
    assert plan.capture_mode == "both"
    # F1: the Project Oxidation native wheel is a SAFE attempt (non-fatal,
    # auto-falls-back to pure-Python), so the interactive default is now ON —
    # but the source-build last resort stays opt-in (default False).
    assert plan.install_gpu_embedder is True
    assert plan.allow_native_source_build is False


def test_wire_opencode_writes_json(monkeypatch, tmp_path):
    """_wire_opencode creates opencode.json (canonical ``command:["m3"]``) when
    no existing config is found at ANY candidate path.

    The self-heal now scans BOTH %APPDATA%/opencode and ~/.config/opencode, so we
    must pin _opencode_config_paths to a hermetic tmp path — otherwise the real
    user's ~/.config/opencode/opencode.json short-circuits the write."""
    import json
    cfg_file = tmp_path / "opencode" / "opencode.json"
    # No file exists at this path yet -> _wire_opencode creates it at paths[0].
    monkeypatch.setattr(setup_wizard, "_opencode_config_paths", lambda: [cfg_file])

    success = setup_wizard._wire_opencode()
    assert success is True

    assert cfg_file.is_file()
    content = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert content["mcp"]["memory"]["command"] == ["m3"]
    assert content["mcp"]["memory"]["enabled"] is True


def test_wire_hermes_plugin_copy(monkeypatch, tmp_path):
    """_wire_hermes copies the bundled Hermes provider files to the plugin destination."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    for fname in ["__init__.py", "m3client.py", "plugin.yaml"]:
        (src_dir / fname).write_text(f"content of {fname}", encoding="utf-8")

    dst_parent = tmp_path / "dst" / "plugins" / "memory"
    dst_parent.mkdir(parents=True)

    monkeypatch.setattr(setup_wizard, "_find_m3_hermes_plugin_src", lambda: src_dir)
    monkeypatch.setattr(setup_wizard, "_find_hermes_plugins_dir", lambda: dst_parent)
    monkeypatch.setattr(setup_wizard, "_ask_yes_no", lambda q, default: True)

    success = setup_wizard._wire_hermes()
    assert success is True

    dst_m3 = dst_parent / "m3"
    assert dst_m3.is_dir()
    for fname in ["__init__.py", "m3client.py", "plugin.yaml"]:
        assert (dst_m3 / fname).is_file()
        assert (dst_m3 / fname).read_text(encoding="utf-8") == f"content of {fname}"


def test_gather_plan_non_interactive_decouple_and_fips(monkeypatch):
    """Non-interactive gather plan respects decoupled roots and FIPS flags."""
    args = argparse.Namespace(
        endpoint="http://localhost:1111",
        cognitive_loop=True,
        non_interactive=True,
        agents="claude",
        capture_mode="stop",
        install_gpu_embedder=True,
        decouple_roots=True,
        config_root="/tmp/config",
        engine_root="/tmp/engine",
        fips_mode=True,
    )
    detected = setup_wizard.AgentTargets(claude=True)
    plan = setup_wizard._gather_plan(detected, args)

    assert plan.decouple_roots is True
    assert plan.config_root == "/tmp/config"
    assert plan.engine_root == "/tmp/engine"
    assert plan.fips_mode is True


def test_gather_plan_interactive_decouple_and_fips(monkeypatch):
    """Interactive gather plan prompts and accepts decoupled roots and FIPS choices."""
    args = argparse.Namespace(
        endpoint=None,
        cognitive_loop=False,
        non_interactive=False,
    )
    detected = setup_wizard.AgentTargets(claude=True)

    calls = []
    def mock_ask_yes_no(question, default):
        calls.append(question)
        # The separate-config+database-folders prompt (formerly "decoupled").
        if "separate config" in question or "folders" in question:
            return True
        # The FIPS "build wolfSSL now?" follow-up — accept it.
        return default

    choice_calls = []
    def mock_ask_choice(question, choices, default):
        choice_calls.append(question)
        # FIPS is now a tiered choice (off/mode/strict) — pick 'mode'.
        if "FIPS" in question:
            return "mode"
        return default

    monkeypatch.setattr(setup_wizard, "_ask_yes_no", mock_ask_yes_no)
    monkeypatch.setattr(setup_wizard, "_ask_choice", mock_ask_choice)

    plan = setup_wizard._gather_plan(detected, args)

    assert plan.decouple_roots is True
    assert plan.config_root is not None
    assert plan.engine_root is not None
    assert plan.fips_mode is True
    assert plan.fips_strict is False  # 'mode' tier, not strict
    assert any("separate config" in c or "folders" in c for c in calls)
    assert any("FIPS" in c for c in choice_calls)


# ────────────────────────────────────────────────────────────────────────
# _step_install_m3 — must pass --force (macOS install hardening)
# ────────────────────────────────────────────────────────────────────────


def test_step_install_m3_passes_force(monkeypatch):
    """Wizard must pass --force to install-m3 so re-running `m3 setup` (and
    install.sh) upgrades in place instead of aborting with `repo already
    exists`. install_m3() preserves user data on --force, so this is safe
    and a no-op on fresh installs.
    """
    captured: list = []

    class _FakeProc:
        returncode = 0

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(setup_wizard, "_run", fake_run)
    # New skip-guard: _step_install_m3 returns early (no fetch subprocess) when
    # find_bridge() already resolves. Force the fetch path by making it None so
    # the --force install-m3 command is actually issued. find_bridge is imported
    # inside the function from m3_memory.installer, so patch it at the source.
    from m3_memory import installer
    monkeypatch.setattr(installer, "find_bridge", lambda: None)

    plan = setup_wizard.SetupPlan()
    plan.capture_mode = "both"
    assert setup_wizard._step_install_m3(plan) is True

    assert len(captured) == 1
    cmd = captured[0]
    assert "install-m3" in cmd
    assert "--force" in cmd, (
        f"_step_install_m3 must pass --force; got: {cmd}"
    )
    assert "--non-interactive" in cmd
    assert "--capture-mode" in cmd


# ────────────────────────────────────────────────────────────────────────
# _persist_embed_gguf — Fix #7: M3_EMBED_GGUF persistence
# ────────────────────────────────────────────────────────────────────────


def test_persist_embed_gguf_writes_zshrc(monkeypatch, tmp_path):
    """Non-interactive run writes the export to ~/.zshrc (zsh shell)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("# existing rc\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(sys, "platform", "darwin")  # not win32

    setup_wizard._persist_embed_gguf("/path/to/bge-m3.gguf", non_interactive=True)

    content = (home / ".zshrc").read_text(encoding="utf-8")
    assert "M3_EMBED_GGUF" in content
    assert '"/path/to/bge-m3.gguf"' in content
    # Original content is preserved
    assert "# existing rc" in content


def test_persist_embed_gguf_idempotent(monkeypatch, tmp_path):
    """Re-running doesn't append a second export line."""
    home = tmp_path / "home"
    home.mkdir()
    rc = home / ".zshrc"
    rc.write_text(
        '# already set\nexport M3_EMBED_GGUF="/old/path.gguf"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(sys, "platform", "darwin")

    setup_wizard._persist_embed_gguf("/new/path.gguf", non_interactive=True)

    content = rc.read_text(encoding="utf-8")
    assert content.count("M3_EMBED_GGUF") == 1, (
        "Expected exactly one M3_EMBED_GGUF entry; got duplicate writes"
    )
    # Pre-existing path is left untouched (user already configured it)
    assert "/old/path.gguf" in content


def test_persist_embed_gguf_patches_claude_settings(monkeypatch, tmp_path):
    """Adds env.M3_EMBED_GGUF to the 'memory' MCP entry in claude settings."""
    import json

    home = tmp_path / "home"
    home.mkdir()
    claude_dir = home / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps({
        "mcpServers": {"memory": {"command": "mcp-memory"}},
    }), encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(sys, "platform", "darwin")

    setup_wizard._persist_embed_gguf("/path/to/bge-m3.gguf", non_interactive=True)

    cfg = json.loads(settings.read_text(encoding="utf-8"))
    mem = cfg["mcpServers"]["memory"]
    assert mem.get("env", {}).get("M3_EMBED_GGUF") == "/path/to/bge-m3.gguf", (
        f"Expected env wired on memory MCP entry; got: {mem}"
    )


def test_persist_embed_gguf_skips_settings_without_memory_entry(monkeypatch, tmp_path):
    """If settings.json has no 'memory' entry yet, we don't pre-create it —
    the per-agent wiring step later in setup is responsible for that."""
    import json

    home = tmp_path / "home"
    home.mkdir()
    claude_dir = home / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr(sys, "platform", "darwin")

    setup_wizard._persist_embed_gguf("/path/to/bge-m3.gguf", non_interactive=True)

    cfg = json.loads(settings.read_text(encoding="utf-8"))
    # No memory entry was conjured up out of nothing
    assert "memory" not in cfg.get("mcpServers", {})


def test_persist_embed_gguf_uses_setx_on_windows(monkeypatch, tmp_path):
    """Windows uses `setx` instead of writing a shell rc — setx is the
    canonical way to persist user env vars across reboot."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(sys, "platform", "win32")

    captured: list = []

    class _FakeResult:
        returncode = 0
        stdout = "SUCCESS: Specified value was saved."
        stderr = ""

    def fake_run(cmd, *args, **kwargs):
        captured.append(cmd)
        return _FakeResult()

    monkeypatch.setattr(subprocess, "run", fake_run)

    setup_wizard._persist_embed_gguf("C:\\path\\bge-m3.gguf", non_interactive=True)

    # Exactly one setx call with the right env var and path
    assert len(captured) == 1
    cmd = captured[0]
    assert cmd[0] == "setx"
    assert cmd[1] == "M3_EMBED_GGUF"
    assert cmd[2] == "C:\\path\\bge-m3.gguf"

    # No Unix rc files were created (we did NOT fall through to the Unix path)
    assert not (home / ".zshrc").exists()
    assert not (home / ".bashrc").exists()


def test_persist_embed_gguf_patches_mcp_settings_on_windows(monkeypatch, tmp_path):
    """The MCP settings.json env wiring must run on Windows too — Claude
    Code on Windows reads %USERPROFILE%\\.claude\\settings.json, and the
    spawned MCP server doesn't inherit the user env from the GUI process."""
    import json

    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    settings = home / ".claude" / "settings.json"
    settings.write_text(json.dumps({
        "mcpServers": {"memory": {"command": "mcp-memory"}},
    }), encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(sys, "platform", "win32")
    # Stub setx so the shell-env step doesn't try to execute a real binary.
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: mock.Mock(returncode=0, stdout="", stderr=""))

    setup_wizard._persist_embed_gguf("C:\\path\\bge-m3.gguf", non_interactive=True)

    cfg = json.loads(settings.read_text(encoding="utf-8"))
    mem = cfg["mcpServers"]["memory"]
    assert mem.get("env", {}).get("M3_EMBED_GGUF") == "C:\\path\\bge-m3.gguf"


def test_persist_embed_gguf_setx_failure_is_non_fatal(monkeypatch, tmp_path):
    """A setx crash on Windows warns but does not abort — the GGUF is still
    set in the current process, and that's enough for the rest of setup."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(*a, **kw):
        raise OSError("setx not found")

    monkeypatch.setattr(subprocess, "run", boom)

    # Should not raise
    setup_wizard._persist_embed_gguf("C:\\path\\bge-m3.gguf", non_interactive=True)


# ────────────────────────────────────────────────────────────────────────
# _pick_unix_shell_rc — Linux/macOS shell rc selection
# ────────────────────────────────────────────────────────────────────────


def test_pick_unix_shell_rc_prefers_zsh_when_shell_env_zsh(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert setup_wizard._pick_unix_shell_rc() == home / ".zshrc"


def test_pick_unix_shell_rc_prefers_bashrc_when_shell_env_bash(monkeypatch, tmp_path):
    """Linux default (bash) lands on ~/.bashrc — distinct from macOS default."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert setup_wizard._pick_unix_shell_rc() == home / ".bashrc"


def test_pick_unix_shell_rc_falls_back_to_existing_file(monkeypatch, tmp_path):
    """Unknown SHELL — pick whichever rc actually exists."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".profile").write_text("# ksh", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/usr/local/bin/ksh")
    assert setup_wizard._pick_unix_shell_rc() == home / ".profile"


# ────────────────────────────────────────────────────────────────────────
# _discover_bge_m3_gguf — Linux LM Studio default path
# ────────────────────────────────────────────────────────────────────────


def test_discover_finds_gguf_in_linux_lmstudio_cache(monkeypatch, tmp_path):
    """LM Studio on Linux stores models under ~/.cache/lm-studio/models —
    the discovery cascade must include that path."""
    home = tmp_path / "home"
    linux_lm = home / ".cache" / "lm-studio" / "models"
    linux_lm.mkdir(parents=True)
    target = linux_lm / "bge-m3-Q4_K_M.gguf"
    target.write_bytes(b"fake")
    monkeypatch.setattr(Path, "home", lambda: home)
    found = setup_wizard._discover_bge_m3_gguf()
    assert found is not None
    assert Path(found) == target




# ── Probe 5: LLM endpoint detection + failover wiring ────────────────────────

def _probe_args(non_interactive=True):
    return argparse.Namespace(non_interactive=non_interactive)


def test_endpoint_reachable_false_on_dead_port():
    # A definitely-closed port must report unreachable (and not raise).
    assert setup_wizard._endpoint_reachable("http://localhost:1", timeout=0.3) is False


def test_probe_honors_explicit_csv(monkeypatch, capsys):
    monkeypatch.setenv("LLM_ENDPOINTS_CSV", "http://x:9/v1")
    monkeypatch.delenv("M3_LLM_URL", raising=False)
    setup_wizard._probe_llm_endpoints(object(), _probe_args())
    assert "LLM_ENDPOINTS_CSV" in capsys.readouterr().out


def test_probe_honors_custom_url(monkeypatch, capsys):
    monkeypatch.delenv("LLM_ENDPOINTS_CSV", raising=False)
    monkeypatch.setenv("M3_LLM_URL", "http://localhost:8080/v1")
    monkeypatch.setattr(setup_wizard, "_endpoint_reachable", lambda u, **k: False)
    setup_wizard._probe_llm_endpoints(object(), _probe_args())
    assert "M3_LLM_URL set" in capsys.readouterr().out


def test_probe_enables_ollama_when_only_ollama_reachable(monkeypatch):
    monkeypatch.delenv("LLM_ENDPOINTS_CSV", raising=False)
    monkeypatch.delenv("M3_LLM_URL", raising=False)
    monkeypatch.delenv("M3_ENABLE_OLLAMA_FAILOVER", raising=False)
    monkeypatch.delenv("M3_ENABLE_LMSTUDIO_FAILOVER", raising=False)
    # Only Ollama (:11434) answers; LM Studio (:1234) is dead.
    monkeypatch.setattr(setup_wizard, "_endpoint_reachable",
                        lambda url, **k: "11434" in url)
    persisted = {}
    monkeypatch.setattr(setup_wizard, "_persist_env_var",
                        lambda n, v, **k: persisted.__setitem__(n, v))
    setup_wizard._probe_llm_endpoints(object(), _probe_args(non_interactive=True))
    # Ollama enabled, LM Studio probe disabled (it's not reachable).
    assert persisted.get("M3_ENABLE_OLLAMA_FAILOVER") == "1"
    assert persisted.get("M3_ENABLE_LMSTUDIO_FAILOVER") == "0"


def test_probe_no_op_when_only_lmstudio_reachable(monkeypatch):
    # LM Studio is the default-on endpoint — nothing to persist when it's the
    # only one up (no stale disable, no redundant enable).
    monkeypatch.delenv("LLM_ENDPOINTS_CSV", raising=False)
    monkeypatch.delenv("M3_LLM_URL", raising=False)
    monkeypatch.delenv("M3_ENABLE_LMSTUDIO_FAILOVER", raising=False)
    monkeypatch.delenv("M3_ENABLE_OLLAMA_FAILOVER", raising=False)
    monkeypatch.setattr(setup_wizard, "_endpoint_reachable",
                        lambda url, **k: "1234" in url)
    persisted = {}
    monkeypatch.setattr(setup_wizard, "_persist_env_var",
                        lambda n, v, **k: persisted.__setitem__(n, v))
    setup_wizard._probe_llm_endpoints(object(), _probe_args(non_interactive=True))
    assert persisted == {}


# ────────────────────────────────────────────────────────────────────────
# _wire_hermes — non-interactive must NOT prompt (regression: EOFError when
# an existing Hermes plugin triggered an overwrite input() under
# --non-interactive, aborting the whole `m3 setup`).
# ────────────────────────────────────────────────────────────────────────


def _hermes_dirs(tmp_path, plugin_content="SRC"):
    """Build a vendored src/ (with all plugin files) + an empty hermes plugins/
    dir. Returns (src, plugins)."""
    src = tmp_path / "src"
    src.mkdir()
    for fname in setup_wizard._HERMES_PLUGIN_FILES:
        (src / fname).write_text(plugin_content)
    plugins = tmp_path / "hermes" / "plugins" / "memory"
    plugins.mkdir(parents=True)
    return src, plugins


def _no_input(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("input() called in non-interactive mode")
    monkeypatch.setattr("builtins.input", _boom)


def test_wire_hermes_fresh_install(monkeypatch, tmp_path):
    """No existing plugin → files copied in; no prompt."""
    src, plugins = _hermes_dirs(tmp_path)
    monkeypatch.setattr(setup_wizard, "_find_m3_hermes_plugin_src", lambda: src)
    monkeypatch.setattr(setup_wizard, "_find_hermes_plugins_dir", lambda: plugins)
    _no_input(monkeypatch)

    assert setup_wizard._wire_hermes(non_interactive=True) is True
    assert (plugins / "m3" / "__init__.py").read_text() == "SRC"


def test_wire_hermes_current_plugin_left_untouched(monkeypatch, tmp_path):
    """An up-to-date plugin (files match source) is not rewritten — mirrors
    OpenCode 'already wired'. No prompt, no backup created."""
    src, plugins = _hermes_dirs(tmp_path)
    dst = plugins / "m3"
    dst.mkdir()
    for fname in setup_wizard._HERMES_PLUGIN_FILES:
        (dst / fname).write_text("SRC")  # identical to source
    monkeypatch.setattr(setup_wizard, "_find_m3_hermes_plugin_src", lambda: src)
    monkeypatch.setattr(setup_wizard, "_find_hermes_plugins_dir", lambda: plugins)
    _no_input(monkeypatch)

    assert setup_wizard._wire_hermes(non_interactive=True) is True
    assert not (plugins / "m3.m3bak").exists()  # nothing backed up


def test_wire_hermes_stale_plugin_backed_up_and_updated(monkeypatch, tmp_path):
    """A stale plugin (older content) is backed up to m3.m3bak and refreshed with
    the new source files — the self-healing upgrade path. No prompt."""
    src, plugins = _hermes_dirs(tmp_path, plugin_content="NEW")
    dst = plugins / "m3"
    dst.mkdir()
    for fname in setup_wizard._HERMES_PLUGIN_FILES:
        (dst / fname).write_text("OLD")  # differs from source -> stale
    monkeypatch.setattr(setup_wizard, "_find_m3_hermes_plugin_src", lambda: src)
    monkeypatch.setattr(setup_wizard, "_find_hermes_plugins_dir", lambda: plugins)
    _no_input(monkeypatch)

    assert setup_wizard._wire_hermes(non_interactive=True) is True
    # new files installed
    assert (dst / "__init__.py").read_text() == "NEW"
    # old files preserved in backup
    assert (plugins / "m3.m3bak" / "__init__.py").read_text() == "OLD"


def test_wire_hermes_partial_plugin_is_stale(monkeypatch, tmp_path):
    """A plugin dir missing one of the shipped files counts as stale and is
    healed (guards _hermes_plugin_is_current's missing-file branch)."""
    src, plugins = _hermes_dirs(tmp_path, plugin_content="NEW")
    dst = plugins / "m3"
    dst.mkdir()
    # only the first file present -> incomplete
    (dst / setup_wizard._HERMES_PLUGIN_FILES[0]).write_text("NEW")
    monkeypatch.setattr(setup_wizard, "_find_m3_hermes_plugin_src", lambda: src)
    monkeypatch.setattr(setup_wizard, "_find_hermes_plugins_dir", lambda: plugins)
    _no_input(monkeypatch)

    assert setup_wizard._wire_hermes(non_interactive=True) is True
    for fname in setup_wizard._HERMES_PLUGIN_FILES:
        assert (dst / fname).read_text() == "NEW"


# ────────────────────────────────────────────────────────────────────────
# _quiesce_db_writers — cooperative HALT flow decision logic (HALT_PROTOCOL).
# A fake halt module injected via _import_m3_halt exercises the branches
# without real processes.
# ────────────────────────────────────────────────────────────────────────


class _FakeProc:
    def __init__(self, role, pid):
        self.role, self.pid = role, pid


class _FakeHalt:
    """Records set/clear/kill and returns scripted quiesce results."""
    def __init__(self, live, quiesce_results):
        self._live = live
        self._results = list(quiesce_results)  # popped per wait_for_quiesce call
        self.set_called = 0
        self.cleared = 0

    def list_live_processes(self):
        return self._live

    def list_all_db_writers(self):
        # In these tests the registry + cmdline-scan union is simulated by _live.
        return self._live

    def elevated_kill_commands(self, pids):
        return [f"sudo kill {' '.join(str(p) for p in pids)}"]

    def set_halt(self, owner, reason):
        self.set_called += 1

    def clear_halt(self):
        self.cleared += 1

    def wait_for_quiesce(self, timeout):
        return self._results.pop(0)


def _R(ok, stuck=()):
    return SimpleNamespace(ok=ok, stuck=list(stuck))


def _q_args(**kw):
    base = dict(non_interactive=True, force_quiesce=False, force_kill_mcp=False,
                quiesce_timeout=1.0)
    base.update(kw)
    return argparse.Namespace(**base)


def test_quiesce_no_writers_proceeds(monkeypatch):
    fake = _FakeHalt(live=[], quiesce_results=[])
    monkeypatch.setattr(setup_wizard, "_import_m3_halt", lambda: fake)
    assert setup_wizard._quiesce_db_writers(_q_args()) is True
    assert fake.set_called == 0  # nothing to quiesce → never raised HALT


def test_quiesce_writers_pause_in_time(monkeypatch):
    fake = _FakeHalt(live=[_FakeProc("cognitive-loop", 111)],
                     quiesce_results=[_R(True)])
    monkeypatch.setattr(setup_wizard, "_import_m3_halt", lambda: fake)
    assert setup_wizard._quiesce_db_writers(_q_args()) is True
    assert fake.set_called == 1
    # HALT stays raised through install (cleared by run_setup, not the helper).
    assert fake.cleared == 0


def test_quiesce_stuck_non_interactive_no_force_aborts(monkeypatch):
    fake = _FakeHalt(live=[_FakeProc("cognitive-loop", 111)],
                     quiesce_results=[_R(False, [_FakeProc("cognitive-loop", 111)])])
    monkeypatch.setattr(setup_wizard, "_import_m3_halt", lambda: fake)
    assert setup_wizard._quiesce_db_writers(_q_args(force_quiesce=False)) is False
    assert fake.cleared == 1  # abort path clears its own HALT


def test_quiesce_stuck_non_interactive_force_kills_then_proceeds(monkeypatch):
    stuck = [_FakeProc("cognitive-loop", 111)]
    fake = _FakeHalt(live=list(stuck),
                     quiesce_results=[_R(False, stuck), _R(True)])
    killed = []
    monkeypatch.setattr(setup_wizard, "_import_m3_halt", lambda: fake)
    monkeypatch.setattr(setup_wizard, "_kill_process_windows", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(setup_wizard, "_kill_process_posix", lambda pid: killed.append(pid) or True)
    assert setup_wizard._quiesce_db_writers(_q_args(force_quiesce=True)) is True
    assert killed == [111]


def test_quiesce_interactive_abort(monkeypatch):
    stuck = [_FakeProc("cognitive-loop", 111)]
    fake = _FakeHalt(live=list(stuck), quiesce_results=[_R(False, stuck)])
    monkeypatch.setattr(setup_wizard, "_import_m3_halt", lambda: fake)
    monkeypatch.setattr(setup_wizard, "_ask_choice", lambda *a, **k: "abort")
    assert setup_wizard._quiesce_db_writers(_q_args(non_interactive=False)) is False
    assert fake.cleared == 1


def test_quiesce_interactive_kill(monkeypatch):
    stuck = [_FakeProc("cognitive-loop", 111)]
    fake = _FakeHalt(live=list(stuck), quiesce_results=[_R(False, stuck), _R(True)])
    killed = []
    monkeypatch.setattr(setup_wizard, "_import_m3_halt", lambda: fake)
    monkeypatch.setattr(setup_wizard, "_ask_choice", lambda *a, **k: "kill")
    monkeypatch.setattr(setup_wizard, "_kill_process_windows", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(setup_wizard, "_kill_process_posix", lambda pid: killed.append(pid) or True)
    assert setup_wizard._quiesce_db_writers(_q_args(non_interactive=False)) is True
    assert killed == [111]


def test_quiesce_halt_unavailable_proceeds(monkeypatch):
    # If m3_halt can't be imported, don't block install (file-lock probe still guards).
    monkeypatch.setattr(setup_wizard, "_import_m3_halt", lambda: None)
    assert setup_wizard._quiesce_db_writers(_q_args()) is True


def test_quiesce_elevated_kill_failure_aborts_with_help(monkeypatch, capsys):
    """When a stuck writer can't be killed (elevated), quiesce must NOT report
    success or proceed — it aborts, clears HALT, and surfaces the elevated
    command to run. (--force-quiesce, non-interactive.)"""
    stuck = [_FakeProc("mcp(elevated?)", 555)]
    fake = _FakeHalt(live=list(stuck), quiesce_results=[_R(False, stuck)])
    monkeypatch.setattr(setup_wizard, "_import_m3_halt", lambda: fake)
    # kill refused (elevated target) on both platforms
    monkeypatch.setattr(setup_wizard, "_kill_process_windows", lambda pid: False)
    monkeypatch.setattr(setup_wizard, "_kill_process_posix", lambda pid: False)

    assert setup_wizard._quiesce_db_writers(_q_args(force_quiesce=True)) is False
    assert fake.cleared == 1  # HALT cleared on abort (writers not left wedged)
    out = capsys.readouterr().out
    assert "elevated" in out.lower()
    assert "sudo kill 555" in out  # the ready-to-run command was surfaced


def test_interactive_kill_retries_with_sudo(monkeypatch, capsys):
    """Interactive 'kill': an unprivileged kill that's refused is retried via sudo
    (which prompts inline). If sudo succeeds, quiesce proceeds — no abort."""
    stuck = [_FakeProc("cognitive-loop(elevated?)", 777)]
    # first wait -> stuck; after sudo kill -> quiesced
    fake = _FakeHalt(live=list(stuck), quiesce_results=[_R(False, stuck), _R(True)])
    monkeypatch.setattr(setup_wizard, "_import_m3_halt", lambda: fake)
    monkeypatch.setattr(setup_wizard, "_ask_choice", lambda *a, **k: "kill")
    # unprivileged kill refused, sudo kill succeeds
    monkeypatch.setattr(setup_wizard, "_kill_process_posix", lambda pid: False)
    sudo_calls = []
    monkeypatch.setattr(setup_wizard, "_sudo_kill_posix",
                        lambda pid: sudo_calls.append(pid) or True)
    monkeypatch.setattr(setup_wizard.sys, "platform", "linux")

    assert setup_wizard._quiesce_db_writers(_q_args(non_interactive=False)) is True
    assert sudo_calls == [777]  # sudo was attempted for the refused pid


def test_noninteractive_force_does_not_use_sudo(monkeypatch):
    """Non-interactive --force-quiesce must NOT attempt sudo (it would hang with no
    console to prompt on); a refused kill aborts with the elevated-command help."""
    stuck = [_FakeProc("mcp(elevated?)", 888)]
    fake = _FakeHalt(live=list(stuck), quiesce_results=[_R(False, stuck)])
    monkeypatch.setattr(setup_wizard, "_import_m3_halt", lambda: fake)
    monkeypatch.setattr(setup_wizard, "_kill_process_posix", lambda pid: False)
    monkeypatch.setattr(setup_wizard, "_kill_process_windows", lambda pid: False)
    sudo_called = []
    monkeypatch.setattr(setup_wizard, "_sudo_kill_posix",
                        lambda pid: sudo_called.append(pid) or True)

    assert setup_wizard._quiesce_db_writers(_q_args(force_quiesce=True)) is False
    assert sudo_called == []  # sudo NEVER attempted headless


def test_interactive_kill_retries_with_runas_on_windows(monkeypatch):
    """Interactive 'kill' on Windows: a refused unprivileged kill retries via
    _runas_kill_windows (UAC), symmetric to sudo on POSIX. If RunAs succeeds,
    quiesce proceeds."""
    stuck = [_FakeProc("mcp(elevated?)", 321)]
    fake = _FakeHalt(live=list(stuck), quiesce_results=[_R(False, stuck), _R(True)])
    monkeypatch.setattr(setup_wizard, "_import_m3_halt", lambda: fake)
    monkeypatch.setattr(setup_wizard, "_ask_choice", lambda *a, **k: "kill")
    monkeypatch.setattr(setup_wizard.sys, "platform", "win32")
    monkeypatch.setattr(setup_wizard, "_kill_process_windows", lambda pid: False)
    runas_calls = []
    monkeypatch.setattr(setup_wizard, "_runas_kill_windows",
                        lambda pid: runas_calls.append(pid) or True)
    # sudo path must NOT be used on Windows
    sudo_calls = []
    monkeypatch.setattr(setup_wizard, "_sudo_kill_posix",
                        lambda pid: sudo_calls.append(pid) or True)

    assert setup_wizard._quiesce_db_writers(_q_args(non_interactive=False)) is True
    assert runas_calls == [321]   # RunAs (UAC) attempted
    assert sudo_calls == []        # sudo NOT used on Windows


def test_runas_kill_windows_reads_elevated_exit_code(monkeypatch):
    """_runas_kill_windows returns True only when the elevated taskkill exits 0
    (killed) or 128 (already gone); a cancelled-UAC exception → False."""
    # exit 0 -> success
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: mock.Mock(returncode=0))
    assert setup_wizard._runas_kill_windows(123) is True
    # exit 128 (not found / already gone) -> treated as success
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: mock.Mock(returncode=128))
    assert setup_wizard._runas_kill_windows(123) is True
    # exit 1 (access denied / still there) -> False
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: mock.Mock(returncode=1))
    assert setup_wizard._runas_kill_windows(123) is False
    # UAC cancelled -> subprocess raises -> False
    def boom(*a, **k):
        raise OSError("user cancelled UAC")
    monkeypatch.setattr(subprocess, "run", boom)
    assert setup_wizard._runas_kill_windows(123) is False


def test_quiesce_registers_atexit_clear(monkeypatch):
    """Raising HALT_m3 also registers an atexit clear, so a sys.exit/re-exec/
    subprocess handoff before run_setup's finally can't leak a raised halt.
    (Regression: a FIPS/wolfSSL upgrade once exited without the finally firing.)"""
    stuck = [_FakeProc("cognitive-loop", 111)]
    fake = _FakeHalt(live=list(stuck), quiesce_results=[_R(True)])
    monkeypatch.setattr(setup_wizard, "_import_m3_halt", lambda: fake)
    registered = []
    monkeypatch.setattr(setup_wizard.atexit, "register",
                        lambda fn, *a, **k: registered.append(fn))

    assert setup_wizard._quiesce_db_writers(_q_args()) is True
    # the fake's clear_halt bound method must be what got registered
    assert fake.clear_halt in registered, "atexit clear not registered on quiesce"


# ────────────────────────────────────────────────────────────────────────
# Boot-task registration: inline UAC elevation offer (Windows), and the
# no-elevation-needed path on Linux/macOS (user-level systemd/launchd).
# ────────────────────────────────────────────────────────────────────────


def test_offer_elevated_repair_noop_off_windows(monkeypatch):
    """Linux/macOS register user-level services (no privilege) — so the elevation
    offer is a no-op there; it must NEVER prompt or try to elevate."""
    monkeypatch.setattr(setup_wizard.sys, "platform", "linux")
    asked = []
    monkeypatch.setattr(setup_wizard, "_ask_yes_no", lambda *a, **k: asked.append(1) or True)
    assert setup_wizard._offer_elevated_schedule_repair("x", non_interactive=False) is False
    assert asked == []  # never even prompted on non-Windows


def test_offer_elevated_repair_noop_non_interactive(monkeypatch):
    """Non-interactive never elevates (UAC is a GUI prompt with no one to consent)."""
    monkeypatch.setattr(setup_wizard.sys, "platform", "win32")
    ran = []
    monkeypatch.setattr(setup_wizard, "_runas_schedule_repair_windows", lambda s: ran.append(s) or True)
    assert setup_wizard._offer_elevated_schedule_repair("x", non_interactive=True) is False
    assert ran == []


def test_offer_elevated_repair_runs_on_yes(monkeypatch):
    """Interactive Windows, user says yes -> UAC repair runs; success -> True."""
    monkeypatch.setattr(setup_wizard.sys, "platform", "win32")
    monkeypatch.setattr(setup_wizard, "_ask_yes_no", lambda *a, **k: True)
    ran = []
    monkeypatch.setattr(setup_wizard, "_runas_schedule_repair_windows",
                        lambda s: ran.append(s) or True)
    assert setup_wizard._offer_elevated_schedule_repair(r"C:\x\install_schedules.py",
                                                        non_interactive=False) is True
    assert ran == [r"C:\x\install_schedules.py"]


def test_offer_elevated_repair_declined(monkeypatch):
    """User declines the prompt -> no elevation, returns False (falls back to banner)."""
    monkeypatch.setattr(setup_wizard.sys, "platform", "win32")
    monkeypatch.setattr(setup_wizard, "_ask_yes_no", lambda *a, **k: False)
    ran = []
    monkeypatch.setattr(setup_wizard, "_runas_schedule_repair_windows", lambda s: ran.append(s) or True)
    assert setup_wizard._offer_elevated_schedule_repair("x", non_interactive=False) is False
    assert ran == []  # never attempted after decline


def test_runas_schedule_repair_reads_exit_code(monkeypatch):
    """_runas_schedule_repair_windows returns True only on elevated exit 0."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: mock.Mock(returncode=0))
    assert setup_wizard._runas_schedule_repair_windows("s") is True
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: mock.Mock(returncode=1))
    assert setup_wizard._runas_schedule_repair_windows("s") is False
    def boom(*a, **k):
        raise OSError("UAC cancelled")
    monkeypatch.setattr(subprocess, "run", boom)
    assert setup_wizard._runas_schedule_repair_windows("s") is False
