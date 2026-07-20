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
    assert set(h) >= {"verdict", "backend", "inference", "cdw", "pipeline", "generated_at"}
    # inference block always present with a known status.
    assert h["inference"]["status"] in (
        "ok", "no_model", "down", "unknown", "none_configured")
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


# ── Inference backend (LLM/SLM) health ────────────────────────────────────────

def _probe(url, backend, *, reachable, queryable, model_loaded, model_id="", detail=""):
    return {"url": url, "backend": backend, "reachable": reachable,
            "queryable": queryable, "model_loaded": model_loaded,
            "model_id": model_id, "detail": detail}


def test_inference_block_reports_where_llm_is_expected(monkeypatch):
    """The block names the resolved endpoint + backend, never a hardcoded port —
    it reads llm_failover.LLM_ENDPOINTS (LM Studio / Ollama / custom)."""
    import dashboard.health as H

    # A served LM Studio endpoint.
    monkeypatch.setattr(H, "_probe_llm_endpoint", lambda ep, *a: _probe(
        ep, "LM Studio", reachable=True, queryable=True, model_loaded=True,
        model_id="qwen/qwen3-8b"))
    import llm_failover as lf
    monkeypatch.setattr(lf, "LLM_ENDPOINTS", ["http://localhost:1234/v1"])
    inf = H._inference_block()
    assert inf["status"] == "ok"
    assert inf["backend"] == "LM Studio"
    assert inf["expected_url"] == "http://localhost:1234/v1"
    assert any(e["model_id"] == "qwen/qwen3-8b" for e in inf["endpoints"])


def test_inference_block_no_model_loaded(monkeypatch):
    """A reachable + queryable server with an empty chat-model list → no_model,
    with a remedy — the exact stall cause the user hit."""
    import dashboard.health as H
    import llm_failover as lf

    monkeypatch.setattr(lf, "LLM_ENDPOINTS", ["http://localhost:1234/v1"])
    monkeypatch.setattr(H, "_probe_llm_endpoint", lambda ep, *a: _probe(
        ep, "LM Studio", reachable=True, queryable=True, model_loaded=False))
    inf = H._inference_block()
    assert inf["status"] == "no_model"
    assert "no chat model is loaded" in inf["remedy"]


def test_inference_block_down(monkeypatch):
    """No endpoint reachable → down, with a start-it remedy."""
    import dashboard.health as H
    import llm_failover as lf

    monkeypatch.setattr(lf, "LLM_ENDPOINTS", ["http://localhost:1234/v1"])
    monkeypatch.setattr(H, "_probe_llm_endpoint", lambda ep, *a: _probe(
        ep, "LM Studio", reachable=False, queryable=False, model_loaded=False,
        detail="ConnectError"))
    inf = H._inference_block()
    assert inf["status"] == "down"
    assert "not reachable" in inf["remedy"].lower() or "no llm backend" in inf["remedy"].lower()


def test_inference_block_reachable_but_unverifiable_is_unknown(monkeypatch):
    """Reachable but /models un-queryable (401) → unknown, NOT a false no_model."""
    import dashboard.health as H
    import llm_failover as lf

    monkeypatch.setattr(lf, "LLM_ENDPOINTS", ["http://localhost:1234/v1"])
    monkeypatch.setattr(H, "_probe_llm_endpoint", lambda ep, *a: _probe(
        ep, "LM Studio", reachable=True, queryable=False, model_loaded=False,
        detail="HTTP 401"))
    inf = H._inference_block()
    assert inf["status"] == "unknown"


def test_inference_block_none_configured(monkeypatch):
    """Empty endpoint list → none_configured with a how-to-configure remedy."""
    import dashboard.health as H
    import llm_failover as lf

    monkeypatch.setattr(lf, "LLM_ENDPOINTS", [])
    inf = H._inference_block()
    assert inf["status"] == "none_configured"
    assert "M3_LLM_URL" in inf["remedy"]


def test_verdict_downgrades_on_inference_stall():
    """no_model backend + a real backlog → red 'INFERENCE BACKEND DOWN'."""
    import dashboard.health as H

    inf = {"status": "no_model", "backend": "LM Studio",
           "expected_url": "http://localhost:1234/v1", "remedy": "load a model"}
    pipe = {"pipelines": [{"label": "Entity extraction", "queue_len": 2215,
                           "eta_human": "stalled"}]}
    v = H._verdict(inference=inf, pipeline=pipe)
    assert v["label"] == "INFERENCE BACKEND DOWN"
    assert v["tone"] == "bad"
    assert any("no model" in r.lower() or "load a model" in r.lower() for r in v["reasons"])


def test_verdict_no_false_alarm_when_backend_dead_but_no_backlog():
    """A dead/empty backend with a DRAINED queue is not a stall — nothing is stuck,
    so the verdict must NOT go red on it."""
    import dashboard.health as H

    inf = {"status": "no_model", "backend": "LM Studio",
           "expected_url": "http://x", "remedy": "load a model"}
    pipe = {"pipelines": [{"label": "Entity extraction", "queue_len": 0,
                           "eta_human": "drained"}]}
    v = H._verdict(inference=inf, pipeline=pipe)
    assert v["label"] != "INFERENCE BACKEND DOWN"
    assert v["tone"] != "bad"


def test_verdict_unknown_inference_is_warn_not_red():
    """Reachable-but-unverifiable + backlog → warn (advisory), never red — we don't
    KNOW the model is missing, so we don't cry wolf."""
    import dashboard.health as H

    inf = {"status": "unknown", "backend": "LM Studio",
           "expected_url": "http://x", "remedy": "confirm a model is loaded"}
    pipe = {"pipelines": [{"label": "E", "queue_len": 500, "eta_human": "stalled"}]}
    v = H._verdict(inference=inf, pipeline=pipe)
    assert v["tone"] != "bad"
    assert v["label"] != "INFERENCE BACKEND DOWN"


def test_health_panel_renders_inference_row():
    """The rendered panel includes the Inference backend section."""
    import dashboard_server as D

    body = D._render_health_panel()
    assert "Inference backend (LLM/SLM)" in body
