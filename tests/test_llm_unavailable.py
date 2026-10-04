"""A local LLM server that cannot answer is reported as a SYSTEM state.

Measured 2026-10-03 on SkyPC: LM Studio was up on :1234 with no model loaded.
Every chat call returned `400 "No models loaded"`, logged only as a bare
"400 Bad Request"; distillation retried the same task every cycle, and entity
extraction charged each row a retry attempt until it was excluded for good.

These pin: usability is probed and cached; a failed call re-checks usability
and raises LLMUnavailable only when no endpoint can answer; the server's error
body reaches the exception; the healthy path makes no extra request; and the
outage is reported once, not per call. Hermetic: httpx.MockTransport only.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import llm_failover  # noqa: E402

EP = "http://127.0.0.1:1234/v1"


@pytest.fixture(autouse=True)
def _isolated_failover_state(monkeypatch):
    monkeypatch.setattr(llm_failover, "LLM_ENDPOINTS", [EP])
    monkeypatch.setattr(llm_failover, "_USABILITY_CACHE", {})
    monkeypatch.setattr(llm_failover, "_OUTAGE_STATE", {})
    monkeypatch.setattr(llm_failover, "_default_lm_token", lambda: None)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _models(*ids):
    return httpx.Response(200, json={"data": [{"id": i} for i in ids]})


# ── probe + cache ────────────────────────────────────────────────────────────

def test_empty_model_list_is_unusable():
    async def run():
        async with _client(lambda r: _models()) as c:
            return await llm_failover.probe_usability(c, EP)
    usable, detail = asyncio.run(run())
    assert usable is False and "no generative model" in detail


def test_an_embedder_alone_is_unusable():
    async def run():
        async with _client(lambda r: _models("text-embedding-bge-m3")) as c:
            return await llm_failover.probe_usability(c, EP)
    assert asyncio.run(run())[0] is False


def test_a_listed_model_is_usable():
    async def run():
        async with _client(lambda r: _models("qwen/qwen3-8b")) as c:
            return await llm_failover.probe_usability(c, EP)
    usable, detail = asyncio.run(run())
    assert usable is True and "qwen3-8b" in detail


def test_unreachable_is_unusable_not_a_crash():
    def handler(r):
        raise httpx.ConnectError("refused", request=r)

    async def run():
        async with _client(handler) as c:
            return await llm_failover.probe_usability(c, EP)
    usable, detail = asyncio.run(run())
    assert usable is False and "ConnectError" in detail


def test_probe_is_cached_and_fresh_bypasses_it():
    calls = []

    def handler(r):
        calls.append(r.url.path)
        return _models()

    async def run():
        async with _client(handler) as c:
            await llm_failover.probe_usability(c, EP)
            await llm_failover.probe_usability(c, EP)
            assert len(calls) == 1, "second probe should be served from the cache"
            await llm_failover.probe_usability(c, EP, fresh=True)
            assert len(calls) == 2, "fresh=True must re-ask the server"
    asyncio.run(run())


def test_first_usable_endpoint_cached_skips_known_unusable(monkeypatch):
    other = "http://127.0.0.1:11434/v1"
    monkeypatch.setattr(llm_failover, "LLM_ENDPOINTS", [EP, other])
    assert llm_failover.first_usable_endpoint_cached() == EP, "unprobed counts as a candidate"
    llm_failover._USABILITY_CACHE[EP] = (llm_failover.time.monotonic(), False, "none loaded")
    assert llm_failover.first_usable_endpoint_cached() == other
    llm_failover._USABILITY_CACHE[other] = (llm_failover.time.monotonic(), False, "down")
    assert llm_failover.first_usable_endpoint_cached() is None


def test_require_usable_endpoint_names_what_each_endpoint_said():
    async def run():
        async with _client(lambda r: _models()) as c:
            await llm_failover.require_usable_endpoint(c)
    with pytest.raises(llm_failover.LLMUnavailable) as ei:
        asyncio.run(run())
    msg = str(ei.value)
    assert EP in msg and "no generative model" in msg and "inspect:" in msg
    assert isinstance(ei.value, httpx.HTTPError), "existing httpx.HTTPError handlers must catch it"


# ── error body ───────────────────────────────────────────────────────────────

def test_raise_for_status_with_body_carries_the_server_message():
    req = httpx.Request("POST", f"{EP}/chat/completions")
    resp = httpx.Response(400, json={"error": {"message": "No models loaded."}}, request=req)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        llm_failover.raise_for_status_with_body(resp)
    assert "No models loaded" in str(ei.value) and "400" in str(ei.value)


def test_raise_for_status_with_body_is_silent_on_success():
    req = httpx.Request("POST", f"{EP}/chat/completions")
    llm_failover.raise_for_status_with_body(httpx.Response(200, json={}, request=req))


# ── _call_model: reactive, and free on the healthy path ─────────────────────

def _profile(tmp_path, monkeypatch):
    import slm_intent
    import yaml
    (tmp_path / "p.yaml").write_text(yaml.safe_dump({
        "url": f"{EP}/chat/completions", "model": "m", "system": "s",
        "labels": ["a"], "fallback": "a", "temperature": 0, "timeout_s": 1.0,
    }), encoding="utf-8")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()
    return slm_intent, slm_intent.load_profile("p")


def test_no_model_loaded_becomes_llm_unavailable(tmp_path, monkeypatch):
    slm_intent, prof = _profile(tmp_path, monkeypatch)

    def handler(r):
        if r.url.path.endswith("/models"):
            return _models()
        return httpx.Response(400, json={"error": {"message": "No models loaded."}})

    async def run():
        async with _client(handler) as c:
            await slm_intent._call_model(prof, "hi", c)
    with pytest.raises(llm_failover.LLMUnavailable) as ei:
        asyncio.run(run())
    assert "no generative model" in str(ei.value)
    assert "No models loaded" in str(ei.value.__cause__), "the original failure is chained"


def test_a_request_failure_on_a_usable_server_stays_a_request_failure(tmp_path, monkeypatch):
    """Not every 400 is an outage: with a model listed, the original error is
    re-raised (with its body), not converted to LLMUnavailable."""
    slm_intent, prof = _profile(tmp_path, monkeypatch)

    def handler(r):
        if r.url.path.endswith("/models"):
            return _models("qwen/qwen3-8b")
        return httpx.Response(400, json={"error": {"message": "bad request shape"}})

    async def run():
        async with _client(handler) as c:
            await slm_intent._call_model(prof, "hi", c)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        asyncio.run(run())
    assert not isinstance(ei.value, llm_failover.LLMUnavailable)
    assert "bad request shape" in str(ei.value)


def test_healthy_path_makes_no_extra_request(tmp_path, monkeypatch):
    slm_intent, prof = _profile(tmp_path, monkeypatch)
    paths = []

    def handler(r):
        paths.append(r.url.path)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def run():
        async with _client(handler) as c:
            return await slm_intent._call_model(prof, "hi", c)
    assert asyncio.run(run()) == "ok"
    assert paths == ["/v1/chat/completions"], f"healthy call must not probe /models: {paths}"


def test_a_known_outage_skips_the_post(tmp_path, monkeypatch):
    slm_intent, prof = _profile(tmp_path, monkeypatch)
    llm_failover._USABILITY_CACHE[EP] = (llm_failover.time.monotonic(), False, "none loaded")
    paths = []

    def handler(r):
        paths.append(r.url.path)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async def run():
        async with _client(handler) as c:
            await slm_intent._call_model(prof, "hi", c)
    with pytest.raises(llm_failover.LLMUnavailable):
        asyncio.run(run())
    assert paths == [], "a cached outage must not POST again"


# ── reporting: once, not per call; quiet when healthy ───────────────────────

def test_outage_is_reported_once_then_recovery(caplog):
    log = logging.getLogger("test-outage")
    err = llm_failover.LLMUnavailable("observed: none loaded")
    with caplog.at_level(logging.DEBUG, logger="test-outage"):
        for _ in range(5):
            llm_failover.report_unavailable(log, "distillation", err)
        llm_failover.report_available(log, "distillation")
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(warns) == 1, [r.message for r in warns]
    assert len(infos) == 1 and "available again" in infos[0].message


def test_outage_warning_repeats_after_the_window(caplog, monkeypatch):
    monkeypatch.setattr(llm_failover, "OUTAGE_REPEAT_S", 0.0)
    log = logging.getLogger("test-outage-repeat")
    err = llm_failover.LLMUnavailable("x")
    with caplog.at_level(logging.WARNING, logger="test-outage-repeat"):
        llm_failover.report_unavailable(log, "c", err)
        llm_failover.report_unavailable(log, "c", err)
    assert len(caplog.records) == 2 and "still unavailable" in caplog.records[1].message


def test_recovery_is_silent_when_there_was_no_outage(caplog):
    log = logging.getLogger("test-healthy")
    with caplog.at_level(logging.DEBUG, logger="test-healthy"):
        llm_failover.report_available(log, "distillation")
    assert caplog.records == [], "a healthy system must log nothing"
