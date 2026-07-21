"""Tests for m3 doctor's duplicate-MCP-registration detector.

A server declared in >1 config file that the SAME client reads gets launched
twice — the root cause of the duplicate memory_bridge that hung a memory_write
for ~2h43m (2026-07-02). The detector must catch that, but must NOT flag the
same server registered across DIFFERENT clients (Claude/Gemini/Antigravity each
registering `memory` is normal multi-client use, not a bug).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from m3_memory import installer as I  # noqa: E402


def _write(p: Path, servers: list[str]) -> None:
    p.write_text(json.dumps({"mcpServers": {s: {} for s in servers}}), encoding="utf-8")


def test_same_client_duplicate_is_flagged(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    mcp = tmp_path / ".mcp.json"
    _write(settings, ["memory", "grok_intel"])
    _write(mcp, ["memory"])  # memory in BOTH sources of one client
    monkeypatch.setattr(I, "_client_config_sources",
                        lambda: {"Claude Code": [settings, mcp]})

    dupes = I._duplicate_mcp_registration()
    assert "Claude Code" in dupes
    assert "memory" in dupes["Claude Code"]
    assert len(dupes["Claude Code"]["memory"]) == 2
    assert "grok_intel" not in dupes["Claude Code"]  # only in one file -> fine


def test_cross_client_same_server_is_NOT_flagged(tmp_path, monkeypatch):
    a = tmp_path / "claude.json"
    b = tmp_path / "gemini.json"
    c = tmp_path / "antigravity.json"
    for f in (a, b, c):
        _write(f, ["memory"])  # same server, but each is a DIFFERENT client
    monkeypatch.setattr(I, "_client_config_sources", lambda: {
        "Claude Code": [a], "Gemini CLI": [b], "Antigravity": [c],
    })
    dupes = I._duplicate_mcp_registration()
    assert dupes == {}, f"cross-client registration wrongly flagged: {dupes}"


def test_clean_config_returns_empty(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    mcp = tmp_path / ".mcp.json"
    _write(settings, ["memory"])
    _write(mcp, [])  # deduped — .mcp.json empty
    monkeypatch.setattr(I, "_client_config_sources",
                        lambda: {"Claude Code": [settings, mcp]})
    assert I._duplicate_mcp_registration() == {}


def test_unreadable_file_skipped(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    bad = tmp_path / ".mcp.json"
    _write(settings, ["memory"])
    bad.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(I, "_client_config_sources",
                        lambda: {"Claude Code": [settings, bad]})
    # must not raise, and the unreadable file can't contribute a duplicate
    assert I._duplicate_mcp_registration() == {}


def test_section_renders_without_error(monkeypatch):
    # With no duplicates, the section prints nothing and must not raise.
    monkeypatch.setattr(I, "_duplicate_mcp_registration", lambda: {})
    monkeypatch.setattr(I, "_live_bridge_counts", lambda: {})
    I._duplicate_registration_section()  # no exception = pass


# ── _live_bridge_counts: shim-aware, cross-OS (Windows shim / POSIX direct) ──────
class _FakeProc:
    def __init__(self, pid, ppid, cmdline):
        self.info = {"pid": pid, "ppid": ppid, "cmdline": cmdline}


def _fake_psutil(procs):
    class _P:
        @staticmethod
        def process_iter(_attrs):
            return list(procs)
    return _P


def test_bridge_count_windows_shim_worker_counts_as_one(monkeypatch):
    """Windows: a bridge is a venv SHIM (parent) + its re-exec'd WORKER (child),
    both carrying <bridge>.py. The pair is ONE logical bridge — must NOT flag."""
    procs = [
        _FakeProc(100, 5, ["python.exe", "grok_bridge.py"]),   # shim, parent=client
        _FakeProc(101, 100, ["python.exe", "grok_bridge.py"]),  # worker, parent=shim
    ]
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(procs))
    assert I._live_bridge_counts() == {}


def test_bridge_count_posix_single_process_counts_as_one(monkeypatch):
    """macOS/Linux: no venv shim — a bridge is ONE process whose parent is the
    MCP client (not a bridge). Must NOT flag."""
    procs = [_FakeProc(200, 5, ["python", "grok_bridge.py"])]  # parent=client
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(procs))
    assert I._live_bridge_counts() == {}


def test_bridge_count_genuine_double_launch_is_flagged(monkeypatch):
    """A REAL double-launch = two independent trees (neither parent a bridge).
    Must flag 2x — the fix must not blind the check to real duplicates."""
    procs = [
        _FakeProc(100, 5, ["python", "grok_bridge.py"]),    # tree 1 root
        _FakeProc(101, 100, ["python", "grok_bridge.py"]),  # tree 1 worker (shim child)
        _FakeProc(200, 6, ["python", "grok_bridge.py"]),    # tree 2 root — 2nd launch
        _FakeProc(201, 200, ["python", "grok_bridge.py"]),  # tree 2 worker
    ]
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(procs))
    assert I._live_bridge_counts() == {"grok_bridge.py": 2}


def test_bridge_count_no_psutil_returns_empty(monkeypatch):
    """psutil absent → best-effort empty (the config check is the primary signal),
    never a crash."""
    import builtins
    real_import = builtins.__import__

    def _no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **k)

    monkeypatch.delitem(sys.modules, "psutil", raising=False)
    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    assert I._live_bridge_counts() == {}
