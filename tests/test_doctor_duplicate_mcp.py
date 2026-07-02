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
