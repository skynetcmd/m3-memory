"""Entity extraction fails LOUD when a reasoning model yields no facts (§3).

On LM Studio / Ollama m3 auto-suppresses a reasoning model's thinking. Everywhere
else (OpenAI cloud, vLLM), or if suppression doesn't take, a reasoning model emits
only its reasoning channel with empty `content` → 0 facts. That used to be
SILENT. The extractor now logs one WARNING per pass naming the model + endpoint +
the fix, so the 0-fact outcome is never swallowed. Hermetic — MockTransport
returns a reasoning-only reply, no server.
"""
import logging
import sys
from pathlib import Path

import pytest

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import llm_failover as LF  # noqa: E402
import m3_entities as E  # noqa: E402


def _profile(model="m"):
    import os
    os.environ["M3_SLM_PROFILES_DIR"] = str(Path(_BIN).parent / "config" / "slm")
    from slm_intent import load_profile
    base = load_profile("entities_local_qwen")
    # pin a concrete model so no discovery GET is needed; keep it anthropic-on-
    # loopback so localize() (entity path) rewrites it to the shared OpenAI endpoint.
    return E.Profile(**{**base.__dict__, "model": model})


def _client(reply_message):
    import httpx

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": reply_message}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


VT = frozenset({"person", "technology", "location"})
VP = frozenset({"uses"})
TEXT = "Ada Lovelace used Rust at Cambridge University in the 1840s."


@pytest.mark.asyncio
async def test_reasoning_only_reply_warns_on_non_suppressible(monkeypatch, caplog):
    # vLLM-style endpoint (:8000) — NOT auto-suppressible.
    monkeypatch.setattr(LF, "LLM_ENDPOINTS", ["http://localhost:8000/v1"], raising=False)
    client = _client({"content": "", "reasoning": "let me think hard about this..."})
    with caplog.at_level(logging.WARNING, logger="m3_entities"):
        out = await E._build_extractor(_profile(), "", VT, VP, client)(TEXT)
    await client.aclose()
    assert out == {"entities": [], "relationships": []}
    msgs = " ".join(r.message for r in caplog.records)
    assert "reasoning" in msgs and "0 facts" in msgs
    assert "auto-suppress" in msgs  # the non-suppressible-endpoint branch


@pytest.mark.asyncio
async def test_clean_json_reply_does_not_warn(monkeypatch, caplog):
    monkeypatch.setattr(LF, "LLM_ENDPOINTS", ["http://localhost:8000/v1"], raising=False)
    good = '{"entities":[{"canonical_name":"Rust","entity_type":"technology","confidence":0.9}],"relationships":[]}'
    client = _client({"content": good})
    with caplog.at_level(logging.WARNING, logger="m3_entities"):
        out = await E._build_extractor(_profile(), "", VT, VP, client)(TEXT)
    await client.aclose()
    assert any(e["canonical_name"] == "Rust" for e in out["entities"])
    assert not [r for r in caplog.records if "reasoning" in r.message]
