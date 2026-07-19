"""End-to-end tests for the dashboard's System Health view.

Covers the backend-agnostic health collector (dashboard.health.collect_health)
and the /health page + /api/health partial routes. Drives the real FastAPI app
with a TestClient. Requires the [dashboard] extra (fastapi); skipped cleanly if
absent, so a bare install doesn't error at collection.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

pytest.importorskip("fastapi", reason="dashboard needs the [dashboard] extra")


def test_fmt_dual_time_local_and_zulu():
    """A timestamp renders as 'LOCAL (…Z)' — local time with Zulu in parens."""
    from dashboard.health import _fmt_dual_time

    out = _fmt_dual_time("2026-07-19T15:10:47")
    assert out.endswith("(2026-07-19T15:10:47Z)"), out
    assert "2026-07-19" in out
    # Empty / None degrade to a dash, never raise.
    assert _fmt_dual_time(None) == "—"
    assert _fmt_dual_time("") == "—"
    # Garbage is returned stringified, not raised.
    assert _fmt_dual_time("not-a-date") == "not-a-date"


def test_collect_health_shape():
    """collect_health returns the expected structured snapshot, never raises."""
    from dashboard.health import collect_health

    h = collect_health()
    assert set(h) >= {"verdict", "backend", "cdw", "pipeline", "generated_at"}
    v = h["verdict"]
    # verdict carries the raw contract value AND a user-facing label + tone.
    assert "verdict" in v
    assert v.get("label")  # e.g. HEALTHY / THROTTLED (RAM) / REDUCED PERFORMANCE
    assert v.get("tone") in ("ok", "warn", "bad")
    # The scary word is never used as the user-facing label.
    assert "DEGRADED" not in v["label"]
    assert "backend" in h["backend"]
    assert isinstance(h["backend"]["stores"], list)
    # generated_at is dual-time formatted.
    assert h["generated_at"].endswith("Z)")


def test_verdict_never_says_degraded():
    """A throttle/perf state must read as THROTTLED/REDUCED PERFORMANCE, never the
    integrity-implying word 'DEGRADED' — anywhere in the rendered panel."""
    import dashboard_server as D

    body = D._render_health_panel()
    assert "DEGRADED" not in body, "panel must not surface the word DEGRADED"


def test_health_routes_render():
    """/health page and /api/health partial both return 200 with the panel."""
    import dashboard_server as D
    from starlette.testclient import TestClient

    client = TestClient(D.app)

    page = client.get("/health")
    assert page.status_code == 200
    body = page.text
    # Nav tab + verdict pill + the panel title are all present.
    assert "System Health" in body
    assert any(v in body for v in
               ("HEALTHY", "THROTTLED", "REDUCED PERFORMANCE", "NEEDS SETUP", "UNKNOWN"))
    # Report time is noted; backend name uses tall-man casing (SQLite/PostgreSQL).
    assert "report time:" in body

    partial = client.get("/api/health")
    assert partial.status_code == 200
    assert "Database backend" in partial.text


def test_existing_tabs_still_render_with_health_nav():
    """Adding the health nav var didn't break the other tabs' header .format()."""
    import dashboard_server as D
    from starlette.testclient import TestClient

    client = TestClient(D.app)
    for path in ("/", "/browse", "/audit"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} → {r.status_code}"
        assert "System Health" in r.text  # the new nav link appears on every page
