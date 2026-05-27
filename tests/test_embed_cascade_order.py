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


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Each test gets a clean env + a tmp DB so the cascade isn't poisoned
    by a real cache hit from a sibling test."""
    monkeypatch.delenv("M3_EMBED_GGUF", raising=False)
    monkeypatch.setenv("M3_DATABASE", str(tmp_path / "test.db"))
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")
    # Force a fresh module load so module-level constants (e.g. _EMBED_GGUF_PATH)
    # see the cleared env.
    for mod in list(sys.modules):
        if mod.startswith("memory."):
            del sys.modules[mod]


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
            return {"data": [{"embedding": [0.0] * 1024, "index": 0}]}

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
            return {"data": [{"embedding": [0.0] * 1024, "index": 0}]}

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
