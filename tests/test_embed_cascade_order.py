"""Regression test for embed cascade tier order (B7).

Prior bug: tier 2 (the always-on m3-embed-server at 127.0.0.1:8082) was
gated behind `_EMBED_GGUF_PATH is not None`. When the MCP server started
without M3_EMBED_GGUF set (the common deployment), tier 2 was skipped
entirely and the cascade fell straight to tier 3 (llm_failover → Ollama
probe). This produced cross-embedding-space vectors silently and caused
minute-long hangs when no Ollama was running.

After the fix, tier 2 is attempted regardless of tier-1 GGUF state — it's
an independent always-on service.

These tests mock the HTTP calls to verify tier ORDER without requiring
the real 8082 service to be running.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

# A proper-identity stub embedding: 1024-dim AND L2-unit-length, so the embedder-
# identity gate accepts it. (A zero vector has norm 0 and is correctly rejected.)
# These tests assert tier ORDER, so the exact direction is irrelevant.
_STUB_EMBEDDING = [1.0] + [0.0] * 1023


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Each test gets a clean env + a tmp DB so the cascade isn't poisoned
    by a real cache hit from a sibling test.

    These tests verify HTTP tier ORDERING, not native embedding. On a host
    that has the m3_core_rs native wheel installed AND a real M3_EMBED_GGUF
    set in the user environment (e.g. a dev box where `m3 setup` persisted
    one), the in-process EmbeddedEmbedder would satisfy the embed before
    tier-2 is ever reached — making `test_tier2_attempted_when_no_gguf` and
    `test_cold_cascade_fails_fast...` fail non-deterministically depending on
    run order. Pinning M3_CORE_RS_DISABLE=1 forces `config.m3_core_rs = None`
    on the fresh re-import below, so tier-1 is deterministically out of the
    picture regardless of the host's wheel/GGUF state. (delenv alone is not
    enough: it clears the GGUF path but the native wheel + a sibling-leaked
    env could still resurrect tier-1.)"""
    monkeypatch.delenv("M3_EMBED_GGUF", raising=False)
    monkeypatch.setenv("M3_CORE_RS_DISABLE", "1")
    monkeypatch.setenv("M3_DATABASE", str(tmp_path / "test.db"))
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")
    # Force a fresh module load so module-level constants (e.g. _EMBED_GGUF_PATH)
    # and config.m3_core_rs see the env set above.
    for mod in list(sys.modules):
        if mod.startswith("memory."):
            del sys.modules[mod]
    # Belt-and-suspenders: a sibling test that imported memory.embed earlier in
    # the session may have INITIALIZED and cached the in-process embedder in the
    # module globals (_embedded_embedder / _embedded_embed_checked). Deleting the
    # module from sys.modules above resets those on re-import — but only if the
    # re-import actually re-runs the module body. To be bulletproof regardless of
    # import-cache subtleties, force the embedder cache cleared on the freshly
    # imported module so `_embed` cannot satisfy a request from a stale tier-1
    # embedder and skip the mocked HTTP tiers (which made vec come back non-None
    # / a real vector when the test expected the HTTP cascade or None).
    import importlib
    embed = importlib.import_module("memory.embed")
    embed._embedded_embedder = None
    embed._embedded_embed_checked = False


@pytest.mark.asyncio
async def test_tier2_attempted_when_no_gguf(monkeypatch):
    """When M3_EMBED_GGUF is unset, the cascade MUST still try tier 2
    (the 8082 HTTP fallback). Pre-fix this was gated out and skipped."""
    from memory import embed

    tier2_called: list[str] = []

    class FakeResp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"data": [{"embedding": _STUB_EMBEDDING, "index": 0}]}

    class FakeClient:
        async def post(self, url, **kwargs):
            tier2_called.append(url)
            return FakeResp()

    monkeypatch.setattr(embed, "_get_embed_client", lambda: FakeClient())
    # Force breaker into closed (allow-request) state by recording successes
    if embed._CPU_FALLBACK_BREAKER is not None:
        for _ in range(5):
            embed._CPU_FALLBACK_BREAKER.record_success()

    vec, model = await embed._embed("hello world")
    assert vec is not None, "expected tier 2 to return a vector"
    assert len(tier2_called) == 1, f"expected exactly 1 tier-2 call, got: {tier2_called}"
    assert "8082" in tier2_called[0] or embed._EMBED_FALLBACK_URL in tier2_called[0]


@pytest.mark.asyncio
async def test_tier3_skipped_when_tier2_succeeds(monkeypatch):
    """When tier 2 returns a vector, tier 3 (llm_failover / Ollama) MUST
    NOT be called. Prevents accidental cross-embedding-space pollution."""
    from memory import embed

    tier3_called: list = []

    class FakeResp:
        status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return {"data": [{"embedding": _STUB_EMBEDDING, "index": 0}]}

    class FakeClient:
        async def post(self, url, **kwargs):
            return FakeResp()

    async def fake_get_best_embed(*args, **kwargs):
        tier3_called.append(args)
        return None

    monkeypatch.setattr(embed, "_get_embed_client", lambda: FakeClient())
    monkeypatch.setattr(embed, "get_best_embed", fake_get_best_embed, raising=False)
    if embed._CPU_FALLBACK_BREAKER is not None:
        for _ in range(5):
            embed._CPU_FALLBACK_BREAKER.record_success()

    vec, _ = await embed._embed("hello")
    assert vec is not None
    assert tier3_called == [], (
        f"tier 3 (llm_failover) was called {len(tier3_called)} times "
        f"despite tier 2 succeeding"
    )


@pytest.mark.asyncio
async def test_cold_cascade_fails_fast_when_no_tier_available(monkeypatch):
    """When NO tier is reachable (no GGUF, no 8082, no Ollama), the
    cascade must return (None, model) within a few seconds — NOT hang
    on retry loops for a minute. Pre-fix hangs were a deployment
    headache; this gate keeps cold cascade latency bounded."""
    from memory import embed

    class FakeClient:
        async def post(self, url, **kwargs):
            from httpx import ConnectError
            raise ConnectError("connection refused (mocked)")

    async def fake_get_best_embed(*args, **kwargs):
        # Simulate primary HTTP also down
        return None

    monkeypatch.setattr(embed, "_get_embed_client", lambda: FakeClient())
    monkeypatch.setattr(embed, "get_best_embed", fake_get_best_embed, raising=False)
    # Force breakers open (tripped) by recording many failures, so the
    # cascade short-circuits instead of retrying with backoff.
    if embed._CPU_FALLBACK_BREAKER is not None:
        for _ in range(20):
            embed._CPU_FALLBACK_BREAKER.record_failure()
    if embed._PRIMARY_BREAKER is not None:
        for _ in range(20):
            embed._PRIMARY_BREAKER.record_failure()

    import time
    t0 = time.perf_counter()
    vec, model = await asyncio.wait_for(embed._embed("hello"), timeout=10.0)
    elapsed_s = time.perf_counter() - t0

    assert vec is None, "expected None when no tier is reachable"
    assert elapsed_s < 5.0, (
        f"cold cascade took {elapsed_s:.1f}s — must fail fast (<5s) to "
        f"avoid the historical minute-long hang"
    )
