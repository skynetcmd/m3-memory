"""On Ollama, a reasoning model's thinking is suppressed so extraction works.

A reasoning model (e.g. Qwen3.5) burns its whole token budget in the reasoning
channel and emits nothing in `content`, so m3's fact extraction parses 0 facts.
Ollama's OpenAI-compatible endpoint honors ``reasoning_effort="none"`` to turn
thinking OFF (verified live) — but ``"none"`` is non-standard and 400s on OpenAI /
LM Studio, so ``slm_intent._call_model`` sends it ONLY when the resolved server is
Ollama. These pin that the payload carries ``reasoning_effort`` for Ollama and NOT
for other servers. Hermetic — MockTransport captures the request, no server.
"""
import sys
from pathlib import Path

import pytest

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import llm_failover as LF  # noqa: E402
import slm_intent as SI  # noqa: E402


def _write_profile(path, **fields):
    import yaml
    base = {"url": "http://127.0.0.1:1234/v1/chat/completions", "model": "m",
            "system": "s", "labels": ["a"], "fallback": "a", "backend": "openai",
            "temperature": 0, "timeout_s": 5.0}
    base.update(fields)
    path.write_text(yaml.safe_dump(base), encoding="utf-8")


async def _capture_payload(monkeypatch, tmp_path, endpoints):
    import httpx
    monkeypatch.setattr(LF, "LLM_ENDPOINTS", endpoints, raising=False)
    _write_profile(tmp_path / "p.yaml")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    SI.invalidate_cache()
    prof = SI.load_profile("p")

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        captured["json"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await SI._call_model(prof, "hi", client)
    finally:
        await client.aclose()
    return captured


@pytest.mark.asyncio
async def test_ollama_gets_reasoning_effort_none(tmp_path, monkeypatch):
    cap = await _capture_payload(monkeypatch, tmp_path, ["http://localhost:11434/v1"])
    assert cap["url"] == "http://localhost:11434/v1/chat/completions"
    assert cap["json"].get("reasoning_effort") == "none"


@pytest.mark.asyncio
async def test_lmstudio_openai_does_not_get_reasoning_effort(tmp_path, monkeypatch):
    # LM Studio (and OpenAI) 400 on reasoning_effort="none" -> must NOT be sent.
    cap = await _capture_payload(monkeypatch, tmp_path, ["http://localhost:1234/v1"])
    assert "reasoning_effort" not in cap["json"]


@pytest.mark.asyncio
async def test_other_openai_server_does_not_get_reasoning_effort(tmp_path, monkeypatch):
    cap = await _capture_payload(monkeypatch, tmp_path, ["http://localhost:8000/v1"])
    assert "reasoning_effort" not in cap["json"]
