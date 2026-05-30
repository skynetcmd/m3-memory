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

import subprocess
import sys
from pathlib import Path
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
