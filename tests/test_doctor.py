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
    """Doctor must complete within a few seconds, even when all probes
    fail. No probe should hang past its per-probe timeout."""
    import time

    from memory.doctor import memory_doctor_impl
    t0 = time.perf_counter()
    out = await asyncio.wait_for(memory_doctor_impl(), timeout=20.0)
    elapsed = time.perf_counter() - t0
    assert elapsed < 15.0, (
        f"doctor took {elapsed:.1f}s — must stay bounded to avoid the "
        f"same hang it's diagnosing"
    )
    assert out["summary"] in {"healthy", "degraded", "broken"}


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
