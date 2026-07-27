"""Regression tests for the 2026-07-27 upgrade defects.

All four were user-visible in ONE `m3 setup` run on a healthy machine:
  1. `m3-embed-server install` failed because the service ALREADY existed, and
     setup reported the embedder "SKIPPED (not installed)" while it was
     Automatic, running, and serving :8082.
  2. The failure advice printed systemd/nohup/crontab — on Windows.
  3. An upgrade replaces the binary on disk but the service keeps running the
     OLD image, with nothing to detect or fix it.
  4. Discovery never probed m3's OWN model dir, so it depended on LM Studio /
     Ollama / llama.cpp staying installed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from m3_memory import embedder_admin as ea  # noqa: E402


# ── 2. OS-correct failure advice ─────────────────────────────────────────────

@pytest.mark.parametrize("platform,expected,forbidden", [
    ("win32", "Administrator", ("crontab", "nohup", "systemd")),
    ("darwin", "launchd", ("crontab", "Administrator terminal")),
    ("linux", "systemd", ("Administrator terminal",)),
])
def test_install_failure_hint_is_os_correct(platform, expected, forbidden):
    """m3 supports 3 OSes; the recovery advice must match the one you're on.

    The original text printed `nohup`/`crontab -e`/`~/.m3/engine/...` on every
    platform, so a Windows user was told to use tools that don't exist there
    for a service that registers with SCM.
    """
    with mock.patch.object(sys, "platform", platform):
        hint = ea._install_failure_hint(Path("/models/bge.gguf"))
    assert expected.lower() in hint.lower()
    for bad in forbidden:
        assert bad.lower() not in hint.lower(), f"{bad!r} leaked into {platform} advice"


# ── 1. install is idempotent when the service already exists ─────────────────

def _fake_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "m3-embed-server.exe"
    binary.write_bytes(b"\x00")
    return binary


def test_service_reports_installed_parses_status(tmp_path, monkeypatch):
    """`status` prints running/stopped when registered, 'not installed' when not.

    Exit code alone is not enough to tell those apart, which is why the opaque
    'IO error in winapi call' was misread as "no service".
    """
    binary = _fake_binary(tmp_path)
    gguf = tmp_path / "m.gguf"

    for stdout, expected in [
        ("running\n", True),
        ("stopped\n", True),
        ("not installed\n", False),
    ]:
        monkeypatch.setattr(
            ea.subprocess, "run",
            lambda *a, _o=stdout, **k: SimpleNamespace(stdout=_o, stderr="", returncode=0),
        )
        assert ea._service_reports_installed(binary, gguf) is expected, stdout


def test_service_reports_installed_survives_a_crash(tmp_path, monkeypatch):
    """A probe that can't run must return False, never raise into setup (§3)."""
    binary = _fake_binary(tmp_path)
    monkeypatch.setattr(ea.subprocess, "run",
                        mock.Mock(side_effect=OSError("boom")))
    assert ea._service_reports_installed(binary, tmp_path / "m.gguf") is False


# ── 3. stale-binary detection after an upgrade ───────────────────────────────

def test_stale_when_process_predates_its_binary(tmp_path, monkeypatch):
    """A process started BEFORE its binary's mtime is running the old image.

    This is the upgrade case: pipx replaces the .exe on disk, Windows keeps the
    original mapped, and the service serves stale code until restarted.
    """
    binary = _fake_binary(tmp_path)
    bin_mtime = binary.stat().st_mtime

    fake_proc = SimpleNamespace(info={
        "name": "m3-embed-server",
        "exe": str(binary),
        "create_time": bin_mtime - 3600,   # started an hour before the upgrade
    })
    fake_psutil = SimpleNamespace(
        process_iter=lambda attrs=None: [fake_proc],
        Error=Exception,
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    assert ea._service_binary_is_stale(binary) is True


def test_not_stale_when_process_started_after_its_binary(tmp_path, monkeypatch):
    """A freshly-restarted service must NOT be flagged — no needless bounce."""
    binary = _fake_binary(tmp_path)
    fake_proc = SimpleNamespace(info={
        "name": "m3-embed-server",
        "exe": str(binary),
        "create_time": binary.stat().st_mtime + 60,
    })
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        process_iter=lambda attrs=None: [fake_proc], Error=Exception))
    assert ea._service_binary_is_stale(binary) is False


def test_not_stale_when_no_matching_process(tmp_path, monkeypatch):
    """Nothing running -> not stale (don't guess)."""
    binary = _fake_binary(tmp_path)
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        process_iter=lambda attrs=None: [], Error=Exception))
    assert ea._service_binary_is_stale(binary) is False


# ── 4. m3's own model dir ────────────────────────────────────────────────────

def test_models_root_precedence(monkeypatch):
    """M3_MODELS_ROOT > M3_MEMORY_ROOT/models > ~/.m3/models — matching the
    established engine/config root contract exactly."""
    from m3_core.paths import get_m3_models_root

    monkeypatch.delenv("M3_MODELS_ROOT", raising=False)
    monkeypatch.delenv("M3_MEMORY_ROOT", raising=False)
    assert get_m3_models_root() == str(Path.home() / ".m3" / "models")

    monkeypatch.setenv("M3_MEMORY_ROOT", str(Path("/srv/m3")))
    assert get_m3_models_root().endswith("models")

    monkeypatch.setenv("M3_MODELS_ROOT", str(Path("/srv/models")))
    assert Path(get_m3_models_root()).name == "models"


def test_m3_models_dir_probed_before_third_party(monkeypatch, tmp_path):
    """m3's own dir must be FIRST. Every other location belongs to a tool the
    user may uninstall (LM Studio, Ollama, llama.cpp)."""
    monkeypatch.setenv("M3_MODELS_ROOT", str(tmp_path))
    import importlib

    import memory.embed as embed
    importlib.reload(embed)

    own = tmp_path / "bge-m3-Q4_K_M.gguf"
    own.write_bytes(b"GGUF" + b"\x00" * 64)

    found = embed.discover_bge_m3_gguf(budget_s=5.0)
    assert found is not None
    assert Path(found).parent == tmp_path, f"third-party dir won: {found}"


def test_invalid_gguf_is_rejected(tmp_path, monkeypatch):
    """An HTML error page or truncated download saved as .gguf must not pass
    as a model — validate MAGIC BYTES, not just the extension."""
    from memory.model_fetch import _is_valid_gguf

    monkeypatch.setenv("M3_MODELS_ROOT", str(tmp_path))
    html = tmp_path / "bad.gguf"
    html.write_bytes(b"<!DOCTYPE html><html>404</html>")
    assert _is_valid_gguf(html) is False

    truncated = tmp_path / "short.gguf"
    truncated.write_bytes(b"GGUF" + b"\x00" * 1000)   # right magic, wrong size
    assert _is_valid_gguf(truncated) is False


def test_fetch_is_opt_out_via_env(monkeypatch):
    """M3_NO_MODEL_DOWNLOAD=1 must suppress the download offer entirely —
    air-gapped and metered-connection installs (§1 local-first)."""
    monkeypatch.setenv("M3_NO_MODEL_DOWNLOAD", "1")
    assert ea._offer_model_download() is None


# ── quiesce progress spinner ─────────────────────────────────────────────────

def test_quiesce_spinner_redraws_one_line(monkeypatch, capsys):
    """The 30s quiesce wait must show motion, and must redraw ONE line with \r
    rather than reprinting it — the same complaint as pipx's whole-line rewrite.
    """
    from m3_memory import setup_wizard as sw

    monkeypatch.setattr(sw.sys.stdout, "isatty", lambda: True, raising=False)
    args = SimpleNamespace(gui_child=False)
    tick = sw._quiesce_tick(args)
    assert tick is not None

    live = [SimpleNamespace(role="dashboard", pid=1)]
    for elapsed in (0.0, 0.25, 0.5, 0.75):
        tick(elapsed, 30.0, live)
    out = capsys.readouterr().out

    assert "\n" not in out, "spinner must not emit newlines (one line, redrawn)"
    assert out.count("\r") >= 4, "each tick must carriage-return"
    assert any(f in out for f in "|/-\\"), "no spinner frame rendered"
    assert "dashboard" in out


def test_quiesce_spinner_disabled_without_a_console(monkeypatch):
    """No TTY (piped output) or a GUI child -> no spinner, so logs and windowless
    runs don't accumulate one line per tick."""
    from m3_memory import setup_wizard as sw

    monkeypatch.setattr(sw.sys.stdout, "isatty", lambda: False, raising=False)
    assert sw._quiesce_tick(SimpleNamespace(gui_child=False)) is None

    monkeypatch.setattr(sw.sys.stdout, "isatty", lambda: True, raising=False)
    assert sw._quiesce_tick(SimpleNamespace(gui_child=True)) is None


def test_wait_for_quiesce_survives_a_raising_tick(monkeypatch):
    """A broken progress renderer must never break the wait itself (§3)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
    import m3_halt

    monkeypatch.setattr(m3_halt, "list_blocking_db_writers",
                        lambda root=None: [])
    # Empty registry returns before any tick; the guard is exercised by the
    # non-empty path below.
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("renderer exploded")

    seq = [[SimpleNamespace(role="x", pid=1)], []]
    monkeypatch.setattr(m3_halt, "list_blocking_db_writers",
                        lambda root=None: seq.pop(0) if seq else [])
    result = m3_halt.wait_for_quiesce(timeout=5.0, poll=0.01, on_tick=_boom)
    assert result.ok is True
    assert calls["n"] >= 1, "on_tick was never invoked"
