"""Entity-extractor blank-model hardening (m3_entities._discover_loaded_model_async).

A blank/sentinel `model` in an SLM profile means "follow the loaded model". The
old code sent an empty `model` and relied on LM Studio routing to the single
loaded model — which 404s ("Invalid model identifier \"\"") whenever >1 model is
loaded or JIT loading is off. These tests pin the fix: discover the loaded model
BY NAME from `{base}/models`, derive that base from the profile url (anthropic
/messages OR openai /chat/completions), and degrade to None (caller keeps "")
on any failure. Hermetic — a fake async client, no real server (CI-safe).
"""
import asyncio
import sys
from pathlib import Path

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import m3_entities as E  # noqa: E402


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return {"data": self._data}


class _Client:
    """Fake httpx.AsyncClient capturing the GET it receives."""
    def __init__(self, data, capture):
        self._data = data
        self._cap = capture

    async def get(self, url, headers=None, timeout=None):
        self._cap["url"] = url
        self._cap["headers"] = headers or {}
        return _Resp(self._data)


def test_discovers_first_loaded_model_and_strips_anthropic_suffix():
    cap = {}
    client = _Client([{"id": "qwen/qwen3.5-9b"}, {"id": "google/gemma-4-26b-a4b"}], cap)
    got = asyncio.run(E._discover_loaded_model_async(
        client, "http://127.0.0.1:1234/v1/messages", "tok"))
    assert got == "qwen/qwen3.5-9b"                       # first loaded model, by name
    assert cap["url"] == "http://127.0.0.1:1234/v1/models"  # /messages -> /models
    assert cap["headers"].get("Authorization") == "Bearer tok"


def test_openai_suffix_also_derives_v1_models():
    cap = {}
    asyncio.run(E._discover_loaded_model_async(
        _Client([{"id": "m"}], cap), "http://127.0.0.1:1234/v1/chat/completions", None))
    assert cap["url"] == "http://127.0.0.1:1234/v1/models"
    assert "Authorization" not in cap["headers"]          # no token -> no auth header


def test_no_models_loaded_returns_none():
    assert asyncio.run(E._discover_loaded_model_async(
        _Client([], {}), "http://x/v1/messages", None)) is None


def test_any_failure_returns_none():
    class _Boom:
        async def get(self, *a, **k):
            raise RuntimeError("server down")
    assert asyncio.run(E._discover_loaded_model_async(
        _Boom(), "http://x/v1/messages", "t")) is None
