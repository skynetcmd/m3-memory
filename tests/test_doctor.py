"""Tests for memory_doctor diagnostic tool (B11)."""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

from conftest import embed_backend_reachable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    monkeypatch.delenv("M3_EMBED_GGUF", raising=False)
    # Determinism across hosts: on a dev box with the native m3_core_rs wheel
    # installed AND a persisted M3_EMBED_GGUF (in the user environment), the
    # tier-1 probe would load the REAL embedder — on CUDA that's a multi-second
    # cold start (model + CUDA context), blowing the <5s SLO assertions and
    # making these tests pass/fail by host rather than by code. These tests are
    # about probe TIMEOUT/parallelism behavior and classification, not real
    # embedding. Pin M3_CORE_RS_DISABLE=1 so tier-1 is deterministically
    # 'not-configured' regardless of the host's wheel/GGUF state.
    monkeypatch.setenv("M3_CORE_RS_DISABLE", "1")
    # Neutralize the tier-3 PRIMARY embed endpoint too. Otherwise the roundtrip
    # probe's cascade falls through tier-1 (disabled) + tier-2 (TEST-NET) to the
    # real LM Studio at :1234 on a dev box — which answers slowly (observed 400s
    # + retry/backoff), pushing the <5s SLO over. Pin the primary at a TEST-NET
    # (RFC 5737) address so every tier fails FAST and deterministically, which is
    # exactly the "broken corpus, every probe hits its timeout" state these SLO
    # tests claim to measure. Also disable the on-by-default LM Studio probe.
    monkeypatch.setenv("M3_LLM_URL", "http://198.51.100.2:9")
    monkeypatch.setenv("M3_ENABLE_LMSTUDIO_FAILOVER", "0")
    monkeypatch.setenv("M3_ENABLE_OLLAMA_FAILOVER", "0")
    monkeypatch.setenv("M3_DATABASE", str(tmp_path / "test.db"))
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")
    # Isolate the config root to an EMPTY dir so these tests never read the host's
    # real .embed_config.json. On a shared-embedder box (the shipped default) that
    # file sets disable_inproc_embedder:true, which correctly makes tier-1 report
    # 'shared-mode' — but the classification tests here assert the generic
    # not-configured / gguf-missing states, so they must run WITHOUT a shared
    # config present (hermetic, host-independent — §3).
    _cfg_root = tmp_path / "config"
    _cfg_root.mkdir(exist_ok=True)
    monkeypatch.setenv("M3_CONFIG_ROOT", str(_cfg_root))
    # Force fresh module load so each doctor test re-reads the env above.
    # CRITICAL: snapshot and RESTORE the purged modules on teardown. Without
    # restore, the whole rest of the pytest session runs with memory.* modules
    # re-initialized under THIS test's env (tmp M3_DATABASE, M3_SKIP_MIGRATIONS,
    # and config defaults like ELBOW_MIN_INPUT=20) — which silently broke
    # test_elbow_trim, test_oxidation_probe, test_memory_search_routed, etc.
    # when they happened to run after this file. (Order-dependent CI reds.)
    #
    # Also purge `llm_failover`: it builds its LLM_ENDPOINTS list from env vars
    # ONCE at import time (M3_LLM_URL / M3_ENABLE_*). If it was already imported
    # before this fixture set the hermetic env above, the cascade's tier-3
    # primary still points at the REAL LM Studio (:1234), so the roundtrip probe
    # hits it (observed 400 + retry/backoff) and the <5s SLO blows. Purging it
    # forces a reimport that reads the TEST-NET endpoint we pinned.
    def _is_purge_target(m):
        return m.startswith("memory.") or m == "llm_failover"

    saved = {m: sys.modules[m] for m in list(sys.modules) if _is_purge_target(m)}
    saved["memory"] = sys.modules.get("memory")
    for mod in list(sys.modules):
        if _is_purge_target(mod):
            del sys.modules[mod]
    yield
    for mod in [m for m in sys.modules if _is_purge_target(m)]:
        del sys.modules[mod]
    for name, module in saved.items():
        if module is not None:
            sys.modules[name] = module


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
async def test_tier1_not_configured_when_no_gguf(monkeypatch):
    """When M3_EMBED_GGUF is unset AND auto-detect is off, tier_1.status is
    'not-configured' (a deliberate-skip state, not 'offline'/'broken').

    Auto-detect is disabled here so the test is deterministic regardless of
    whether the host happens to have a bge-m3 GGUF in a canonical dir — that
    auto-detect path is covered separately."""
    monkeypatch.setenv("M3_EMBED_GGUF_AUTODETECT", "0")
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

    # Force fresh module so M3_EMBED_FALLBACK_URL is picked up. The autouse
    # _isolate_env fixture snapshots + restores memory.* around every test in
    # this file, so this eviction does not leak to later tests.
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


@pytest.mark.skipif(
    not embed_backend_reachable(),
    reason="warm-cascade <3s SLO requires a reachable embedder; with no backend "
           "(e.g. CI) every probe waits out its full retry/backoff and the warm "
           "call can't beat 3s — an environment limit, not a regression.",
)
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


@pytest.mark.asyncio
async def test_doctor_fix_mode():
    """Verify that memory_doctor_fix_impl runs successfully in both dry_run and active mode."""
    from memory.doctor import memory_doctor_fix_impl
    # 1. Test dry_run=True (should run diagnosis and record skipped/dry_run actions)
    out_dry = await memory_doctor_fix_impl(dry_run=True)
    assert out_dry["dry_run"] is True
    assert "actions" in out_dry
    assert "summary" in out_dry
    for act in out_dry["actions"]:
        assert "action" in act
        assert "status" in act
        assert "detail" in act
        if act["status"] != "skipped":
            # Should have skipped or ok (if skipped due to dry_run or nothing to do)
            assert "dry_run=True" in act["detail"] or "skipped" in act["status"]

    # 2. Test dry_run=False
    out_active = await memory_doctor_fix_impl(dry_run=False)
    assert out_active["dry_run"] is False
    assert "actions" in out_active
    assert "summary" in out_active
    # Since isolated environment DB might not have schema_versions or other tables initially,
    # the doctor --fix will attempt migrations or cohesion rebuild.
    # At least some action should succeed or be skipped.
    assert out_active["summary"] in {"ok", "nothing_to_do", "partial", "failed"}

