"""Procedural distillation — autonomous tasks → `procedure` memories.

Covers memory_distill_procedures_impl (selection, the pluggable model call
stubbed, the backend-agnostic write via memory_write_impl, `distills_from`
provenance, and source PRESERVATION — sources are NOT soft-deleted, unlike belief
consolidation) plus the distill_procedures job's hard safety gate (no write
without BOTH --apply and M3_DISTILL_AUTO=1).

The distillation model + embedder are stubbed so the write path runs offline and
deterministically.
"""
from __future__ import annotations

import json
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


def _seed_completed_task(conn, *, task_id="t1", result_id="m-result",
                         conv="conv-1", user="u1"):
    """A completed task with a result memory and one sibling step memory."""
    conn.execute(
        "INSERT INTO memory_items (id, type, title, content, user_id, agent_id, "
        "conversation_id, created_at, is_deleted) VALUES (?,?,?,?,?,?,?,?,0)",
        (result_id, "note", "final result", "the deploy succeeded on retry",
         user, "claude", conv, "2026-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO memory_items (id, type, title, content, user_id, agent_id, "
        "conversation_id, created_at, is_deleted) VALUES (?,?,?,?,?,?,?,?,0)",
        ("m-step", "note", "step 1", "ran the migration first",
         user, "claude", conv, "2026-01-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO tasks (id, title, description, state, owner_agent, created_by, "
        "result_memory_id, created_at, updated_at, completed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (task_id, "Deploy the service", "deploy steps", "completed", "claude",
         "claude", result_id, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
         "2026-01-01T00:00:00Z"),
    )


def _patch_db(monkeypatch, db_path):
    import memory_core
    import memory_maintenance

    @contextmanager
    def fake_db(existing=None, *a, **k):
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

    monkeypatch.setattr(memory_core, "_db", fake_db)
    monkeypatch.setattr(memory_maintenance, "_db", fake_db)
    return memory_maintenance


def _patch_model_and_embed(monkeypatch, mm):
    """Stub the distillation model (return a canned procedure JSON) + the embedder
    that memory_write_impl invokes."""
    canned = json.dumps({
        "name": "Deploy the service safely",
        "procedure_kind": "runbook",
        "preconditions": ["migration ready"],
        "steps": ["run the migration", "deploy", "verify on retry"],
        "gotchas": ["first deploy can fail; retry"],
    })

    async def _fake_call(prompt):
        return canned

    monkeypatch.setattr(mm, "_distill_call_model", _fake_call)

    # memory_write_impl embeds via memory_core._embed; stub it deterministically.
    import memory_core

    async def _fake_embed(text, *a, **k):
        return ([0.1, 0.2], "test-embed")

    monkeypatch.setattr(memory_core, "_embed", _fake_embed)
    monkeypatch.setattr(memory_core.ctx, "get_secret", lambda *a, **k: "tok")


@pytest.mark.asyncio
async def test_distill_writes_procedure_with_provenance_and_preserves_sources(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_completed_task(conn)
        conn.commit()

    mm = _patch_db(monkeypatch, db)
    _patch_model_and_embed(monkeypatch, mm)

    out = await mm.memory_distill_procedures_impl(stale_days=0, threshold=1)
    assert "procedure" in out.lower()

    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        proc = conn.execute("SELECT * FROM memory_items WHERE type='procedure'").fetchone()
        assert proc is not None, "a procedure row should be written"
        meta = json.loads(proc["metadata_json"] or "{}")
        assert meta.get("procedure_kind") == "runbook"
        assert meta.get("steps"), "steps should ride metadata_json"
        assert meta.get("distilled_from_task") == "t1"

        edges = conn.execute(
            "SELECT COUNT(*) FROM memory_relationships "
            "WHERE from_id=? AND relationship_type='distills_from'",
            (proc["id"],),
        ).fetchone()[0]
        assert edges >= 2, "procedure must link to result + step sources"

        # Sources PRESERVED (not soft-deleted) — the key difference from belief
        # consolidation.
        n_live_sources = conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE id IN ('m-result','m-step') AND is_deleted=0"
        ).fetchone()[0]
        assert n_live_sources == 2, "distillation must NOT delete its sources"


@pytest.mark.asyncio
async def test_distill_dry_run_writes_nothing(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_completed_task(conn)
        conn.commit()
    mm = _patch_db(monkeypatch, db)

    out = await mm.memory_distill_procedures_impl(stale_days=0, threshold=1, dry_run=True)
    assert "DRY RUN" in out
    with sqlite3.connect(str(db)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM memory_items WHERE type='procedure'").fetchone()[0]
    assert n == 0


@pytest.mark.asyncio
async def test_distill_skips_when_no_completed_tasks(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)  # no tasks seeded
    mm = _patch_db(monkeypatch, db)
    out = await mm.memory_distill_procedures_impl(stale_days=0, threshold=1)
    assert "No procedural distillation" in out


# ── distill_procedures job safety gate ───────────────────────────────────────

@pytest.mark.asyncio
async def test_job_gate_forces_dry_run_without_env(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_completed_task(conn)
        conn.commit()
    mm = _patch_db(monkeypatch, db)
    _patch_model_and_embed(monkeypatch, mm)
    monkeypatch.delenv("M3_DISTILL_AUTO", raising=False)

    import distill_procedures
    out = await distill_procedures._run(apply=True, threshold=1, stale_days=0, max_procedures=5)
    assert "skipped-apply" in out and "DRY RUN" in out
    with sqlite3.connect(str(db)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM memory_items WHERE type='procedure'").fetchone()[0]
    assert n == 0, "no procedure should be written when the env gate is off"


@pytest.mark.asyncio
async def test_job_writes_when_apply_and_env_set(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_completed_task(conn)
        conn.commit()
    mm = _patch_db(monkeypatch, db)
    _patch_model_and_embed(monkeypatch, mm)
    monkeypatch.setenv("M3_DISTILL_AUTO", "1")

    import distill_procedures
    monkeypatch.setattr(distill_procedures, "_should_yield_to_user", lambda *a, **k: None)
    out = await distill_procedures._run(apply=True, threshold=1, stale_days=0, max_procedures=5)
    assert "skipped-apply" not in out
    with sqlite3.connect(str(db)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM memory_items WHERE type='procedure'").fetchone()[0]
    assert n >= 1


@pytest.mark.asyncio
async def test_job_defers_when_user_active(monkeypatch, tmp_path):
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_completed_task(conn)
        conn.commit()
    mm = _patch_db(monkeypatch, db)
    _patch_model_and_embed(monkeypatch, mm)
    monkeypatch.setenv("M3_DISTILL_AUTO", "1")

    import distill_procedures
    monkeypatch.setattr(distill_procedures, "_should_yield_to_user",
                        lambda *a, **k: "user active in the last 30s")
    out = await distill_procedures._run(apply=True, threshold=1, stale_days=0, max_procedures=5)
    assert "deferred" in out
    with sqlite3.connect(str(db)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM memory_items WHERE type='procedure'").fetchone()[0]
    assert n == 0, "a real write must defer when the host is busy"
