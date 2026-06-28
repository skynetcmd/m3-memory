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
