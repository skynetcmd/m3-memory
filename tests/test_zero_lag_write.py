"""Tests for zero-lag memory_write embedding deferral (§3/§8).

When no FAST embedder is available (tier-1 in-process absent AND tier-2 CPU-HTTP
breaker open/unconfigured), memory_write must NOT block on the slow HTTP cascade
(tier-2 30s read + tier-3 retries + 30s semaphore, per chunk = minutes). It must
persist the verbatim row (FTS-searchable now) and defer the vector to
embed_backfill (which selects WHERE NOT EXISTS an embedding row).

Correctly-configured installs (fast embedder present) are unaffected — they embed
inline exactly as before.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from memory import embed as embed_mod  # noqa: E402


def test_fast_embedder_available_false_without_tier1_or_tier2(monkeypatch):
    """No in-process embedder and an open tier-2 breaker => not fast."""
    monkeypatch.setattr(embed_mod, "_get_embedded_embedder", lambda: None)

    class _OpenBreaker:
        def allow_request(self):
            return False

    monkeypatch.setattr(embed_mod, "_CPU_FALLBACK_BREAKER", _OpenBreaker())
    assert embed_mod.fast_embedder_available() is False


def test_fast_embedder_available_true_with_tier1(monkeypatch):
    """In-process embedder present => fast, regardless of tier-2."""
    monkeypatch.setattr(embed_mod, "_get_embedded_embedder", lambda: object())
    assert embed_mod.fast_embedder_available() is True


def test_fast_embedder_available_true_with_healthy_tier2(monkeypatch):
    """No tier-1 but a closed tier-2 breaker + configured URL => fast."""
    monkeypatch.setattr(embed_mod, "_get_embedded_embedder", lambda: None)

    class _ClosedBreaker:
        def allow_request(self):
            return True

    monkeypatch.setattr(embed_mod, "_CPU_FALLBACK_BREAKER", _ClosedBreaker())
    monkeypatch.setattr(embed_mod, "_EMBED_FALLBACK_URL", "http://127.0.0.1:8082")
    assert embed_mod.fast_embedder_available() is True


@pytest.mark.asyncio
async def test_write_defers_and_is_fast_without_embedder(monkeypatch, tmp_path):
    """A write with no fast embedder returns quickly, marks deferral, and leaves
    NO embedding row (so embed_backfill will pick it up by construction)."""
    import memory_core as mc
    import migrate_memory
    from m3_sdk import active_database

    db = str(tmp_path / "zerolag.db")
    monkeypatch.setenv("M3_DATABASE", db)
    try:
        migrate_memory.run_migrations(db)
    except Exception:
        pass

    # Force "no fast embedder". The write path calls
    # `_embed_mod.fast_embedder_available()`, where _embed_mod is whatever object
    # memory.write bound via `from . import embed`. Under the full suite another
    # test evicts+reimports memory.*, so the module the write path holds can be a
    # DIFFERENT object than this file's `embed_mod` — patching only embed_mod then
    # misses (isolated test passed, full-suite run attempted a real embed and did
    # not defer). Patch the exact object the code under test dereferences.
    import memory.write as _write_mod
    for _target in {embed_mod, _write_mod._embed_mod}:
        monkeypatch.setattr(_target, "fast_embedder_available", lambda: False)

    with active_database(db):
        t = time.time()
        r = await mc.memory_write_impl("note", "deferred embed content", title="z1")
        dt = time.time() - t

    assert dt < 5.0, f"deferred write should be fast, took {dt:.1f}s"
    assert "deferred" in r
    item_id = r.split("Created: ")[1].split(" ")[0]

    import sqlite3
    conn = sqlite3.connect(db)
    try:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM memory_embeddings WHERE memory_id = ?", (item_id,)
        ).fetchone()
        (rows,) = conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE id = ?", (item_id,)
        ).fetchone()
    finally:
        conn.close()

    assert rows == 1, "verbatim row must be persisted"
    assert n == 0, "no embedding row yet — backfill candidate by construction"
