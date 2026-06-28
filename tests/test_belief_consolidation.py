"""Phase 4 tests — autonomous episodic->semantic belief consolidation.

Covers the new `belief` target_type on memory_consolidate_impl (high-confidence
belief row + `consolidates` edges + soft-deleted sources), the dry-run preview,
protected-type skipping, and the consolidate_beliefs job's hard safety gate
(no write without BOTH --apply and M3_CONSOLIDATION_AUTO=1).

The LLM + embedder are stubbed so the write path is exercised deterministically
offline.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


@pytest.fixture(autouse=True)
def _skip_migrations(monkeypatch):
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")


def _full_db(db_path):
    from conftest import create_full_main_schema
    create_full_main_schema(db_path)


def _seed_observations(conn, n, *, agent="claude", user="u1", typ="observation"):
    for i in range(n):
        conn.execute(
            "INSERT INTO memory_items (id, type, title, content, agent_id, user_id, "
            "created_at, importance, is_deleted) VALUES (?,?,?,?,?,?,?,?,0)",
            (f"obs-{i}", typ, f"obs {i}", f"the user did thing {i}", agent, user,
             "2026-01-01T00:00:00Z", 0.3),
        )


def _patch_db(monkeypatch, db_path):
    import memory_core
    import memory_maintenance

    @contextmanager
    def fake_db(existing=None, *a, **k):
        # Honor a passed-in connection (memory_link_impl passes db=conn) so
        # nested calls share the transaction instead of opening a new file.
        if existing is not None:
            yield existing
            return
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # Patch the SOURCE (memory_core._db): memory_maintenance imported it by
    # reference and memory.write looks it up as an override, so one patch here
    # reaches both the maintenance pass AND memory_link_impl's nested _db(db).
    monkeypatch.setattr(memory_core, "_db", fake_db)
    monkeypatch.setattr(memory_maintenance, "_db", fake_db)
    return memory_maintenance


def _patch_llm(monkeypatch, mm):
    """Stub the LLM + embedder so consolidation writes without a live server."""
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "CONSOLIDATED BELIEF TEXT"}}]}

    class _Client:
        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(mm, "_get_embed_client", lambda: _Client())

    async def _fake_best(client, token):
        return ("http://local", "test-model")

    monkeypatch.setattr(mm, "get_best_llm", _fake_best)

    async def _fake_embed(text):
        return ([0.1, 0.2], "test-embed")

    monkeypatch.setattr(mm, "_embed", _fake_embed)
    monkeypatch.setattr(mm.ctx, "get_secret", lambda *a, **k: "tok")


# ── belief target_type ───────────────────────────────────────────────────────

def test_belief_is_a_valid_memory_type():
    import mcp_tool_catalog
    assert "belief" in mcp_tool_catalog.VALID_MEMORY_TYPES


@pytest.mark.asyncio
async def test_consolidate_emits_belief_with_confidence_and_edges(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_observations(conn, 5)
        conn.commit()

    mm = _patch_db(monkeypatch, db)
    _patch_llm(monkeypatch, mm)

    # threshold=2 → 3 of the 5 observations get consolidated into one belief.
    out = await mm.memory_consolidate_impl(
        type_filter="observation", threshold=2, target_type="belief",
    )
    assert "belief" in out.lower()

    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        belief = conn.execute("SELECT * FROM memory_items WHERE type='belief'").fetchone()
        assert belief is not None, "a belief row should be written"
        assert belief["confidence"] == pytest.approx(0.85)
        edges = conn.execute(
            "SELECT COUNT(*) FROM memory_relationships WHERE from_id=? AND relationship_type='consolidates'",
            (belief["id"],),
        ).fetchone()[0]
        assert edges >= 1, "belief must link to its sources via consolidates edges"
        deleted = conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE type='observation' AND is_deleted=1"
        ).fetchone()[0]
        assert deleted >= 1, "consolidated sources must be soft-deleted (reversible)"


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_observations(conn, 5)
        conn.commit()
    mm = _patch_db(monkeypatch, db)
    out = await mm.memory_consolidate_impl(
        type_filter="observation", threshold=2, target_type="belief", dry_run=True,
    )
    assert "DRY RUN" in out
    with sqlite3.connect(str(db)) as conn:
        n_belief = conn.execute("SELECT COUNT(*) FROM memory_items WHERE type='belief'").fetchone()[0]
        n_deleted = conn.execute("SELECT COUNT(*) FROM memory_items WHERE is_deleted=1").fetchone()[0]
    assert n_belief == 0 and n_deleted == 0


@pytest.mark.asyncio
async def test_protected_types_never_consolidated(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_observations(conn, 5, typ="preference")  # protected
        conn.commit()
    mm = _patch_db(monkeypatch, db)
    out = await mm.memory_consolidate_impl(threshold=2, target_type="belief", dry_run=True)
    assert "No memory groups" in out  # preference is in DEFAULT_PROTECTED_TYPES


# ── consolidate_beliefs job safety gate ──────────────────────────────────────

@pytest.mark.asyncio
async def test_job_gate_forces_dry_run_without_env(monkeypatch, tmp_path):
    """--apply WITHOUT M3_CONSOLIDATION_AUTO=1 must still be a dry-run (safe
    scheduled no-op)."""
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_observations(conn, 5)
        conn.commit()
    mm = _patch_db(monkeypatch, db)
    _patch_llm(monkeypatch, mm)
    monkeypatch.delenv("M3_CONSOLIDATION_AUTO", raising=False)

    import consolidate_beliefs
    out = await consolidate_beliefs._run(apply=True, threshold=2, stale_days=0,
                                         source_type="observation")
    assert "skipped-apply" in out and "DRY RUN" in out
    with sqlite3.connect(str(db)) as conn:
        n_belief = conn.execute("SELECT COUNT(*) FROM memory_items WHERE type='belief'").fetchone()[0]
    assert n_belief == 0, "no belief should be written when the env gate is off"


@pytest.mark.asyncio
async def test_job_writes_when_apply_and_env_set(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_observations(conn, 5)
        conn.commit()
    mm = _patch_db(monkeypatch, db)
    _patch_llm(monkeypatch, mm)
    monkeypatch.setenv("M3_CONSOLIDATION_AUTO", "1")

    import consolidate_beliefs
    # Idle host: the governor/activity guard must not defer.
    monkeypatch.setattr(consolidate_beliefs, "_should_yield_to_user", lambda *a, **k: None)
    out = await consolidate_beliefs._run(apply=True, threshold=2, stale_days=0,
                                         source_type="observation")
    assert "skipped-apply" not in out
    with sqlite3.connect(str(db)) as conn:
        n_belief = conn.execute("SELECT COUNT(*) FROM memory_items WHERE type='belief'").fetchone()[0]
    assert n_belief >= 1


@pytest.mark.asyncio
async def test_apply_defers_when_user_active(monkeypatch, tmp_path):
    """A real write defers (writes nothing) when the governor/activity guard
    says the host is busy — exactly like memory_maintenance yields."""
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_observations(conn, 5)
        conn.commit()
    mm = _patch_db(monkeypatch, db)
    _patch_llm(monkeypatch, mm)
    monkeypatch.setenv("M3_CONSOLIDATION_AUTO", "1")

    import consolidate_beliefs
    monkeypatch.setattr(consolidate_beliefs, "_should_yield_to_user",
                        lambda *a, **k: "user active in the last 30s")
    out = await consolidate_beliefs._run(apply=True, threshold=2, stale_days=0,
                                         source_type="observation")
    assert "[deferred]" in out
    with sqlite3.connect(str(db)) as conn:
        n_belief = conn.execute("SELECT COUNT(*) FROM memory_items WHERE type='belief'").fetchone()[0]
    assert n_belief == 0, "deferred consolidation must write nothing"


@pytest.mark.asyncio
async def test_dry_run_not_deferred_by_governor(monkeypatch, tmp_path):
    """A dry-run is cheap+read-only, so the governor guard must NOT defer it —
    otherwise the scheduled no-op preview would confusingly skip."""
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_observations(conn, 5)
        conn.commit()
    _patch_db(monkeypatch, db)
    yielded = {"called": False}

    def _guard(*a, **k):
        yielded["called"] = True
        return "busy"

    import consolidate_beliefs
    monkeypatch.setattr(consolidate_beliefs, "_should_yield_to_user", _guard)
    # apply=False -> dry_run -> guard must not even be consulted.
    out = await consolidate_beliefs._run(apply=False, threshold=2, stale_days=0,
                                         source_type="observation")
    assert "[deferred]" not in out
    assert yielded["called"] is False


def test_consolidate_beliefs_main_importable():
    import inspect

    import consolidate_beliefs
    assert hasattr(consolidate_beliefs, "main")
    assert inspect.iscoroutinefunction(consolidate_beliefs._run)
