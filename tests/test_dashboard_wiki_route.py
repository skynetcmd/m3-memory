"""Tests for the dashboard's /wiki route.

The Wiki tab shows the generated self-contained vault when present, and OS-specific
"how to generate it" instructions when not. Drives the real FastAPI app with a
TestClient. Requires the [dashboard] extra (fastapi); skipped cleanly if absent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

pytest.importorskip("fastapi", reason="dashboard needs the [dashboard] extra")

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover
    from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A dashboard TestClient whose engine root is an isolated tmp dir, so the
    wiki's presence/absence is fully controlled by the test."""
    monkeypatch.setenv("M3_ENGINE_ROOT", str(tmp_path))
    # Point the DB at a throwaway sqlite so app startup doesn't touch real data.
    db = tmp_path / "agent_memory.db"
    import sqlite3
    sqlite3.connect(str(db)).close()
    monkeypatch.setenv("M3_DATABASE", str(db))
    import dashboard_server
    return TestClient(dashboard_server.app), tmp_path


def test_wiki_absent_shows_install_instructions(client):
    c, _root = client
    r = c.get("/wiki")
    assert r.status_code == 200
    body = r.text
    assert "No wiki generated yet" in body
    # OS-variant guidance for every platform.
    for os_name in ("Windows", "macOS", "Linux"):
        assert os_name in body
    assert "m3 wiki generate --html" in body
    # /wiki/raw 404s when nothing is generated.
    assert c.get("/wiki/raw").status_code == 404


def test_wiki_present_embeds_viewer(client):
    c, root = client
    wiki_dir = root / "wiki"
    wiki_dir.mkdir()
    (wiki_dir / "wiki.html").write_text(
        "<!doctype html><title>m3 Wiki</title><body>VAULT-CONTENT</body>",
        encoding="utf-8",
    )
    r = c.get("/wiki")
    assert r.status_code == 200
    assert "iframe" in r.text and "/wiki/raw" in r.text
    assert "No wiki generated yet" not in r.text
    # The raw route serves the actual vault html.
    raw = c.get("/wiki/raw")
    assert raw.status_code == 200
    assert "VAULT-CONTENT" in raw.text


def test_wiki_nav_link_on_every_page(client):
    """The Wiki tab appears in the shared header, so it's reachable everywhere."""
    c, _root = client
    for path in ("/", "/browse", "/audit", "/wiki"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert ">Wiki</a>" in r.text, f"Wiki nav link missing on {path}"
