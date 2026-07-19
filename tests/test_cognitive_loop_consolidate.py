"""Tests for the belief-consolidation pass inside the cognitive loop.

The pass is event-driven (only fires when an aged source-type group exceeds the
threshold) and delegates to consolidate_beliefs._run, which carries the
governor/activity yield. Here we test the work-detection gate and that the pass
short-circuits when there's nothing to do.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import m3_cognitive_loop as cl  # noqa: E402


@pytest.fixture(autouse=True)
def _skip_migrations(monkeypatch):
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")


def _db_with_observations(tmp_path, n, *, typ="observation", created="2026-01-01T00:00:00Z"):
    from conftest import create_full_main_schema
    db = tmp_path / "t.db"
    create_full_main_schema(db)
    with sqlite3.connect(str(db)) as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO memory_items (id, type, title, content, agent_id, user_id, "
                "created_at, is_deleted) VALUES (?,?,?,?,?,?,?,0)",
                (f"o{i}", typ, "t", "c", "claude", "u1", created),
            )
        conn.commit()
    return str(db)


def test_has_consolidate_work_true_over_threshold(tmp_path):
    db = _db_with_observations(tmp_path, 5)
    assert cl.has_consolidate_work(db, "observation", threshold=2, stale_days=0) is True


def test_has_consolidate_work_false_under_threshold(tmp_path):
    db = _db_with_observations(tmp_path, 5)
    assert cl.has_consolidate_work(db, "observation", threshold=10, stale_days=0) is False


def test_has_consolidate_work_respects_stale_window(tmp_path):
    # All rows are old (2026-01-01); a 9999-day window excludes everything → no work.
    db = _db_with_observations(tmp_path, 5)
    assert cl.has_consolidate_work(db, "observation", threshold=2, stale_days=9999) is False


def test_has_consolidate_work_false_for_other_type(tmp_path):
    db = _db_with_observations(tmp_path, 5, typ="note")
    assert cl.has_consolidate_work(db, "observation", threshold=2, stale_days=0) is False


def test_has_consolidate_work_missing_db_is_safe(tmp_path):
    # A non-existent DB must not raise; conservative default is False (no LLM work).
    assert cl.has_consolidate_work(str(tmp_path / "nope.db"), "observation", 2, 0) is False


@pytest.mark.asyncio
async def test_run_consolidate_pass_skips_when_no_work(tmp_path, monkeypatch):
    """When there's no aged group over threshold, the pass must NOT invoke the
    (expensive) consolidate job at all."""
    db = _db_with_observations(tmp_path, 1)  # 1 obs, threshold 50 → no work
    called = {"run": False}

    import consolidate_beliefs

    async def _fake_run(*a, **k):
        called["run"] = True
        return ""

    monkeypatch.setattr(consolidate_beliefs, "_run", _fake_run)

    args = _Args(database=db, consolidate_source_type="observation",
                 consolidate_threshold=50, consolidate_stale_days=0)
    await cl.run_consolidate_pass(args)
    assert called["run"] is False


@pytest.mark.asyncio
async def test_run_consolidate_pass_invokes_job_when_work(tmp_path, monkeypatch):
    db = _db_with_observations(tmp_path, 5)  # group of 5, threshold 2 → work
    called = {"run": False, "kwargs": None}

    import consolidate_beliefs

    async def _fake_run(*a, **k):
        called["run"] = True
        called["kwargs"] = k
        return "ok"

    monkeypatch.setattr(consolidate_beliefs, "_run", _fake_run)

    args = _Args(database=db, consolidate_source_type="observation",
                 consolidate_threshold=2, consolidate_stale_days=0)
    await cl.run_consolidate_pass(args)
    assert called["run"] is True
    # The loop always passes apply=True; the job enforces the env/idle gate.
    assert called["kwargs"]["apply"] is True
    assert called["kwargs"]["source_type"] == "observation"


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        os.environ.setdefault("M3_DATABASE", kw.get("database", ""))


# ── Round-robin + idle-aware pass selection (_select_pass_order) ──────────────

_PASSES = [{"name": n} for n in ("entities", "enrich", "consolidate", "prune")]


def _names(order):
    return [p["name"] for p in order]


def test_pass_order_rotates_leader_each_cycle():
    # Over 4 cycles (idle), every pass leads exactly once — no starvation.
    leaders = [_names(cl._select_pass_order(_PASSES, c, active=False))[0] for c in range(4)]
    assert leaders == ["entities", "enrich", "consolidate", "prune"]


def test_pass_order_idle_runs_all_passes():
    order = cl._select_pass_order(_PASSES, cycle=0, active=False)
    assert _names(order) == ["entities", "enrich", "consolidate", "prune"]


def test_pass_order_active_runs_only_leader():
    # When active (throttled), exactly one pass runs — the rotated leader.
    assert _names(cl._select_pass_order(_PASSES, cycle=0, active=True)) == ["entities"]
    assert _names(cl._select_pass_order(_PASSES, cycle=1, active=True)) == ["enrich"]
    assert _names(cl._select_pass_order(_PASSES, cycle=2, active=True)) == ["consolidate"]


def test_pass_order_rotation_is_stable_over_large_cycle():
    # Cycle counter may grow unbounded; modulo keeps rotation correct.
    assert _names(cl._select_pass_order(_PASSES, cycle=4, active=False))[0] == "entities"
    assert _names(cl._select_pass_order(_PASSES, cycle=1000003, active=False))[0] == "prune"


def test_pass_order_preserves_relative_sequence():
    # Rotation is a rotation, not a reshuffle: order after the leader is preserved.
    assert _names(cl._select_pass_order(_PASSES, cycle=1, active=False)) == \
        ["enrich", "consolidate", "prune", "entities"]


def test_pass_order_empty_is_safe():
    assert cl._select_pass_order([], cycle=5, active=False) == []
    assert cl._select_pass_order([], cycle=5, active=True) == []


# ── Queue-aware selection (has_work filter) ───────────────────────────────────

def test_pass_order_none_work_map_is_backcompat():
    # has_work=None (or omitted) must behave exactly like before — rotate all.
    assert _names(cl._select_pass_order(_PASSES, cycle=0, active=False, has_work=None)) == \
        ["entities", "enrich", "consolidate", "prune"]


def test_pass_order_throttled_skips_empty_leader_for_backlogged_pass():
    # THE BUG THIS FIXES: throttled runs one pass. Cycle 1's positional leader is
    # 'enrich', but enrich's queue is empty and entities has a backlog. The empty
    # pass must be filtered out so the single slot goes to a pass with real work —
    # not wasted on a no-op while the entity backlog waits another cycle.
    work = {"entities": True, "enrich": False, "consolidate": False, "prune": False}
    # Only 'entities' is eligible, so it leads regardless of cycle.
    for c in range(4):
        assert _names(cl._select_pass_order(_PASSES, cycle=c, active=True, has_work=work)) == ["entities"]


def test_pass_order_absent_pass_is_kept():
    # Passes NOT in the map (time-driven sync/maintenance/audit) are always kept —
    # absence means "unknown eligibility, let the pass's own due-gate decide",
    # never "no work". Here 'prune' is absent from the map and must survive.
    work = {"entities": False, "enrich": False, "consolidate": False}  # 'prune' absent
    assert _names(cl._select_pass_order(_PASSES, cycle=0, active=False, has_work=work)) == ["prune"]


def test_pass_order_all_empty_returns_nothing():
    # If every pass is explicitly empty, no work runs this cycle (no crash, no
    # wasted no-op dispatch).
    work = {"entities": False, "enrich": False, "consolidate": False, "prune": False}
    assert cl._select_pass_order(_PASSES, cycle=0, active=True, has_work=work) == []
    assert cl._select_pass_order(_PASSES, cycle=0, active=False, has_work=work) == []


def test_pass_order_queue_aware_still_rotates_among_eligible():
    # Fairness preserved within the eligible set: with two backlogged passes,
    # the leader still rotates between them across cycles (idle mode).
    work = {"entities": True, "enrich": True, "consolidate": False, "prune": False}
    assert _names(cl._select_pass_order(_PASSES, cycle=0, active=True, has_work=work)) == ["entities"]
    assert _names(cl._select_pass_order(_PASSES, cycle=1, active=True, has_work=work)) == ["enrich"]
    assert _names(cl._select_pass_order(_PASSES, cycle=2, active=True, has_work=work)) == ["entities"]
