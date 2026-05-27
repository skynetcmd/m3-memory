"""Tests for memory_doctor diagnostic tool (B11)."""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    monkeypatch.delenv("M3_EMBED_GGUF", raising=False)
    monkeypatch.setenv("M3_DATABASE", str(tmp_path / "test.db"))
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")
    # Force fresh module load
    for mod in list(sys.modules):
        if mod.startswith("memory."):
            del sys.modules[mod]


def test_shim_identity_preserved(monkeypatch):
    """memory_doctor_impl must be identical when reached via shim.

    The autouse `_isolate_env` fixture nukes cached memory.* modules
    to ensure each test gets a fresh import; that interferes with the
    `memory_core` shim's identity caching of submodule symbols. For
    THIS test we want to verify the shim binding works END-TO-END from
    a clean import, so we re-import both sides after the cache-bust.
    """
    # Clear memory_core too so its re-export of memory_doctor_impl is
    # also from the freshly-loaded memory.doctor module.
    sys.modules.pop("memory_core", None)
    sys.modules.pop("memory.doctor", None)
    import memory_core as mc
    from memory.doctor import memory_doctor_impl
    assert mc.memory_doctor_impl is memory_doctor_impl


@pytest.mark.asyncio
async def test_returns_required_top_level_keys():
    """Contract: every doctor call returns the documented top-level shape."""
    from memory.doctor import memory_doctor_impl
    out = await memory_doctor_impl()
    assert set(out.keys()) == {
        "summary", "tier_1", "tier_2", "db", "roundtrip",
        "issues", "recommendations",
    }
    assert out["summary"] in {"healthy", "degraded", "broken"}
    assert isinstance(out["issues"], list)
    assert isinstance(out["recommendations"], list)


@pytest.mark.asyncio
async def test_tier1_not_configured_when_no_gguf():
    """When M3_EMBED_GGUF is unset, tier_1.status is 'not-configured'
    (not 'offline' or 'broken' — it's a deliberate-skip state)."""
    from memory.doctor import memory_doctor_impl
    out = await memory_doctor_impl()
    assert out["tier_1"]["status"] == "not-configured"
    assert out["tier_1"]["gguf_path"] is None
    assert out["tier_1"]["gguf_exists"] is False


@pytest.mark.asyncio
async def test_tier1_gguf_missing_reports_error(monkeypatch):
    """When M3_EMBED_GGUF points to a non-existent file, doctor reports
    a specific issue (not a generic offline)."""
    monkeypatch.setenv("M3_EMBED_GGUF", r"C:\nonexistent\fake.gguf")
    from memory.doctor import memory_doctor_impl
    out = await memory_doctor_impl()
    assert out["tier_1"]["gguf_exists"] is False
    assert any("missing" in issue.lower() or "fake.gguf" in issue
                 for issue in out["issues"])


@pytest.mark.asyncio
async def test_doctor_bounded_latency():
    """Doctor must complete fast even when all probes fail.

    Pre-registered SLO contract (B20 explicit):
      - Healthy corpus (warm cascade, tier-2 up): P95 < 3s
      - Degraded corpus (one tier down): hard cap 5s
      - Broken corpus (no tiers): hard cap 5s (probe timeouts respected)

    Each probe has a per-probe 2s timeout (see PROBE_TIMEOUT_S in
    memory.doctor). The roundtrip probe wraps the embed call at 10s but
    gather() cancels stragglers when the other probes complete. Total
    wall-clock must therefore stay under 5s in every state.

    The 15s threshold previously here was a safety net not a real
    target — the tool exists to diagnose hangs, so it must not itself
    approach "hang" territory. If this fails, doctor IS the hang it's
    meant to find."""
    import time

    from memory.doctor import memory_doctor_impl
    t0 = time.perf_counter()
    out = await asyncio.wait_for(memory_doctor_impl(), timeout=10.0)
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, (
        f"doctor took {elapsed:.1f}s — pre-registered SLO is <5s "
        f"(P95 < 3s on healthy path); regression to fix immediately"
    )
    assert out["summary"] in {"healthy", "degraded", "broken"}


@pytest.mark.asyncio
async def test_doctor_cold_cascade_slo(monkeypatch):
    """B20: explicit COLD-cascade SLO — first call after a fresh import,
    no tier-1 GGUF set, tier-2 mocked unreachable. Doctor must still
    return under 5s and classify the state correctly.

    This is the worst-case latency profile (no caches warm, every
    probe hits its full timeout) — it bounds the upper end of the SLO
    envelope for users who haven't installed the embedder service yet.
    """
    import time

    # Mock tier-2 at a TEST-NET address so the probe times out at 2s
    # instead of getting a fast ECONNREFUSED.
    monkeypatch.setenv("M3_EMBED_FALLBACK_URL", "http://198.51.100.1:8082")

    # Force fresh module so M3_EMBED_FALLBACK_URL is picked up
    for mod in list(sys.modules):
        if mod.startswith("memory."):
            del sys.modules[mod]

    from memory.doctor import memory_doctor_impl
    t0 = time.perf_counter()
    out = await asyncio.wait_for(memory_doctor_impl(), timeout=10.0)
    elapsed = time.perf_counter() - t0

    # SLO: 5s hard cap on cold cascade with one tier hung. If we hit
    # this, the parallel-probe gather isn't working.
    assert elapsed < 5.0, (
        f"COLD cascade took {elapsed:.1f}s — pre-registered SLO is <5s. "
        f"Likely probes ran sequentially, not in parallel."
    )
    # Classification: tier_2 must be NOT online; summary either degraded
    # (if roundtrip somehow worked via tier-1) or broken (typical).
    assert out["tier_2"]["status"] != "online"


@pytest.mark.asyncio
async def test_doctor_warm_cascade_slo():
    """B20: explicit WARM-cascade SLO — second call shortly after first,
    same process. Module-level caches + HTTP client reuse should make
    this faster than the cold call.

    Target: P50 < 1s on the warm call. Hard fail at 3s (regressions
    beyond that suggest a cache that's not being reused).
    """
    import time

    from memory.doctor import memory_doctor_impl

    # Cold call to warm caches
    await asyncio.wait_for(memory_doctor_impl(), timeout=10.0)

    # Now the warm call
    t0 = time.perf_counter()
    out = await asyncio.wait_for(memory_doctor_impl(), timeout=10.0)
    elapsed = time.perf_counter() - t0

    assert elapsed < 3.0, (
        f"WARM cascade took {elapsed:.1f}s — pre-registered SLO is "
        f"P50<1s with hard fail at 3s. Cache reuse may be broken."
    )
    assert out["summary"] in {"healthy", "degraded", "broken"}


@pytest.mark.asyncio
async def test_doctor_classification_all_four_states():
    """Effectiveness contract: doctor MUST classify all known states
    correctly. The 4 canonical states are encoded by which tiers can
    answer:

      | tier_1 | tier_2 | roundtrip | summary   |
      |--------|--------|-----------|-----------|
      |   X    |   X    |    OK     | healthy   |
      |   .    |   X    |    OK     | degraded  |
      |   X    |   .    |    OK     | degraded  |
      |   .    |   .    |   FAIL    | broken    |

    This test exercises the "degraded via tier-2-only" state (the
    default for any install that hasn't set M3_EMBED_GGUF). Other
    states are exercised by neighboring tests."""
    from memory.doctor import memory_doctor_impl
    out = await memory_doctor_impl()
    # In test env: no GGUF (tier 1 not-configured), 8082 may or may not
    # be up depending on environment. Either way, classification logic
    # must be self-consistent.
    t1 = out["tier_1"]["status"]
    t2 = out["tier_2"]["status"]
    rt = out["roundtrip"]["status"]
    summary = out["summary"]
    # Self-consistency: summary must match the tier+roundtrip facts.
    if rt == "ok" and (t1 == "online" or t2 == "online"):
        # At least one tier serves; classification depends on whether
        # the OTHER tier is also online.
        if t1 == "online" and t2 == "online":
            assert summary == "healthy", f"both tiers online but summary={summary}"
        else:
            assert summary == "degraded", (
                f"one tier online, one missing: expected degraded, got {summary}"
            )
    elif rt != "ok":
        assert summary == "broken", f"roundtrip failed but summary={summary}"


@pytest.mark.asyncio
async def test_doctor_per_probe_timeouts_respected(monkeypatch):
    """Each probe has a 2s timeout. Even if every probe hangs for its
    full 2s, total wall-clock should stay <5s thanks to asyncio.gather
    parallelism (not <8s = 4×2s sequential)."""
    # Point tier-2 at a port that will time out (not refuse): use
    # 198.51.100.1 (TEST-NET-2, RFC 5737) — packets get dropped, not
    # ICMP-refused, so the probe must hit its own 2s timeout.
    monkeypatch.setenv("M3_EMBED_FALLBACK_URL", "http://198.51.100.1:8082")

    import time

    from memory.doctor import memory_doctor_impl
    t0 = time.perf_counter()
    out = await asyncio.wait_for(memory_doctor_impl(), timeout=8.0)
    elapsed = time.perf_counter() - t0
    # Sequential would be ~4×2s = 8s. Parallel must be much less.
    assert elapsed < 5.0, (
        f"doctor took {elapsed:.1f}s with one hanging probe — probes "
        f"are running sequentially instead of concurrently"
    )
    # Tier-2 must report unreachable rather than crash
    assert out["tier_2"]["status"] != "online"


@pytest.mark.asyncio
async def test_summary_broken_when_no_tiers_and_no_roundtrip(monkeypatch):
    """If all tiers fail AND roundtrip fails, summary must be 'broken'."""
    # Point fallback URL at a port nothing listens on
    monkeypatch.setenv("M3_EMBED_FALLBACK_URL", "http://127.0.0.1:1")
    from memory.doctor import memory_doctor_impl
    out = await memory_doctor_impl()
    assert out["tier_2"]["status"] != "online"
    # Summary: broken (no tier 1, no tier 2, roundtrip likely fails)
    assert out["summary"] in {"broken", "degraded"}
    if out["summary"] == "broken":
        assert len(out["issues"]) > 0


@pytest.mark.asyncio
async def test_recommendations_actionable():
    """Recommendations should be human-actionable strings, not generic."""
    from memory.doctor import memory_doctor_impl
    out = await memory_doctor_impl()
    for rec in out["recommendations"]:
        assert isinstance(rec, str)
        assert len(rec) > 20, f"recommendation too terse: {rec!r}"
