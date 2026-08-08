"""files_memory.extract._llm_call: auto-suppress on LMS/Ollama, fail loud else.

Mirrors the entity path. On LM Studio / Ollama the payload carries
reasoning_effort="none" so a reasoning model's thinking is turned off; anywhere
else a reasoning model returns only reasoning + empty content → 0 facts, and that
must be a LOUD warning, never a silent "". Hermetic — httpx.Client is stubbed.
"""
import logging
import sys
from pathlib import Path

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import httpx  # noqa: E402
from files_memory import extract as X  # noqa: E402


class _FakeClient:
    """Stand-in for httpx.Client capturing the POSTed json + returning a canned reply."""
    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        _FakeClient.captured = {"url": url, "json": json}
        return httpx.Response(200, json={"choices": [{"message": _FakeClient.reply}]},
                              request=httpx.Request("POST", url))


def _patch(monkeypatch, endpoint, reply):
    monkeypatch.setattr(X, "_llm_endpoint", lambda: endpoint)
    monkeypatch.setattr(X, "_llm_model", lambda: "m")
    monkeypatch.setattr(X, "chat_completions_url", lambda e: e.rstrip("/") + "/chat/completions", raising=False)
    monkeypatch.setattr(X, "llm_auth_headers", lambda: {}, raising=False)
    _FakeClient.reply = reply
    monkeypatch.setattr(httpx, "Client", _FakeClient)


def test_ollama_gets_reasoning_effort(monkeypatch):
    _patch(monkeypatch, "http://localhost:11434/v1", {"content": "{}"})
    X._llm_call("some document text to extract facts from")
    assert _FakeClient.captured["json"].get("reasoning_effort") == "none"


def test_other_server_no_reasoning_effort(monkeypatch):
    _patch(monkeypatch, "http://localhost:8000/v1", {"content": "{}"})
    X._llm_call("some document text to extract facts from")
    assert "reasoning_effort" not in _FakeClient.captured["json"]


def test_reasoning_only_reply_warns_loud(monkeypatch, caplog):
    _patch(monkeypatch, "http://localhost:8000/v1",
           {"content": "", "reasoning": "thinking at length..."})
    with caplog.at_level(logging.WARNING, logger="files_memory.extract"):
        out = X._llm_call("some document text to extract facts from")
    assert out == ""
    joined = " ".join(r.message for r in caplog.records)
    assert "reasoning" in joined and "0 facts" in joined


def test_clean_content_does_not_warn(monkeypatch, caplog):
    _patch(monkeypatch, "http://localhost:8000/v1", {"content": '{"facts":[]}'})
    with caplog.at_level(logging.WARNING, logger="files_memory.extract"):
        out = X._llm_call("some document text to extract facts from")
    assert out == '{"facts":[]}'
    assert not [r for r in caplog.records if "reasoning" in r.message]
