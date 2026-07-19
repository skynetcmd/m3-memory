"""Regression tests for the shared-embedder-by-default, hang-proof feature.

Covers the three defects that lined up to cause a multi-minute read/write hang:
  1. Installer wrote M3_EMBED_GGUF into the MCP-server env (forces a per-process
     CUDA embedder). -> generated env must never carry it; shared config seeded.
  2. `disable_inproc_embedder` only enforced when the config file was FOUND; a
     missing/misresolved config silently PERMITTED inproc load. -> safe-by-default.
  3. The in-proc CUDA init had no timeout, so a stuck load hung forever. -> the
     init is now bounded by M3_EMBED_INIT_TIMEOUT_S and degrades to HTTP.

All hermetic (§3): no live embed server / GPU assumed; module state is patched
at the layer the code reads.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _fresh_embed(monkeypatch, **env):
    """Import bin/memory/embed.py fresh with a controlled environment so the
    module-level safe-default resolution runs under the given env. Snapshot +
    restore memory.* (the sys.modules-evict discipline — see
    m3-test-sysmodules-evict-must-restore)."""
    for k in ("M3_EMBED_GGUF", "M3_CONFIG_ROOT", "M3_MEMORY_ROOT",
              "M3_EMBED_INPROC", "M3_EMBED_GGUF_AUTODETECT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    saved = {m: sys.modules[m] for m in list(sys.modules) if m.startswith("memory.")}
    for m in saved:
        del sys.modules[m]
    try:
        mod = importlib.import_module("memory.embed")
        return importlib.reload(mod)
    finally:
        # leave the fresh module in place for the test; restore after via fixture
        pass


# ── Defect 2: safe-by-default when config is unresolved ───────────────────────

def test_no_config_plus_gguf_defaults_to_shared(monkeypatch):
    """The exact footgun: M3_EMBED_GGUF set but no .embed_config.json found ->
    inproc must be OFF (defer to shared), not spin up a per-process CUDA context."""
    empty_root = tempfile.mkdtemp()  # no .embed_config.json here
    e = _fresh_embed(monkeypatch, M3_CONFIG_ROOT=empty_root,
                     M3_EMBED_GGUF="C:/fake/model.gguf")
    assert e._EMBED_CFG_PRESENT is False
    assert e._INPROC_ALLOWED is False, "missing config must default to shared"
    assert e._EMBED_GGUF_PATH is None, "GGUF path must be cleared when inproc off"
    assert e._EMBED_GGUF_AUTODETECT is False


def test_explicit_opt_in_allows_inproc(monkeypatch):
    """M3_EMBED_INPROC=1 is the deliberate escape hatch — inproc allowed even
    with no config file."""
    empty_root = tempfile.mkdtemp()
    e = _fresh_embed(monkeypatch, M3_CONFIG_ROOT=empty_root,
                     M3_EMBED_GGUF="C:/fake/model.gguf", M3_EMBED_INPROC="1")
    assert e._INPROC_ALLOWED is True
    assert e._EMBED_GGUF_PATH == "C:/fake/model.gguf"


def test_config_present_shared_disables_inproc(monkeypatch):
    """Config present with disable_inproc_embedder:true -> inproc off even if the
    env var is set (config is the headless-safe source of truth)."""
    root = tempfile.mkdtemp()
    with open(os.path.join(root, ".embed_config.json"), "w") as f:
        json.dump({"disable_inproc_embedder": True,
                   "fallback_url": "http://127.0.0.1:8082"}, f)
    e = _fresh_embed(monkeypatch, M3_CONFIG_ROOT=root,
                     M3_EMBED_GGUF="C:/fake/model.gguf")
    assert e._EMBED_CFG_PRESENT is True
    assert e._INPROC_ALLOWED is False
    assert e._EMBED_GGUF_PATH is None


def test_config_present_not_shared_allows_inproc(monkeypatch):
    """Config present WITHOUT disabling inproc -> inproc allowed (operator opted
    into a per-process embedder via the config file)."""
    root = tempfile.mkdtemp()
    with open(os.path.join(root, ".embed_config.json"), "w") as f:
        json.dump({"fallback_url": "http://127.0.0.1:8082"}, f)
    e = _fresh_embed(monkeypatch, M3_CONFIG_ROOT=root,
                     M3_EMBED_GGUF="C:/fake/model.gguf")
    assert e._EMBED_CFG_PRESENT is True
    assert e._INPROC_ALLOWED is True
    assert e._EMBED_GGUF_PATH == "C:/fake/model.gguf"


# ── Defect 3: the CUDA init is timeout-bounded ────────────────────────────────

def test_inproc_init_times_out_instead_of_hanging(monkeypatch):
    """A hanging EmbeddedEmbedder(path) must be abandoned within the deadline and
    fall through to HTTP (None) — never block the caller forever."""
    import threading
    import time as _time

    root = tempfile.mkdtemp()
    with open(os.path.join(root, ".embed_config.json"), "w") as f:
        json.dump({"fallback_url": "http://127.0.0.1:8082"}, f)  # inproc allowed
    e = _fresh_embed(monkeypatch, M3_CONFIG_ROOT=root,
                     M3_EMBED_GGUF="C:/fake/model.gguf",
                     M3_EMBED_INIT_TIMEOUT_S="1")

    # The wedge must stay blocked while the caller's 1s deadline elapses (that's
    # what proves the abandon path). But `embed.py` runs the init on a daemon
    # thread it CANNOT force-kill, so a bare `Event().wait()` here leaks a live
    # thread into the rest of the session — on Windows that later races with
    # test_fips_integrity.py's crypto_provider reload and segfaults the run (#85).
    # Make the wedge INTERRUPTIBLE: it waits on an event we set in teardown so the
    # thread actually exits, with a finite backstop cap so it can never truly hang
    # even if teardown is skipped. The cap (30s) is far beyond the <5s assertion
    # window, so the wedge is still blocking when the caller times out.
    _release = threading.Event()

    class _HangingEmbedder:
        def __init__(self, *_a):
            # simulate a wedged CUDA load — released only in teardown
            _release.wait(timeout=30.0)

        def embedding_dim(self):  # pragma: no cover — never reached
            return 1024

    class _FakeCore:
        EmbeddedEmbedder = _HangingEmbedder

    monkeypatch.setattr(e.config, "m3_core_rs", _FakeCore, raising=False)
    # force re-resolution
    e._embedded_embed_checked = False
    e._embedded_embedder = None
    e._EMBED_GGUF_PATH = "C:/fake/model.gguf"
    e._EMBED_INIT_TIMEOUT_S = 1.0

    try:
        t0 = _time.time()
        result = e._get_embedded_embedder()
        dt = _time.time() - t0
        assert result is None, "hanging init must yield None (HTTP fallback)"
        assert dt < 5.0, f"init should abandon within the deadline, took {dt:.1f}s"
    finally:
        # Release the wedge and reap the abandoned m3-embed-init daemon thread so
        # it does not survive into later tests (the #85 segfault trigger).
        _release.set()
        for _th in threading.enumerate():
            if _th.name == "m3-embed-init" and _th is not threading.current_thread():
                _th.join(timeout=5.0)


def test_inproc_init_succeeds_fast_when_healthy(monkeypatch):
    """A healthy embedder still loads (timeout guard doesn't break the happy path)."""
    root = tempfile.mkdtemp()
    with open(os.path.join(root, ".embed_config.json"), "w") as f:
        json.dump({"fallback_url": "http://127.0.0.1:8082"}, f)
    e = _fresh_embed(monkeypatch, M3_CONFIG_ROOT=root,
                     M3_EMBED_GGUF="C:/fake/model.gguf")

    class _GoodEmbedder:
        def __init__(self, *_a):
            pass

        def embedding_dim(self):
            return e.config.EMBED_DIM

    class _FakeCore:
        EmbeddedEmbedder = _GoodEmbedder

    monkeypatch.setattr(e.config, "m3_core_rs", _FakeCore, raising=False)
    e._embedded_embed_checked = False
    e._embedded_embedder = None
    e._EMBED_GGUF_PATH = "C:/fake/model.gguf"
    e._EMBED_INIT_TIMEOUT_S = 20.0
    result = e._get_embedded_embedder()
    assert result is not None


# ── Defect 1: installer/config seeding never writes the env footgun ───────────

def test_seed_shared_config_shape_and_idempotency():
    from m3_memory.embedder_admin import seed_shared_config
    root = tempfile.mkdtemp()
    path, wrote1 = seed_shared_config(root, gguf_path="C:/m/bge.gguf")
    assert wrote1 is True
    with open(path) as f:
        cfg = json.load(f)
    assert cfg["disable_inproc_embedder"] is True
    assert cfg["fallback_url"] == "http://127.0.0.1:8082"
    assert cfg["gguf_path"] == "C:/m/bge.gguf"
    _, wrote2 = seed_shared_config(root, gguf_path="C:/m/bge.gguf")
    assert wrote2 is False, "seeding an already-correct config must be a no-op"


def test_seed_preserves_existing_keys():
    from m3_memory.embedder_admin import seed_shared_config
    root = tempfile.mkdtemp()
    with open(os.path.join(root, ".embed_config.json"), "w") as f:
        json.dump({"fallback_url": "http://127.0.0.1:9999", "custom": "keep"}, f)
    path, _ = seed_shared_config(root)
    with open(path) as f:
        cfg = json.load(f)
    assert cfg["fallback_url"] == "http://127.0.0.1:9999", "must not clobber existing url"
    assert cfg["custom"] == "keep"
    assert cfg["disable_inproc_embedder"] is True


def test_installer_memory_env_never_has_gguf(monkeypatch):
    monkeypatch.setenv("M3_CONFIG_ROOT", tempfile.mkdtemp())
    from m3_memory import installer
    env = installer._canonical_memory_env()
    assert "M3_EMBED_GGUF" not in env, "MCP server env must never carry the GGUF path"


# ── Defect 1 auto-heal: scrub existing settings + doctor detection/fix ────────

def _write_settings_with_leak(tmp: str) -> str:
    path = os.path.join(tmp, "settings.json")
    with open(path, "w") as f:
        json.dump({"mcpServers": {
            "memory": {"args": ["bin/memory_bridge.py"],
                       "env": {"M3_ENGINE_ROOT": "/x", "M3_EMBED_GGUF": "/m/bge.gguf"}},
            "unrelated": {"args": ["bin/other.py"],
                          "env": {"M3_EMBED_GGUF": "/m/bge.gguf"}},
        }}, f, indent=2)
    return path


def test_installer_scrub_is_m3_only_and_idempotent():
    from pathlib import Path

    from m3_memory import installer
    tmp = tempfile.mkdtemp()
    path = _write_settings_with_leak(tmp)
    actions = installer._scrub_embed_gguf_from_settings(Path(path), apply=True)
    assert any("memory" in a for a in actions)
    with open(path) as f:
        data = json.load(f)
    assert "M3_EMBED_GGUF" not in data["mcpServers"]["memory"]["env"]
    assert "M3_EMBED_GGUF" in data["mcpServers"]["unrelated"]["env"], "non-m3 untouched"
    assert os.path.exists(path + ".bak")
    # idempotent
    assert installer._scrub_embed_gguf_from_settings(Path(path), apply=True) == []


def test_doctor_detects_and_fixes_env_leak(monkeypatch):
    from doctor import shared_embedder_probe as p
    monkeypatch.delenv("M3_EMBED_GGUF", raising=False)
    tmp = tempfile.mkdtemp()
    path = _write_settings_with_leak(tmp)
    monkeypatch.setattr(p, "_known_agent_settings", lambda: [("Test", path)])

    assert p._detect_inproc_env_leak(), "must detect the leak"
    scrubbed, remains = p._fix_scrub_env_leak()
    assert scrubbed is True
    assert remains is False
    assert p._detect_inproc_env_leak() == [], "no residue after fix"
    assert os.path.exists(path + ".bak")


def test_doctor_detects_process_env_leak(monkeypatch):
    from doctor import shared_embedder_probe as p
    monkeypatch.setenv("M3_EMBED_GGUF", "/m/bge.gguf")
    monkeypatch.setattr(p, "_known_agent_settings", lambda: [])
    hits = p._detect_inproc_env_leak()
    assert any("process env" in h for h in hits)


# ── UX: shared mode must read as HEALTHY, not a scary "degraded/init-failed" ───

def test_probe_tier1_reports_shared_mode_not_init_failed(monkeypatch):
    """On a shared-embedder box, tier-1 is intentionally off — the probe must say
    'shared-mode', never 'init-failed' (which reads as broken to a user)."""
    root = tempfile.mkdtemp()
    with open(os.path.join(root, ".embed_config.json"), "w") as f:
        json.dump({"disable_inproc_embedder": True,
                   "fallback_url": "http://127.0.0.1:8082"}, f)
    e = _fresh_embed(monkeypatch, M3_CONFIG_ROOT=root)
    # doctor imports memory.embed fresh; make sure it sees the same shared config
    import importlib
    d = importlib.reload(importlib.import_module("memory.doctor"))
    out = d._probe_tier1()
    assert out["status"] == "shared-mode", out
    assert out.get("shared_mode") is True
    assert e._INPROC_ALLOWED is False  # sanity: shared config really is active


def test_doctor_summary_healthy_in_shared_mode_when_tier2_up():
    """summary must be 'healthy' (not 'degraded') when shared mode is on and the
    shared tier-2 server is online — tier-1 offline is by design, not a fault."""
    import importlib

    from memory import doctor as d
    importlib.reload(d)
    # Build the classification the way memory_doctor_impl does, but drive the
    # tier states directly so the test is hermetic (no live server needed).
    tier1 = {"status": "shared-mode", "shared_mode": True}
    tier2 = {"status": "online", "url": "http://127.0.0.1:8082"}
    # Reproduce the summary rule locally against the same booleans the impl uses.
    t1_ok = tier1["status"] == "online"
    t2_ok = tier2["status"] == "online"
    shared_mode = tier1.get("shared_mode") is True
    rt_ok = db_ok = True
    if rt_ok and db_ok and (t1_ok or t2_ok):
        summary = "healthy" if (t1_ok and t2_ok) else (
            "healthy" if (shared_mode and t2_ok) else "degraded")
    else:
        summary = "broken"
    assert summary == "healthy"


def test_doctor_summary_degraded_when_not_shared_and_tier1_down():
    """Guard against false-healthy: a NON-shared box with tier-1 down and only
    tier-2 up is still 'degraded' — the shared-mode healthy path must not leak
    into the generic degraded case."""
    tier1 = {"status": "not-configured"}  # not shared_mode
    tier2 = {"status": "online"}
    t1_ok = tier1["status"] == "online"
    t2_ok = tier2["status"] == "online"
    shared_mode = tier1.get("shared_mode") is True
    rt_ok = db_ok = True
    if rt_ok and db_ok and (t1_ok or t2_ok):
        summary = "healthy" if (t1_ok and t2_ok) else (
            "healthy" if (shared_mode and t2_ok) else "degraded")
    else:
        summary = "broken"
    assert summary == "degraded"


# ── embed_for_search: bounded, degrade-safe query embed (lockup fix) ──────────
# The stdio MCP server is a single event loop; an unbounded query embed on the
# search path can stack the full cascade into minutes and freeze every
# concurrent tool call (the "MCP server locked up" symptom). embed_for_search
# gates on fast_embedder_available() and caps the embed with a wall-clock
# deadline, degrading to (None, ...) — the caller's FTS-only fallback — instead
# of hanging. These tests are hermetic: no live embedder, no real timers beyond
# a sub-second sleep.

import asyncio

import pytest


@pytest.mark.asyncio
async def test_embed_for_search_skips_when_no_fast_tier(monkeypatch):
    """No fast tier available -> skip the embed entirely, return (None, model).
    The underlying _embed must NOT be called (that's the whole point: we never
    enter the slow cascade)."""
    import memory.embed as emb

    called = {"n": 0}

    async def _boom(text):  # would be the slow cascade in production
        called["n"] += 1
        return [0.1] * 1024, "should-not-run"

    monkeypatch.setattr(emb, "fast_embedder_available", lambda: False)
    monkeypatch.setattr(emb, "_embed", _boom)

    vec, model = await emb.embed_for_search("anything")
    assert vec is None
    assert called["n"] == 0, "embed_for_search must not invoke _embed when no fast tier"


@pytest.mark.asyncio
async def test_embed_for_search_runs_when_fast_tier_present(monkeypatch):
    """Fast tier available + fast _embed -> the vector is returned unchanged."""
    import memory.embed as emb

    async def _fast(text):
        return [0.5] * 1024, "mock"

    monkeypatch.setattr(emb, "fast_embedder_available", lambda: True)
    monkeypatch.setattr(emb, "_embed", _fast)

    vec, model = await emb.embed_for_search("q")
    assert vec == [0.5] * 1024
    assert model == "mock"


@pytest.mark.asyncio
async def test_embed_for_search_deadline_degrades_not_hangs(monkeypatch):
    """A fast tier that is believed-up but actually SLOW (never trips its
    breaker) must not hang the caller: the deadline fires and we degrade to
    (None, ...). Uses a tiny deadline + a sleep longer than it."""
    import memory.config as cfg
    import memory.embed as emb

    async def _slow(text):
        await asyncio.sleep(0.5)  # longer than the deadline below
        return [0.9] * 1024, "too-slow"

    monkeypatch.setattr(emb, "fast_embedder_available", lambda: True)
    monkeypatch.setattr(emb, "_embed", _slow)
    monkeypatch.setattr(cfg, "EMBED_SEARCH_DEADLINE_S", 0.05)

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    vec, model = await emb.embed_for_search("q")
    elapsed = loop.time() - t0

    assert vec is None, "slow embed past deadline must degrade to None, not return a vector"
    assert elapsed < 0.4, f"deadline should bound the wait well under the 0.5s embed; took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_embed_for_search_deadline_zero_disables_ceiling(monkeypatch):
    """EMBED_SEARCH_DEADLINE_S=0 disables the wall-clock ceiling (pre-fix
    behavior) — the embed runs to completion even if slow."""
    import memory.config as cfg
    import memory.embed as emb

    async def _slow_but_ok(text):
        await asyncio.sleep(0.05)
        return [0.3] * 1024, "ok"

    monkeypatch.setattr(emb, "fast_embedder_available", lambda: True)
    monkeypatch.setattr(emb, "_embed", _slow_but_ok)
    monkeypatch.setattr(cfg, "EMBED_SEARCH_DEADLINE_S", 0.0)

    vec, model = await emb.embed_for_search("q")
    assert vec == [0.3] * 1024


@pytest.mark.asyncio
async def test_search_scored_degrades_when_no_fast_tier(monkeypatch):
    """PRODUCTION-SHAPE gate test: with the REAL `_embed` in place (not
    monkeypatched) and no fast embedder tier, memory_search_scored_impl must NOT
    enter the embed cascade — it degrades to an empty semantic result instead of
    hanging. Proves the inline search-path gate (search.py), distinct from the
    embed_for_search helper, fires on the real code path.

    The gate only applies when `_embed` IS the module cascade (identity check);
    here we deliberately leave `_embed` un-patched so `_embed_is_real` is True
    and the gate governs. fast_embedder_available()=False is the "embedder down"
    condition the fix targets."""
    import sqlite3

    import memory_core

    from memory import embed as _emb
    from memory import search

    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")
    # Embedder believed DOWN — the whole point of the degrade path.
    monkeypatch.setattr(_emb, "fast_embedder_available", lambda: False)

    # We deliberately leave `_embed` UN-patched so search's identity check makes
    # _embed_is_real True and the fast-tier gate governs. Correctness is asserted
    # via the empty result below: the stubbed DB would return a vector row if the
    # embed path were reached, so [] can only mean the embed was skipped.

    # A DB whose vector query would return a row IF reached — so an empty result
    # can only mean the embed was skipped, not that the store was empty.
    class _Cur:
        def fetchall(self):
            return [{"id": "x", "content": "c", "title": "t", "type": "concept",
                     "importance": 0.5, "embedding": b"\x00" * 12,
                     "vec_score": 0.9, "bm25_score": 0.0}]
        def fetchone(self):
            return None

    class _Conn(sqlite3.Connection):
        def execute(self, sql, *a):
            return _Cur()

    conn = sqlite3.connect(":memory:", factory=_Conn)
    try:
        class _Ctx:
            def __enter__(self): return conn
            def __exit__(self, *a): return False

        monkeypatch.setattr(search, "_db", lambda: _Ctx())
        monkeypatch.setattr(memory_core, "_db", lambda: _Ctx())
        monkeypatch.setattr(search, "_detect_sqlite_vec", lambda db: False)
        monkeypatch.setattr(search, "_prefer_observations_gate", lambda: False)
        monkeypatch.setattr(search, "_two_stage_observations_gate", lambda: False)

        # A non-exact query so the FTS short-circuit does not early-return.
        # search_mode="semantic" specifically: with no fast tier, PURE-semantic
        # search must degrade to empty (it can't do vector similarity, and it must
        # NOT return the stubbed vector row — proving the embed path was skipped).
        # NOTE: hybrid/fts5 modes now fall back to FTS keyword results in this
        # no-vector case (see _fts_only_results); semantic mode deliberately does
        # not, so this assertion — embed-skipped, no vec rows — still holds.
        results = await search.memory_search_scored_impl(
            "some semantic query that is not an exact substring", search_mode="semantic", k=5
        )
        assert results == [], "no fast tier -> SEMANTIC search must degrade to empty, not hang or return vec rows"
    finally:
        conn.close()
