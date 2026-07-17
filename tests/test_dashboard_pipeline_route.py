"""End-to-end test for the /api/pipeline dashboard route.

Drives the real FastAPI app with a TestClient against a seeded temp DB and
asserts the rendered panel contains the governor line, per-queue cards, the
1/10/30/60-min throughput, and a drain ETA. Requires the [web] extra (fastapi);
skipped cleanly if it's not installed.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

pytest.importorskip("fastapi", reason="dashboard needs the [web] extra")
starlette_testclient = pytest.importorskip("starlette.testclient")


def _seed_db(tmp_path) -> str:
    db = str(tmp_path / "dash.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE observation_queue (id INTEGER PRIMARY KEY, enqueued_at TEXT);
        CREATE TABLE reflector_queue (id INTEGER PRIMARY KEY, enqueued_at TEXT);
        CREATE TABLE memory_embeddings (id TEXT PRIMARY KEY, memory_id TEXT,
            created_at TEXT);
        CREATE TABLE memory_items (id TEXT PRIMARY KEY, type TEXT, created_at TEXT);
        """
    )
    # 3 embeddings produced "now" -> nonzero recent throughput -> a real ETA.
    conn.execute("INSERT INTO memory_embeddings (id,memory_id,created_at) "
                 "SELECT 'e'||value, 'm', datetime('now') "
                 "FROM (SELECT 1 value UNION SELECT 2 UNION SELECT 3)")
    conn.commit()
    conn.close()
    return db


def test_pipeline_route_renders_panel(tmp_path, monkeypatch):
    db = _seed_db(tmp_path)
    monkeypatch.setenv("M3_DATABASE", db)

    import dashboard_server as d
    client = starlette_testclient.TestClient(d.app)
    resp = client.get("/api/pipeline")

    assert resp.status_code == 200
    html = resp.text
    assert "Governor" in html
    # queue cards for both pipelines
    assert "Enrichment queue" in html
    assert "Reflection queue" in html
    # throughput windows are shown
    for w in ("1m:", "10m:", "30m:", "60m:"):
        assert w in html
    # drain ETA line present
    assert "drain:" in html
