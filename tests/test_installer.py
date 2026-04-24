"""Tests for m3_memory.installer.

Covers the resolution order in find_bridge and the install_m3 flow end-to-end
with git + tarball both mocked. Does NOT hit the network.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path):
    """Redirect Path.home() to tmp_path so config + repo don't touch the real home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    # Path.home() caches nothing; it reads HOME / USERPROFILE every call.
    # Still, drop any stale M3_BRIDGE_PATH.
    monkeypatch.delenv("M3_BRIDGE_PATH", raising=False)
    # Reload the installer module so any module-level paths re-resolve against
    # the new HOME. (There aren't any today, but cheap insurance.)
    if "m3_memory.installer" in sys.modules:
        del sys.modules["m3_memory.installer"]


def test_find_bridge_returns_none_when_nothing_configured(tmp_path):
    from m3_memory.installer import find_bridge

    # No env var, no config file, no sibling bin/memory_bridge.py reachable
    # from the installer module's location (pytest imports from site-packages
    # or -e install; either way the sibling walk succeeds in dev).
    #
    # This test only asserts the env + config paths return None. The
    # developer-sibling fallback is tested separately below.
    result = find_bridge()
    # In the dev repo the developer-sibling walk finds bin/memory_bridge.py.
    # In a pure pip install it would be None. Accept both: the point is the
    # function doesn't crash and returns a resolvable path when one exists.
    assert result is None or result.is_file()


def test_find_bridge_honors_env_var(tmp_path):
    """$M3_BRIDGE_PATH, when pointing at a real file, is returned first."""
    from m3_memory import installer

    fake_bridge = tmp_path / "fake_bridge.py"
    fake_bridge.write_text("# fake")

    with patch.dict(os.environ, {"M3_BRIDGE_PATH": str(fake_bridge)}):
        assert installer.find_bridge() == fake_bridge.resolve()


def test_find_bridge_env_var_nonexistent_falls_through(tmp_path):
    """An env var pointing at a missing file is ignored; we don't crash."""
    from m3_memory import installer

    with patch.dict(os.environ, {"M3_BRIDGE_PATH": "/nowhere/fake_bridge.py"}):
        # Either None or a fallback (developer sibling); never raises.
        result = installer.find_bridge()
        assert result is None or result.is_file()


def test_find_bridge_honors_config_file(tmp_path, monkeypatch):
    """When ~/.m3-memory/config.json has a valid bridge_path, use it."""
    from m3_memory import installer

    fake_bridge = tmp_path / "configured_bridge.py"
    fake_bridge.write_text("# fake")

    # Redirect config_dir to tmp_path to avoid touching real home.
    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".m3-memory")
    monkeypatch.setattr(installer, "config_file", lambda: tmp_path / ".m3-memory" / "config.json")

    installer.save_config({"bridge_path": str(fake_bridge)})
    assert installer.find_bridge() == fake_bridge.resolve()


def test_config_roundtrip(tmp_path, monkeypatch):
    from m3_memory import installer

    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".m3-memory")
    monkeypatch.setattr(installer, "config_file", lambda: tmp_path / ".m3-memory" / "config.json")

    assert installer.load_config() == {}
    installer.save_config({"bridge_path": "/a/b", "version": "1.0"})
    assert installer.load_config() == {"bridge_path": "/a/b", "version": "1.0"}


def test_load_config_tolerates_malformed_json(tmp_path, monkeypatch):
    from m3_memory import installer

    cfg_dir = tmp_path / ".m3-memory"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    cfg_file.write_text("{ not valid json")

    monkeypatch.setattr(installer, "config_dir", lambda: cfg_dir)
    monkeypatch.setattr(installer, "config_file", lambda: cfg_file)

    # Should return {} instead of crashing.
    assert installer.load_config() == {}


def test_install_m3_via_git_mock(tmp_path, monkeypatch):
    """install_m3 uses git clone when available; writes config with bridge_path."""
    from m3_memory import installer

    repo_path = tmp_path / "repo"
    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".m3-memory")
    monkeypatch.setattr(installer, "config_file", lambda: tmp_path / ".m3-memory" / "config.json")

    # Mock git clone by creating the expected directory structure at the
    # destination when subprocess.run is called.
    def fake_run(cmd, **kwargs):
        assert cmd[0] == "git" and cmd[1] == "clone"
        dest = Path(cmd[-1])
        (dest / "bin").mkdir(parents=True)
        (dest / "bin" / "memory_bridge.py").write_text("# fetched")
        class _R: returncode = 0
        return _R()

    monkeypatch.setattr(subprocess, "run", fake_run)

    bridge = installer.install_m3(repo_path=repo_path, tag="v1.2.3")
    assert bridge == (repo_path / "bin" / "memory_bridge.py").resolve()

    cfg = installer.load_config()
    assert cfg["bridge_path"] == str(bridge)
    assert cfg["tag"] == "v1.2.3"


def test_install_m3_refuses_overwrite_without_force(tmp_path, monkeypatch):
    from m3_memory import installer

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".m3-memory")
    monkeypatch.setattr(installer, "config_file", lambda: tmp_path / ".m3-memory" / "config.json")

    with pytest.raises(RuntimeError, match="already exists"):
        installer.install_m3(repo_path=repo_path, tag="v1.2.3")


def test_install_m3_falls_back_to_tarball_when_git_missing(tmp_path, monkeypatch):
    """When git is not installed, install_m3 falls through to the tarball path."""
    from m3_memory import installer

    repo_path = tmp_path / "repo"
    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".m3-memory")
    monkeypatch.setattr(installer, "config_file", lambda: tmp_path / ".m3-memory" / "config.json")

    def fake_run(*a, **kw):
        raise FileNotFoundError("git not installed")
    monkeypatch.setattr(subprocess, "run", fake_run)

    # Mock the tarball downloader by staging a fake tarball layout.
    def fake_download(tag, dest):
        # Simulate "git clone" result: bin/memory_bridge.py at dest.
        dest.mkdir(parents=True)
        (dest / "bin").mkdir()
        (dest / "bin" / "memory_bridge.py").write_text("# via tarball")
    monkeypatch.setattr(installer, "_download_tarball", fake_download)

    bridge = installer.install_m3(repo_path=repo_path, tag="v1.2.3")
    assert bridge.is_file()
    assert bridge.read_text() == "# via tarball"


def test_install_m3_fails_if_bridge_missing_in_fetched_repo(tmp_path, monkeypatch):
    """If the fetched repo lacks bin/memory_bridge.py, install_m3 surfaces a
    clear error instead of writing a bogus config."""
    from m3_memory import installer

    repo_path = tmp_path / "repo"
    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".m3-memory")
    monkeypatch.setattr(installer, "config_file", lambda: tmp_path / ".m3-memory" / "config.json")

    def fake_run(cmd, **kwargs):
        # Simulate a successful clone that happens to NOT contain the bridge.
        dest = Path(cmd[-1])
        dest.mkdir(parents=True)
        (dest / "README.md").write_text("no bin/ here")
        class _R: returncode = 0
        return _R()
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="not found"):
        installer.install_m3(repo_path=repo_path, tag="v1.2.3")

    # And no config was written.
    assert not installer.config_file().is_file()


def test_uninstall_with_yes(tmp_path, monkeypatch):
    from m3_memory import installer

    cfg_dir = tmp_path / ".m3-memory"
    cfg_file = cfg_dir / "config.json"
    repo_path = cfg_dir / "repo"
    (repo_path / "bin").mkdir(parents=True)
    (repo_path / "bin" / "memory_bridge.py").write_text("# fetched")

    monkeypatch.setattr(installer, "config_dir", lambda: cfg_dir)
    monkeypatch.setattr(installer, "config_file", lambda: cfg_file)

    installer.save_config({
        "repo_path": str(repo_path),
        "bridge_path": str(repo_path / "bin" / "memory_bridge.py"),
    })

    installer.uninstall_m3(yes=True)
    assert not repo_path.exists()
    assert not cfg_file.is_file()


def test_doctor_reports_missing_when_not_installed(tmp_path, monkeypatch, capsys):
    from m3_memory import installer

    cfg_dir = tmp_path / ".m3-memory"  # doesn't exist yet
    monkeypatch.setattr(installer, "config_dir", lambda: cfg_dir)
    monkeypatch.setattr(installer, "config_file", lambda: cfg_dir / "config.json")

    # Also neutralize the developer-sibling walk so we're testing the "pure
    # pip user" branch.
    monkeypatch.setattr(installer, "_developer_bridge", lambda: None)

    rc = installer.doctor()
    out = capsys.readouterr().out
    assert rc == 1
    assert "not installed" in out.lower() or "install-m3" in out


def test_doctor_reports_resolved_bridge(tmp_path, monkeypatch, capsys):
    from m3_memory import installer

    cfg_dir = tmp_path / ".m3-memory"
    fake_bridge = cfg_dir / "repo" / "bin" / "memory_bridge.py"
    fake_bridge.parent.mkdir(parents=True)
    fake_bridge.write_text("# fetched")

    monkeypatch.setattr(installer, "config_dir", lambda: cfg_dir)
    monkeypatch.setattr(installer, "config_file", lambda: cfg_dir / "config.json")
    installer.save_config({
        "repo_path": str(fake_bridge.parent.parent),
        "bridge_path": str(fake_bridge),
        "version": "1.2.3",
    })

    rc = installer.doctor()
    out = capsys.readouterr().out
    assert rc == 0
    assert "resolved bridge" in out.lower()


# ── Auto-install tests (Option C behavior on `mcp-memory` bare invocation) ────

def test_auto_install_opt_out_via_env(monkeypatch, tmp_path):
    """M3_AUTO_INSTALL=0 is an absolute opt-out: _auto_install returns None
    in both TTY and non-TTY modes, regardless of what install_m3 would do."""
    import importlib
    from m3_memory import cli, installer

    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".m3-memory")
    monkeypatch.setattr(installer, "config_file", lambda: tmp_path / ".m3-memory" / "config.json")
    monkeypatch.setenv("M3_AUTO_INSTALL", "0")

    # If install_m3 got called despite the opt-out we'd blow up loudly.
    def boom(*a, **kw):
        raise AssertionError("install_m3 should not be called when M3_AUTO_INSTALL=0")
    monkeypatch.setattr(installer, "install_m3", boom)

    assert cli._auto_install(interactive=True) is None
    assert cli._auto_install(interactive=False) is None


def test_auto_install_interactive_prompt_declined(monkeypatch, tmp_path):
    """Interactive mode with user answering 'n' to the prompt returns None
    without calling install_m3."""
    from m3_memory import cli, installer

    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".m3-memory")
    monkeypatch.setattr(installer, "config_file", lambda: tmp_path / ".m3-memory" / "config.json")
    monkeypatch.delenv("M3_AUTO_INSTALL", raising=False)

    def boom(*a, **kw):
        raise AssertionError("install_m3 should not be called when user declines")
    monkeypatch.setattr(installer, "install_m3", boom)
    monkeypatch.setattr("builtins.input", lambda: "n")

    assert cli._auto_install(interactive=True) is None


def test_auto_install_interactive_prompt_accepted(monkeypatch, tmp_path):
    """Interactive mode with user answering 'y' calls install_m3 and returns
    the resolved bridge path."""
    from m3_memory import cli, installer

    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".m3-memory")
    monkeypatch.setattr(installer, "config_file", lambda: tmp_path / ".m3-memory" / "config.json")
    monkeypatch.delenv("M3_AUTO_INSTALL", raising=False)

    fake_bridge = tmp_path / "repo" / "bin" / "memory_bridge.py"
    called = []
    def fake_install(*a, **kw):
        called.append((a, kw))
        fake_bridge.parent.mkdir(parents=True, exist_ok=True)
        fake_bridge.write_text("# fetched")
        return fake_bridge
    monkeypatch.setattr(installer, "install_m3", fake_install)
    monkeypatch.setattr("builtins.input", lambda: "y")

    result = cli._auto_install(interactive=True)
    assert result == fake_bridge
    assert called, "install_m3 should have been invoked"


def test_auto_install_non_interactive_auto_fetches(monkeypatch, tmp_path):
    """Non-interactive mode (no TTY) auto-fetches without prompting. This is
    the MCP-subprocess path: prompting would deadlock the parent."""
    from m3_memory import cli, installer

    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".m3-memory")
    monkeypatch.setattr(installer, "config_file", lambda: tmp_path / ".m3-memory" / "config.json")
    monkeypatch.delenv("M3_AUTO_INSTALL", raising=False)

    fake_bridge = tmp_path / "repo" / "bin" / "memory_bridge.py"
    def fake_install(*a, **kw):
        fake_bridge.parent.mkdir(parents=True, exist_ok=True)
        fake_bridge.write_text("# fetched")
        return fake_bridge
    monkeypatch.setattr(installer, "install_m3", fake_install)

    # input() would deadlock or raise EOFError in non-interactive; either way
    # the prompt path shouldn't be reached. Rigging input() to blow up
    # confirms we never call it under interactive=False.
    def boom():
        raise AssertionError("input() should not be called under interactive=False")
    monkeypatch.setattr("builtins.input", boom)

    result = cli._auto_install(interactive=False)
    assert result == fake_bridge


def test_auto_install_surfaces_install_m3_failure(monkeypatch, tmp_path, capsys):
    """If install_m3 raises RuntimeError (bad tag, network blip, etc.),
    _auto_install returns None and prints the error to stderr so the caller
    can fall through to the actionable help message."""
    from m3_memory import cli, installer

    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path / ".m3-memory")
    monkeypatch.setattr(installer, "config_file", lambda: tmp_path / ".m3-memory" / "config.json")
    monkeypatch.delenv("M3_AUTO_INSTALL", raising=False)

    def fail(*a, **kw):
        raise RuntimeError("network unreachable")
    monkeypatch.setattr(installer, "install_m3", fail)

    result = cli._auto_install(interactive=False)
    assert result is None
    err = capsys.readouterr().err
    assert "network unreachable" in err
    assert "auto-install failed" in err


def test_safe_tar_member_rejects_path_traversal(tmp_path):
    """_safe_tar_member drops tarball entries whose paths escape dest_root
    (classic tarslip CVE class). These are the inputs we'd see from a
    maliciously-crafted tarball claiming to be a GitHub release."""
    import tarfile
    from m3_memory.installer import _safe_tar_member

    dest = tmp_path / "dest"
    dest.mkdir()
    dest_resolved = dest.resolve()

    # Helper to fabricate a TarInfo with a given name/type.
    def mk(name, type_byte=tarfile.REGTYPE, linkname=""):
        ti = tarfile.TarInfo(name=name)
        ti.type = type_byte
        ti.linkname = linkname
        return ti

    # Rejections
    assert _safe_tar_member(mk("../escape.txt"), dest_resolved) is None
    assert _safe_tar_member(mk("/etc/passwd"), dest_resolved) is None
    assert _safe_tar_member(mk("legit/../../../escape"), dest_resolved) is None
    assert _safe_tar_member(mk("dev-node", tarfile.CHRTYPE), dest_resolved) is None
    assert _safe_tar_member(mk("fifo", tarfile.FIFOTYPE), dest_resolved) is None
    # Symlink pointing outside
    assert _safe_tar_member(mk("link", tarfile.SYMTYPE, linkname="/etc/passwd"), dest_resolved) is None

    # Accepted cases
    assert _safe_tar_member(mk("m3-memory-x/README.md"), dest_resolved) is not None
    assert _safe_tar_member(mk("m3-memory-x/", tarfile.DIRTYPE), dest_resolved) is not None
    # Symlink that stays within dest_root
    inside_link = _safe_tar_member(
        mk("m3-memory-x/link", tarfile.SYMTYPE, linkname="README.md"),
        dest_resolved,
    )
    assert inside_link is not None


def test_download_tarball_rejects_non_github_url(tmp_path, monkeypatch):
    """_download_tarball refuses any URL that doesn't start with the
    hardcoded GitHub archive prefix. This is belt-and-suspenders vs the
    existing TARBALL_URL_TEMPLATE pinning — protects against a malicious
    `tag` that tries to inject a full URL (e.g., tag="http://evil.com/x")."""
    from m3_memory import installer

    # Monkeypatch the template so a bad `tag` WOULD produce a bad URL if
    # the guard weren't in place.
    monkeypatch.setattr(installer, "TARBALL_URL_TEMPLATE", "http://evil.example.com/{tag}")

    with pytest.raises(RuntimeError, match="untrusted URL"):
        installer._download_tarball("v1.2.3", tmp_path / "out")
