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
        "ok", "failover_active", "no_model", "auth_failed", "down", "unknown",
        "none_configured")
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

    # The /health PAGE now renders an instant skeleton and fetches the real panel
    # from /api/health on load (collect_health() pings the inference endpoint and
    # is slow, so it no longer blocks the page). The page shell shows the loading
    # placeholder; the data lives in the partial.
    page = client.get("/health")
    assert page.status_code == 200
    body = page.text
    assert "System Health" in body                 # nav tab
    assert "Gathering system health" in body       # instant loading skeleton
    assert "/api/health" in body                   # async fetch is wired

    # The actual health data — verdict pill, report time, stores — is the partial.
    partial = client.get("/api/health")
    assert partial.status_code == 200
    pbody = partial.text
    assert any(v in pbody for v in
               ("HEALTHY", "THROTTLED", "REDUCED PERFORMANCE", "NEEDS SETUP", "UNKNOWN"))
    assert "report time:" in pbody
    assert "Database backend" in pbody


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

def _probe(url, backend, *, reachable, queryable, model_loaded, model_id="",
           detail="", loaded_confirmed=None):
    # loaded_confirmed defaults to model_loaded so a "model present" probe takes the
    # cache-safe (state-verified) path and does NOT trigger a real completion smoke.
    if loaded_confirmed is None:
        loaded_confirmed = model_loaded
    return {"url": url, "backend": backend, "reachable": reachable,
            "queryable": queryable, "model_loaded": model_loaded,
            "loaded_confirmed": loaded_confirmed, "model_id": model_id, "detail": detail}


@pytest.fixture(autouse=True)
def _clear_inference_caches():
    """The inference block + smoke are cached; clear both between tests so mocks
    don't leak across cases."""
    import dashboard.health as H
    H._BLOCK_CACHE.clear()
    H._SMOKE_CACHE.clear()
    yield
    H._BLOCK_CACHE.clear()
    H._SMOKE_CACHE.clear()


def test_inference_block_reports_where_llm_is_expected(monkeypatch):
    """The block names the resolved endpoint + backend, never a hardcoded port —
    it reads llm_failover.LLM_ENDPOINTS (LM Studio / Ollama / custom)."""
    import dashboard.health as H

    # A served, state-verified LM Studio endpoint (cache-safe path, no smoke).
    monkeypatch.setattr(H, "_probe_llm_endpoint", lambda ep, *a: _probe(
        ep, "LM Studio", reachable=True, queryable=True, model_loaded=True,
        model_id="qwen/qwen3-8b", loaded_confirmed=True))
    import llm_failover as lf
    monkeypatch.setattr(lf, "LLM_ENDPOINTS", ["http://localhost:1234/v1"])
    inf = H._inference_block_uncached()
    assert inf["status"] == "ok"
    assert inf["backend"] == "LM Studio"
    assert inf["expected_url"] == "http://localhost:1234/v1"
    assert inf["model_id"] == "qwen/qwen3-8b"
    # Verified via the cache-safe state signal — NOT a real completion.
    assert inf["chain"][0]["verified"] == "state"


def test_inference_block_no_model_loaded(monkeypatch):
    """A reachable + queryable server with no loaded chat model → no_model."""
    import dashboard.health as H
    import llm_failover as lf

    monkeypatch.setattr(lf, "LLM_ENDPOINTS", ["http://localhost:1234/v1"])
    monkeypatch.setattr(H, "_probe_llm_endpoint", lambda ep, *a: _probe(
        ep, "LM Studio", reachable=True, queryable=True, model_loaded=False))
    inf = H._inference_block_uncached()
    assert inf["status"] == "no_model"
    assert "no chat model" in inf["remedy"].lower()


def test_inference_block_down(monkeypatch):
    """No endpoint reachable → down, with a start-it remedy."""
    import dashboard.health as H
    import llm_failover as lf

    monkeypatch.setattr(lf, "LLM_ENDPOINTS", ["http://localhost:1234/v1"])
    monkeypatch.setattr(H, "_probe_llm_endpoint", lambda ep, *a: _probe(
        ep, "LM Studio", reachable=False, queryable=False, model_loaded=False,
        detail="ConnectError"))
    inf = H._inference_block_uncached()
    assert inf["status"] == "down"
    assert "no llm backend" in inf["remedy"].lower() or "start" in inf["remedy"].lower()


def test_inference_block_auth_failed_when_models_unreadable(monkeypatch):
    """Reachable but /models rejected auth (401/403) → the chain hop fails auth;
    a single-endpoint chain with only that failure surfaces as down/auth, never a
    false 'no model loaded'."""
    import dashboard.health as H
    import llm_failover as lf

    monkeypatch.setattr(lf, "LLM_ENDPOINTS", ["http://localhost:1234/v1"])
    monkeypatch.setattr(H, "_probe_llm_endpoint", lambda ep, *a: _probe(
        ep, "LM Studio", reachable=True, queryable=False, model_loaded=False,
        detail="HTTP 401 (auth rejected)"))
    inf = H._inference_block_uncached()
    assert inf["status"] in ("auth_failed", "down")
    # The important guarantee: it must NOT falsely claim a model is missing.
    assert inf["status"] != "no_model"


def test_inference_block_none_configured(monkeypatch):
    """Empty endpoint list → none_configured with a how-to-configure remedy."""
    import dashboard.health as H
    import llm_failover as lf

    monkeypatch.setattr(lf, "LLM_ENDPOINTS", [])
    inf = H._inference_block_uncached()
    assert inf["status"] == "none_configured"
    assert "M3_LLM_URL" in inf["remedy"]


def test_inference_block_failover_active(monkeypatch):
    """Primary endpoint down, secondary serves → failover_active (warn), naming the
    failed primary. Mirrors get_best_llm falling through to the next endpoint."""
    import dashboard.health as H
    import llm_failover as lf

    monkeypatch.setattr(lf, "LLM_ENDPOINTS",
                        ["http://localhost:1234/v1", "http://localhost:11434/v1"])

    def _fake_probe(ep, *a):
        if ":1234" in ep:  # primary down
            return _probe(ep, "LM Studio", reachable=False, queryable=False,
                          model_loaded=False, detail="ConnectError")
        return _probe(ep, "Ollama", reachable=True, queryable=True,       # secondary serves
                      model_loaded=True, model_id="llama3.2:3b", loaded_confirmed=True)

    monkeypatch.setattr(H, "_probe_llm_endpoint", _fake_probe)
    inf = H._inference_block_uncached()
    assert inf["status"] == "failover_active"
    assert inf["backend"] == "Ollama"                # landed on the secondary
    assert "1234" in inf["remedy"]                   # names the failed primary
    assert "failed over" in inf["remedy"].lower()    # the failover is spelled out


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


def test_llm_token_reuses_m3_key_resolution(monkeypatch):
    """The probe must authenticate with m3's OWN token (auth_utils.get_api_key on
    the LM_API_TOKEN service, LM Studio's 'lm-studio' fallback) — NOT an invented
    env var — so it sees exactly what m3's real LLM calls see."""
    import auth_utils
    import dashboard.health as H

    # When the secret resolves, that value is used verbatim.
    monkeypatch.setattr(auth_utils, "get_api_key",
                        lambda service: "sk-lm-secret" if service == "LM_API_TOKEN" else None)
    assert H._llm_token() == "sk-lm-secret"

    # When it doesn't resolve, fall back to LM Studio's conventional placeholder
    # (the same 'lm-studio' default memory_core/custom_tool_bridge use).
    monkeypatch.setattr(auth_utils, "get_api_key", lambda service: None)
    assert H._llm_token() == "lm-studio"


class _R:
    """Tiny httpx.Response stand-in for URL-routed GET mocks."""
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
    def json(self):
        return self._payload


def _routed_get(routes, connect_error_urls=()):
    """Build a fake httpx.get that returns routes[path-substring] or 404, and raises
    for any URL containing a connect_error_urls fragment."""
    def _get(url, headers=None, timeout=None):
        for frag in connect_error_urls:
            if frag in url:
                raise __import__("httpx").ConnectError("refused")
        for frag, resp in routes.items():
            if frag in url:
                return resp
        return _R(404, {})
    return _get


def test_probe_sends_m3_token(monkeypatch):
    """Every probe GET carries the m3 token as a Bearer header, so an auth'd backend
    (LM Studio, or latest Ollama with auth) is not falsely 401'd."""
    import dashboard.health as H
    import httpx

    monkeypatch.setattr(H, "_llm_token", lambda: "sk-m3-token")
    seen_auth = []

    def _get(url, headers=None, timeout=None):
        seen_auth.append((headers or {}).get("Authorization"))
        # Reachable OpenAI-compat with a listed model, no native endpoints.
        if url.endswith("/v1/models"):
            return _R(200, {"data": [{"id": "qwen/qwen3-8b"}]})
        return _R(404, {})

    monkeypatch.setattr(httpx, "get", _get)
    out = H._probe_llm_endpoint("http://localhost:9999/v1", 0.3, 4.0)
    assert all(a == "Bearer sk-m3-token" for a in seen_auth if a is not None)
    assert out["model_loaded"] and out["model_id"] == "qwen/qwen3-8b"


def test_probe_parses_ollama_native_models_shape(monkeypatch):
    """Latest Ollama's /v1/models may return {'models': [...]} rather than the
    OpenAI {'data': [...]}. The probe (like m3's own parser) handles both. Here
    /api/ps returns empty (cold) so it falls through to /v1/models."""
    import dashboard.health as H
    import httpx

    monkeypatch.setattr(H, "_llm_token", lambda: "x")
    routes = {
        "/api/ps": _R(200, {"models": []}),                    # reachable but cold
        "/v1/models": _R(200, {"models": [{"model": "llama3.2:3b"}]}),  # native shape
    }
    monkeypatch.setattr(httpx, "get", _routed_get(routes))
    out = H._probe_llm_endpoint("http://localhost:11434/v1", 0.3, 4.0)
    assert out["queryable"] and out["model_loaded"]
    assert out["model_id"] == "llama3.2:3b"


# ── Cache-safe provider-native readiness signals (no inference) ────────────────

def test_lmstudio_state_loaded_is_cache_safe(monkeypatch):
    """LM Studio /api/v0/models with state=='loaded' proves ready WITHOUT any
    completion — loaded_confirmed set, no /chat/completions ever sent."""
    import dashboard.health as H
    import httpx

    monkeypatch.setattr(H, "_llm_token", lambda: "x")
    monkeypatch.setattr(httpx, "get", _routed_get({
        "/api/v0/models": _R(200, {"data": [{"id": "qwen/qwen3.5-9b",
                                             "type": "vlm", "state": "loaded"}]}),
    }))
    # Guard: no completion smoke may be sent.
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("cache-safe path must NOT POST a completion")))
    out = H._probe_llm_endpoint("http://localhost:1234/v1", 0.3, 4.0)
    assert out["loaded_confirmed"] and out["model_loaded"]
    assert out["model_id"] == "qwen/qwen3.5-9b"


def test_ollama_api_ps_loaded_is_cache_safe(monkeypatch):
    """Ollama /api/ps listing a loaded model → loaded_confirmed, no inference."""
    import dashboard.health as H
    import httpx

    monkeypatch.setattr(H, "_llm_token", lambda: "x")
    monkeypatch.setattr(httpx, "get", _routed_get({
        "/api/ps": _R(200, {"models": [{"name": "llama3.2:3b"}]}),
    }))
    out = H._probe_llm_endpoint("http://localhost:11434/v1", 0.3, 4.0)
    assert out["loaded_confirmed"] and out["model_id"] == "llama3.2:3b"
    assert "api/ps" in out["detail"]


def test_llamacpp_health_ok_is_cache_safe(monkeypatch):
    """llama.cpp /health → 200 {'status':'ok'} proves a model is loaded, no
    inference. A custom port (not 1234/11434) exercises the generic path."""
    import dashboard.health as H
    import httpx

    monkeypatch.setattr(H, "_llm_token", lambda: "x")
    monkeypatch.setattr(httpx, "get", _routed_get({
        "/health": _R(200, {"status": "ok"}),
    }))
    out = H._probe_llm_endpoint("http://localhost:8080/v1", 0.3, 4.0)
    assert out["loaded_confirmed"] and out["model_loaded"]
    assert "llama.cpp" in out["detail"]


def test_llamacpp_health_loading_not_ready(monkeypatch):
    """llama.cpp /health 503 {'status':'loading model'} → reachable but not ready
    (not model_loaded), so failover would skip it — no false 'serving'."""
    import dashboard.health as H
    import httpx

    monkeypatch.setattr(H, "_llm_token", lambda: "x")
    monkeypatch.setattr(httpx, "get", _routed_get({
        "/health": _R(503, {"status": "loading model"}),
    }))
    out = H._probe_llm_endpoint("http://localhost:8080/v1", 0.3, 4.0)
    assert out["reachable"] and not out["model_loaded"]


def test_smoke_used_only_when_no_cache_safe_signal(monkeypatch):
    """A bare OpenAI-compat server (no /api/v0/models, no /api/ps, no /health) that
    LISTS a model → the block falls back to ONE real completion to verify it can
    serve. Confirms the smoke is the LAST resort (only when no cache-safe signal).
    The real-completion smoke is OPT-IN (default off, to never bill an unproven
    endpoint), so this test enables it explicitly."""
    import dashboard.health as H
    import httpx
    import llm_failover as lf

    monkeypatch.setenv("M3_DASHBOARD_LLM_SMOKE", "1")
    monkeypatch.setattr(H, "_llm_token", lambda: "x")
    monkeypatch.setattr(lf, "LLM_ENDPOINTS", ["http://localhost:9999/v1"])
    # Only /v1/models answers; native readiness endpoints 404.
    monkeypatch.setattr(httpx, "get", _routed_get({
        "/v1/models": _R(200, {"data": [{"id": "some-model"}]}),
    }))
    posts = []

    def _post(url, json=None, headers=None, timeout=None):
        posts.append(url)
        return _R(200, {"model": "some-model",
                        "choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(httpx, "post", _post)
    inf = H._inference_block_uncached()
    assert inf["status"] == "ok"
    assert len(posts) == 1 and posts[0].endswith("/chat/completions")
    assert inf["chain"][0]["verified"] == "inference"


def test_block_cache_short_ttl(monkeypatch):
    """The whole block is short-TTL cached so rapid /api/health polls don't re-probe
    every few seconds; a second immediate call is served from cache."""
    import dashboard.health as H

    calls = []
    monkeypatch.setattr(H, "_inference_block_uncached",
                        lambda: (calls.append(1) or {"status": "ok", "endpoints": []}))
    H._BLOCK_CACHE.clear()
    a = H._inference_block()
    b = H._inference_block()
    assert len(calls) == 1                 # second call hit the cache
    assert a["block_cached"] is False and b["block_cached"] is True


# ── Cloud / frontier models: PING-ONLY, never a billed completion ──────────────

def test_cloud_url_detection():
    """Anthropic/Gemini/OpenAI/xAI hosts (and https non-loopback) are cloud;
    localhost/LAN-http are not."""
    import dashboard.health as H

    for u in ("https://api.anthropic.com/v1/messages",
              "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
              "https://api.openai.com/v1/chat/completions",
              "https://api.x.ai/v1/chat/completions"):
        assert H._is_cloud_url(u), u
    for u in ("http://localhost:1234/v1", "http://127.0.0.1:11434/v1"):
        assert not H._is_cloud_url(u), u


def test_cloud_ping_ok_never_completes(monkeypatch):
    """A cloud endpoint with a resolvable key + reachable /models → ok via PING
    ONLY. No /chat/completions or /messages POST may ever be sent (no token cost)."""
    import auth_utils
    import dashboard.health as H
    import httpx

    monkeypatch.setattr(auth_utils, "get_api_key",
                        lambda svc: "sk-ant-real" if svc == "ANTHROPIC_API_KEY" else None)
    seen = {}

    def _get(url, headers=None, timeout=None):
        seen["url"] = url
        seen["auth_style"] = "x-api-key" if "x-api-key" in (headers or {}) else "bearer"
        return _R(200, {"data": [{"id": "claude-haiku-4-5"}]})

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("cloud must NOT be smoked with a completion")))
    out = H._probe_cloud_endpoint("https://api.anthropic.com/v1/messages",
                                  "ANTHROPIC_API_KEY", 0.3, 4.0)
    assert out["reachable"] and out["loaded_confirmed"]
    assert out["backend"] == "Anthropic"
    assert seen["auth_style"] == "x-api-key"     # Anthropic wire auth, not Bearer
    assert seen["url"].endswith("/models")


def test_cloud_missing_key_is_auth_failed(monkeypatch):
    """No API key resolves for the profile's service → auth_missing, surfaced as a
    clear 'set <service>' remedy — never a false 'model not loaded'."""
    import auth_utils
    import dashboard.health as H
    import httpx

    monkeypatch.setattr(auth_utils, "get_api_key", lambda svc: None)
    # Must not even hit the network without a key.
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not call the API without a key")))
    out = H._probe_cloud_endpoint("https://api.openai.com/v1/chat/completions",
                                  "OPENAI_API_KEY", 0.3, 4.0)
    assert out["auth_missing"] and not out["reachable"]
    assert "OPENAI_API_KEY" in out["detail"]


def test_cloud_rejected_key_is_auth_failed(monkeypatch):
    """A resolvable key that the API rejects (401) → auth_rejected."""
    import auth_utils
    import dashboard.health as H
    import httpx

    monkeypatch.setattr(auth_utils, "get_api_key", lambda svc: "sk-bad")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R(401, {}))
    out = H._probe_cloud_endpoint("https://api.x.ai/v1/chat/completions",
                                  "XAI_API_KEY", 0.3, 4.0)
    assert out.get("auth_rejected") and out["reachable"]


def test_inference_block_cloud_endpoint_never_smokes(monkeypatch):
    """End-to-end: LLM_ENDPOINTS pointing at a cloud host → block reports ok via
    ping, and the completion smoke is never invoked."""
    import auth_utils
    import dashboard.health as H
    import httpx
    import llm_failover as lf

    monkeypatch.setattr(lf, "LLM_ENDPOINTS", ["https://api.anthropic.com/v1/messages"])
    monkeypatch.setattr(auth_utils, "get_api_key",
                        lambda svc: "sk-ant-real")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R(200, {"data": [{"id": "claude-haiku-4-5"}]}))
    monkeypatch.setattr(H, "_smoke_llm_completion", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("cloud endpoint must never be smoked")))
    inf = H._inference_block_uncached()
    assert inf["status"] == "ok"
    assert inf["backend"] == "Anthropic"
