"""Tests for `m3 status` / status_summary() — the one-glance health verdict."""
from __future__ import annotations

import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from m3_memory import installer  # noqa: E402


def test_status_summary_shape():
    s = installer.status_summary()
    for k in ("verdict", "installed", "memories", "embedder", "chatlog", "headline"):
        assert k in s
    assert s["verdict"] in ("healthy", "degraded", "broken")


def test_broken_when_not_installed(monkeypatch):
    """No resolvable bridge -> broken verdict pointing at `m3 setup`."""
    monkeypatch.setattr(installer, "find_bridge", lambda: None)
    s = installer.status_summary()
    assert s["installed"] is False
    assert s["verdict"] == "broken"
    assert "m3 setup" in s["headline"]


def test_status_prints_one_line_and_returns_code(monkeypatch):
    """`m3 status` prints a single verdict line; exit 0 only when healthy."""
    monkeypatch.setattr(installer, "find_bridge", lambda: None)  # force broken
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = installer.status()
    out = buf.getvalue()
    assert rc == 1  # broken -> nonzero
    assert out.strip().splitlines()[0].startswith("[X] m3")  # leads with verdict icon


def test_headline_names_the_facts(monkeypatch, tmp_path):
    """When installed, the headline names memories + embedder + chatlog."""
    monkeypatch.setattr(installer, "find_bridge", lambda: tmp_path / "bridge.py")
    (tmp_path / "bridge.py").write_text("# bridge")
    s = installer.status_summary()
    assert s["installed"] is True
    # headline mentions the three subsystems regardless of exact values
    assert "memories" in s["headline"]
    assert "embedder:" in s["headline"]
    assert "chatlog:" in s["headline"]
