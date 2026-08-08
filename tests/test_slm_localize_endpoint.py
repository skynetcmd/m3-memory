"""Loopback profiles follow the shared LLM seam (LM Studio OR Ollama).

An SLM profile pinned at a loopback ``/v1/messages`` (Anthropic-compat) only works
on LM Studio — Ollama serves no such endpoint, so every enrich/entity/observer/
reflector request 404s there. ``slm_intent.localize_endpoint`` normalizes a
loopback profile to ``llm_failover``'s resolved OpenAI-compatible
``/v1/chat/completions`` (served by both) and forces ``backend='openai'``, while
leaving a remote/pinned profile untouched. Hermetic — patches ``LLM_ENDPOINTS``,
no server.
"""
import sys
from pathlib import Path

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import llm_failover as LF  # noqa: E402
import slm_intent as SI  # noqa: E402


def _with_endpoints(monkeypatch, endpoints):
    monkeypatch.setattr(LF, "LLM_ENDPOINTS", endpoints, raising=False)


def test_loopback_anthropic_becomes_shared_openai(monkeypatch):
    _with_endpoints(monkeypatch, ["http://localhost:1234/v1"])
    url, backend = SI.localize_endpoint("http://127.0.0.1:1234/v1/messages", "anthropic")
    assert url == "http://localhost:1234/v1/chat/completions"
    assert backend == "openai"


def test_loopback_follows_ollama_port(monkeypatch):
    # Ollama enabled -> the seam resolves :11434; localize must follow the PORT,
    # not just swap the path on the pinned :1234.
    _with_endpoints(monkeypatch, ["http://localhost:11434/v1"])
    url, backend = SI.localize_endpoint("http://127.0.0.1:1234/v1/messages", "anthropic")
    assert url == "http://localhost:11434/v1/chat/completions"
    assert backend == "openai"


def test_remote_profile_untouched(monkeypatch):
    _with_endpoints(monkeypatch, ["http://localhost:1234/v1"])
    url, backend = SI.localize_endpoint("https://api.anthropic.com/v1/messages", "anthropic")
    assert url == "https://api.anthropic.com/v1/messages"
    assert backend == "anthropic"


def test_empty_seam_keeps_profile(monkeypatch):
    # No local LLM resolved -> keep the profile as-is (nothing is listening
    # anyway; don't invent an endpoint).
    _with_endpoints(monkeypatch, [])
    url, backend = SI.localize_endpoint("http://localhost:1234/v1/messages", "anthropic")
    assert url == "http://localhost:1234/v1/messages"
    assert backend == "anthropic"


def test_loopback_openai_normalized_to_shared(monkeypatch):
    # Already openai + loopback but pinned to a stale port -> still re-homed to
    # the shared endpoint so it follows the live server.
    _with_endpoints(monkeypatch, ["http://localhost:11434/v1"])
    url, backend = SI.localize_endpoint("http://localhost:1234/v1/chat/completions", "openai")
    assert url == "http://localhost:11434/v1/chat/completions"
    assert backend == "openai"


# ── prefer_native (the _call_model path: enrich / observer / reflector) ────────
# Keep the Anthropic wire format ONLY for LM Studio (where it buys prompt caching);
# route every other local server through the universal OpenAI path. Ollama actually
# serves /v1/messages too, but gains no caching from it, so OpenAI is still chosen.

def test_prefer_native_keeps_anthropic_on_lmstudio(monkeypatch):
    _with_endpoints(monkeypatch, ["http://localhost:1234/v1"])
    url, backend = SI.localize_endpoint(
        "http://127.0.0.1:1234/v1/messages", "anthropic", prefer_native=True)
    # LM Studio (:1234) -> keep native Anthropic so prompt caching is preserved.
    assert url == "http://127.0.0.1:1234/v1/messages"
    assert backend == "anthropic"


def test_prefer_native_downgrades_anthropic_on_ollama(monkeypatch):
    _with_endpoints(monkeypatch, ["http://localhost:11434/v1"])
    url, backend = SI.localize_endpoint(
        "http://127.0.0.1:1234/v1/messages", "anthropic", prefer_native=True)
    # Ollama (:11434): no caching benefit -> universal OpenAI path, follow its port.
    assert url == "http://localhost:11434/v1/chat/completions"
    assert backend == "openai"


def test_prefer_native_downgrades_anthropic_on_other_openai_server(monkeypatch):
    # A non-LM-Studio server that may not implement /v1/messages at all (e.g. vLLM
    # on :8000) must be routed through OpenAI, not left on Anthropic.
    _with_endpoints(monkeypatch, ["http://localhost:8000/v1"])
    url, backend = SI.localize_endpoint(
        "http://127.0.0.1:1234/v1/messages", "anthropic", prefer_native=True)
    assert url == "http://localhost:8000/v1/chat/completions"
    assert backend == "openai"


def test_prefer_native_openai_loopback_follows_shared(monkeypatch):
    _with_endpoints(monkeypatch, ["http://localhost:11434/v1"])
    url, backend = SI.localize_endpoint(
        "http://localhost:1234/v1/chat/completions", "openai", prefer_native=True)
    assert url == "http://localhost:11434/v1/chat/completions"
    assert backend == "openai"


def test_prefer_native_remote_anthropic_untouched(monkeypatch):
    _with_endpoints(monkeypatch, ["http://localhost:11434/v1"])
    url, backend = SI.localize_endpoint(
        "https://api.anthropic.com/v1/messages", "anthropic", prefer_native=True)
    assert url == "https://api.anthropic.com/v1/messages"
    assert backend == "anthropic"
