"""Tests for memory/embed._embeddings_post_url — tier-3 endpoint resolution.

Regression cover for the 2026-08-09 path mismatch. The tier-3 (OpenAI-compatible)
sites built `f"{base}/embeddings"`, which silently assumes the base already
carries the version segment. `M3_EMBED_URL` / `fallback_url` point at a BARE HOST
for the shared m3 embedder (`http://127.0.0.1:8082`), so every primary-tier call
went to `/embeddings` — a route no server in this tier serves. The result was not
a visible failure: it 404'd, burned three attempts with exponential backoff, then
fell through to the tier-2 fallback and returned correct vectors. Only the ~6s
stall showed up, as test_doctor_cold_cascade_slo blowing its 5s budget.

The tier boundary matters and is asserted below: the m3-native SINGULAR
`/embedding` belongs to tier 2 (no auth, X-M3-Embed-Priority headers); tier 3
speaks OpenAI (`model` field, Bearer token, `data[0].embedding`). A bare host
must therefore resolve to `/v1/embeddings`, never to tier 2's route.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))

from memory.embed import _embeddings_post_url  # noqa: E402


@pytest.mark.parametrize("base,expected", [
    # The bug: a bare host must get the OpenAI version segment, not "/embeddings".
    ("http://127.0.0.1:8082", "http://127.0.0.1:8082/v1/embeddings"),
    ("http://127.0.0.1:8082/", "http://127.0.0.1:8082/v1/embeddings"),
    ("http://localhost:8082", "http://localhost:8082/v1/embeddings"),
    # Already an OpenAI base (LM Studio, vLLM, OpenAI itself): append only.
    ("http://host:1234/v1", "http://host:1234/v1/embeddings"),
    ("http://host:1234/v1/", "http://host:1234/v1/embeddings"),
    ("https://api.openai.com/v1", "https://api.openai.com/v1/embeddings"),
    ("http://host/v2", "http://host/v2/embeddings"),
    # Explicit full endpoints are honored verbatim — the caller was specific.
    ("http://host/v1/embeddings", "http://host/v1/embeddings"),
    ("http://host:8082/embedding", "http://host:8082/embedding"),
    ("http://host:8082/embedding/", "http://host:8082/embedding"),
])
def test_endpoint_resolution(base, expected):
    assert _embeddings_post_url(base) == expected


def test_bare_host_never_resolves_to_the_tier2_route():
    """A bare host must NOT become the m3-native singular `/embedding`. That
    route is tier 2's contract (no auth, priority headers); tier 3 posts a
    `model` field with a Bearer token and would be talking the wrong protocol."""
    url = _embeddings_post_url("http://127.0.0.1:8082")
    assert not url.endswith("/embedding")
    assert url.endswith("/v1/embeddings")


def test_no_double_suffix():
    """Idempotent: re-resolving an already-resolved URL must not stack paths."""
    once = _embeddings_post_url("http://127.0.0.1:8082")
    assert _embeddings_post_url(once) == once
    assert once.count("/embeddings") == 1


@pytest.mark.parametrize("empty", ["", None])
def test_empty_base_does_not_raise(empty):
    """Fail predictably, not with an AttributeError deep in a retry loop."""
    assert _embeddings_post_url(empty) == "/v1/embeddings"
