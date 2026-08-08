"""Blank-model hardening for entity extraction.

A blank/sentinel `model` in an SLM profile means "follow the loaded model". The
old code sent an empty `model` and relied on LM Studio routing to the single
loaded model — which 404s ("Invalid model identifier \"\"") whenever >1 model is
loaded or JIT loading is off. The fix discovers the loaded model BY NAME through
the shared `llm_failover` seam (async sibling of discover_model_sync).

Covers: `llm_failover.discover_model_async` (largest usable pick, embedder
filtering, fresh=cache-bypass, failure->None) and `m3_entities`'s base-url
derivation + delegation with fresh=True. Hermetic — a fake async client, no real
server (CI-safe).
"""
import asyncio
import sys
from pathlib import Path

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import llm_failover as LF  # noqa: E402
import m3_entities as E  # noqa: E402


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _Client:
    """Fake httpx.AsyncClient capturing the GET it receives."""
    def __init__(self, resp_json, capture=None):
        self._j = resp_json
        self._cap = capture if capture is not None else {}

    async def get(self, url, headers=None, timeout=None):
        self._cap["url"] = url
        self._cap["headers"] = headers or {}
        return _Resp(self._j)


# ── llm_failover.discover_model_async (the shared seam) ─────────────────────────

def test_async_picks_usable_and_filters_embedders(monkeypatch):
    monkeypatch.setattr(LF, "_SYNC_MODEL_CACHE", {})
    cap = {}
    # embedder must be excluded; the generative model is the only usable one.
    client = _Client({"data": [{"id": "text-embedding-bge-m3"}, {"id": "qwen/qwen3.5-9b"}]}, cap)
    got = asyncio.run(LF.discover_model_async(client, "http://127.0.0.1:1234/v1", "tok", fresh=True))
    assert got == "qwen/qwen3.5-9b"
    assert cap["url"] == "http://127.0.0.1:1234/v1/models"


def test_async_fresh_bypasses_cache_but_default_uses_it(monkeypatch):
    monkeypatch.setattr(LF, "_SYNC_MODEL_CACHE", {"http://x/v1": "STALE-MODEL"})
    client = _Client({"data": [{"id": "qwen/qwen3-8b"}]})
    # fresh=True ignores the (stale) cache and re-queries
    assert asyncio.run(LF.discover_model_async(client, "http://x/v1", "t", fresh=True)) == "qwen/qwen3-8b"
    # fresh=False returns the cached value
    assert asyncio.run(LF.discover_model_async(client, "http://x/v1", "t", fresh=False)) == "STALE-MODEL"


def test_async_failure_returns_none(monkeypatch):
    monkeypatch.setattr(LF, "_SYNC_MODEL_CACHE", {})

    class _Boom:
        async def get(self, *a, **k):
            raise RuntimeError("server down")
    assert asyncio.run(LF.discover_model_async(_Boom(), "http://x/v1", "t", fresh=True)) is None


# ── m3_entities: derive the OpenAI-style base + delegate fresh ───────────────────

def test_entities_strips_anthropic_suffix_and_forces_fresh(monkeypatch):
    seen = {}

    async def _fake(client, endpoint, token, *, fresh=False):
        seen["endpoint"] = endpoint
        seen["fresh"] = fresh
        return "MODEL-X"
    monkeypatch.setattr(LF, "discover_model_async", _fake)
    got = asyncio.run(E._discover_loaded_model_async(object(), "http://127.0.0.1:1234/v1/messages", "tok"))
    assert got == "MODEL-X"
    assert seen["endpoint"] == "http://127.0.0.1:1234/v1"   # /messages stripped -> base
    assert seen["fresh"] is True                            # per-pass fresh (survives swaps)


def test_entities_strips_openai_suffix(monkeypatch):
    seen = {}

    async def _fake(client, endpoint, token, *, fresh=False):
        seen["endpoint"] = endpoint
        return "M"
    monkeypatch.setattr(LF, "discover_model_async", _fake)
    asyncio.run(E._discover_loaded_model_async(object(), "http://127.0.0.1:1234/v1/chat/completions", None))
    assert seen["endpoint"] == "http://127.0.0.1:1234/v1"


def test_entities_discovery_failure_keeps_none(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(LF, "discover_model_async", _boom)
    assert asyncio.run(E._discover_loaded_model_async(object(), "http://x/v1/messages", "t")) is None
