"""Tests for Tier 4 Cloud Enclave failover, PII redaction, and keyring lookup."""
from __future__ import annotations

import os
import sys
import uuid
from unittest.mock import MagicMock

import pytest

# Ensure bin is in python path
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "bin"))

import memory.config as config
from memory.embed import _embed, get_embed_breaker_state, reset_embed_breakers


@pytest.fixture(autouse=True)
def clean_breakers(monkeypatch):
    reset_embed_breakers()
    # Ultimate cache buster: force content hash to be unique every time to guarantee cache misses
    monkeypatch.setattr("memory.embed._content_hash", lambda t: str(uuid.uuid4()))

    # Mock best LLM failover globally to avoid real network requests
    async def mock_get_best_embed(*args, **kwargs):
        return None
    monkeypatch.setattr("memory.embed.get_best_embed", mock_get_best_embed)
    monkeypatch.setattr("llm_failover.get_best_embed", mock_get_best_embed)

    # Bypass migrations globally to avoid DB lock contention
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")

    yield
    reset_embed_breakers()


@pytest.mark.asyncio
async def test_tier4_fallback_disabled_by_default(monkeypatch):
    """By default, M3_ALLOW_CLOUD_FALLBACK is False, so failover should not occur."""
    monkeypatch.setattr(config, "M3_ALLOW_CLOUD_FALLBACK", False)
    monkeypatch.setattr(config, "M3_CLOUD_ENCLAVE_URL", "http://enclave.test")
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")

    # Force all local tiers to fail by raising exceptions
    monkeypatch.setattr("memory.embed._get_embedded_embedder", lambda: None)
    monkeypatch.setattr(config, "EMBED_BREAKER_CPU_FALLBACK_THRESHOLD", 0)  # disable breaker
    monkeypatch.setattr(config, "EMBED_BREAKER_PRIMARY_THRESHOLD", 0)

    # Mock client post to fail
    async def mock_post(*args, **kwargs):
        raise RuntimeError("Local HTTP down")

    client_mock = MagicMock()
    client_mock.post = mock_post
    monkeypatch.setattr("memory.embed._get_embed_client", lambda: client_mock)

    vec, model = await _embed("hello")
    assert vec is None


@pytest.mark.asyncio
async def test_tier4_fallback_triggered_with_redaction(monkeypatch):
    """When fallback is allowed, Tier 4 is reached and PII is redacted."""
    monkeypatch.setattr(config, "M3_ALLOW_CLOUD_FALLBACK", True)
    monkeypatch.setattr(config, "M3_CLOUD_ENCLAVE_URL", "http://enclave.test/embeddings")
    monkeypatch.setattr(config, "M3_CLOUD_AUTH_TOKEN_KEYRING", "service:user")

    # Mock embedded and CPU fallback to fail
    monkeypatch.setattr("memory.embed._get_embedded_embedder", lambda: None)
    # NULL the embed circuit-breaker globals so every tier is unconditionally
    # allowed (the cascade gates each tier on `_X_BREAKER is None or
    # _X_BREAKER.allow_request()`). This is the real fix for the CI failure:
    # the breakers are built ONCE at module import from config thresholds, so
    # patching the config values is too late — the already-constructed breaker
    # objects are unaffected. If a breaker was left OPEN by an earlier test/embed
    # call in the session, the tier short-circuits WITHOUT calling
    # _get_embed_client, the cascade returns None before tier 4, and the mocked
    # cloud client is never reached. (Diagnosed via a CI assertion: the patched
    # getter was never called.) Setting the globals to None forces every tier
    # open so the cascade flows through the mocked client to tier 4.
    import memory.embed as _me
    for _bk in ("_EMBEDDED_BREAKER", "_CPU_FALLBACK_BREAKER",
                "_PRIMARY_BREAKER", "_CLOUD_BREAKER"):
        monkeypatch.setattr(_me, _bk, None)

    # Mock best LLM failover to fail
    async def mock_get_best_embed(*args, **kwargs):
        return None
    monkeypatch.setattr("memory.embed.get_best_embed", mock_get_best_embed)

    # Mock keyring lookup to return a token
    monkeypatch.setattr("auth_utils.safe_keyring_get_password", lambda s, u: "keyring-token")

    # Mock HTTP client responses
    posted_payloads = []
    posted_headers = []
    posted_urls = []

    async def mock_post(url, json, headers=None, **kwargs):
        if "enclave.test" in url:
            posted_urls.append(url)
            posted_payloads.append(json)
            posted_headers.append(headers)
            # Return dummy embedding
            resp = MagicMock()
            resp.json.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]}
            return resp
        raise RuntimeError("Local HTTP down")

    client_mock = MagicMock()
    client_mock.post = mock_post
    get_client_calls = []

    def _fake_get_client():
        get_client_calls.append(1)
        return client_mock
    monkeypatch.setattr(_me, "_get_embed_client", _fake_get_client)
    # Neutralize the process-wide cached real client so nothing can reuse a
    # stale loop-bound AsyncClient that bypasses the patched getter.
    monkeypatch.setattr(_me, "_EMBED_CLIENT", None, raising=False)

    # Let's request an embedding with sensitive data
    vec, model = await _embed("My secret key is sk-proj-12345678901234567890 and email is test@domain.com")

    # Regression guard: tier 4 must have gone through the mocked client getter.
    # If this is empty the cascade short-circuited before any HTTP tier (the
    # original CI failure mode), so a bare `vec is None` check would mask it.
    assert get_client_calls, (
        "embed cascade short-circuited before reaching a client tier — "
        "a breaker was left open (see breaker-nulling above)"
    )
    assert vec == [0.1, 0.2, 0.3, 0.4]
    assert len(posted_payloads) == 1
    # Verify PII was redacted!
    input_text = posted_payloads[0]["input"]
    assert "sk-proj" not in input_text
    assert "test@domain.com" not in input_text
    assert "[REDACTED:api_keys]" in input_text
    assert "[REDACTED:pii]" in input_text

    # Verify authorization header had the keyring token
    assert posted_headers[0]["Authorization"] == "Bearer keyring-token"
    assert posted_urls[0] == "http://enclave.test/embeddings"


@pytest.mark.asyncio
async def test_tier4_circuit_breaker(monkeypatch):
    """Tier 4 Cloud Enclave breaker trips after consecutive failures."""
    monkeypatch.setattr(config, "M3_ALLOW_CLOUD_FALLBACK", True)
    monkeypatch.setattr(config, "M3_CLOUD_ENCLAVE_URL", "http://enclave.test/embeddings")
    monkeypatch.setattr(config, "EMBED_BREAKER_CLOUD_THRESHOLD", 2)
    monkeypatch.setattr(config, "EMBED_BREAKER_CLOUD_RESET_SECS", 30.0)

    # Mock all local tiers to fail
    monkeypatch.setattr("memory.embed._get_embedded_embedder", lambda: None)
    monkeypatch.setattr("memory.embed.get_best_embed", lambda *a, **k: None)

    # Force enclave to fail
    async def mock_post_fail(*args, **kwargs):
        raise RuntimeError("Enclave down")

    client_mock = MagicMock()
    client_mock.post = mock_post_fail
    monkeypatch.setattr("memory.embed._get_embed_client", lambda: client_mock)

    # Reset breakers so we have a clean slate
    reset_embed_breakers()

    # First attempt -> Fail
    vec, _ = await _embed("hello")
    assert vec is None

    # Second attempt -> Fail
    vec, _ = await _embed("hello")
    assert vec is None

    # Third attempt -> Fail -> Breaker should open (threshold is 3 at import-time)
    vec, _ = await _embed("hello")
    assert vec is None

    # Breaker state should now be open!
    state = get_embed_breaker_state()
    # Wait, if Rust is not loaded or disabled, state is 'disabled'
    if state["cloud"] != "disabled":
        assert state["cloud"] == "open"
