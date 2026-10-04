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


# ── a distilled task is never re-distilled (2026-10-04) ──────────────────────

@pytest.mark.asyncio
async def test_a_distilled_task_is_not_distilled_again(monkeypatch, tmp_path):
    """Nothing excluded already-distilled tasks: every pass re-distilled every
    completed task, and contradiction detection superseded the previous copy
    (~5 procedure writes per minute on 2026-10-04)."""
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_completed_task(conn)
        conn.commit()
    mm = _patch_db(monkeypatch, db)
    _patch_model_and_embed(monkeypatch, mm)

    first = await mm.memory_distill_procedures_impl(stale_days=0, threshold=1)
    assert "Distilled task t1" in first, first
    second = await mm.memory_distill_procedures_impl(stale_days=0, threshold=1)
    assert "Distilled task" not in second, second
    with sqlite3.connect(str(db)) as conn:
        n = conn.execute("SELECT COUNT(*) FROM memory_items WHERE type='procedure'").fetchone()[0]
    assert n == 1, f"exactly one procedure per task, found {n}"


def test_loop_work_gate_agrees_after_distillation(monkeypatch, tmp_path):
    """The loop's gate must stop reporting work once the task is distilled, or
    it keeps spending a model call per cycle on nothing."""
    monkeypatch.setenv("M3_DB_BACKEND", "sqlite")
    import m3_cognitive_loop as loop

    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_completed_task(conn)
        conn.commit()
    assert loop.has_distill_work(str(db), 1, 0) is True, "precondition: one undistilled task"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "INSERT INTO memory_items (id, type, title, content, metadata_json, created_at, is_deleted) "
            "VALUES ('proc-1', 'procedure', 'p', 'body', ?, '2026-01-01T00:00:00Z', 0)",
            (json.dumps({"distilled_from_task": "t1"}),),
        )
        conn.commit()
    assert loop.has_distill_work(str(db), 1, 0) is False


# ── a local LLM that cannot answer stops the pass (2026-10-03) ───────────────

def _seed_second_task(conn):
    conn.execute(
        "INSERT INTO memory_items (id, type, title, content, user_id, agent_id, "
        "conversation_id, created_at, is_deleted) VALUES (?,?,?,?,?,?,?,?,0)",
        ("m-result-2", "note", "second result", "it worked", "u1", "claude",
         "conv-2", "2026-01-02T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO tasks (id, title, description, state, owner_agent, created_by, "
        "result_memory_id, created_at, updated_at, completed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("t2", "Second task", "steps", "completed", "claude", "claude", "m-result-2",
         "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z"),
    )


@pytest.mark.asyncio
async def test_unavailable_llm_stops_the_pass_after_one_call(monkeypatch, tmp_path):
    """Measured 2026-10-03: with no model loaded, the pass asked the model once
    per task, every cycle (one task skipped 546 times in a day). A SYSTEM-down
    error must stop the pass after the first call, not fail each task."""
    import llm_failover

    db = tmp_path / "t.db"
    _full_db(db)
    with sqlite3.connect(str(db)) as conn:
        _seed_completed_task(conn)
        _seed_second_task(conn)
        conn.commit()
    mm = _patch_db(monkeypatch, db)
    monkeypatch.setattr(llm_failover, "_OUTAGE_STATE", {})
    calls = []

    async def _down(prompt):
        calls.append(prompt)
        raise llm_failover.LLMUnavailable("observed: no generative model listed")

    monkeypatch.setattr(mm, "_distill_call_model", _down)
    out = await mm.memory_distill_procedures_impl(stale_days=0, threshold=1)
    assert len(calls) == 1, f"pass must stop after the first system-down call, made {len(calls)}"
    assert "Stopped: local LLM unavailable" in out and "2 task(s) left" in out
    assert "Skipped task" not in out


@pytest.mark.asyncio
async def test_distill_call_classifies_connect_error_as_unavailable(monkeypatch):
    import httpx
    import llm_failover
    import memory_maintenance as mm
    import slm_intent

    async def _refused(prof, prompt, client):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(slm_intent, "_call_model", _refused)
    monkeypatch.delenv("M3_DISTILL_MODEL", raising=False)
    with pytest.raises(llm_failover.LLMUnavailable):
        await mm._distill_call_model("p")


@pytest.mark.asyncio
async def test_distill_call_keeps_a_request_error_per_task(monkeypatch, caplog):
    """A 4xx on a server that CAN answer is this task's problem: warn with the
    body and return None, so the pass moves on to the next task."""
    import httpx
    import memory_maintenance as mm
    import slm_intent

    async def _bad(prof, prompt, client):
        raise httpx.HTTPStatusError("400 Bad Request from x: prompt too long",
                                    request=httpx.Request("POST", "http://x"),
                                    response=httpx.Response(400))

    monkeypatch.setattr(slm_intent, "_call_model", _bad)
    monkeypatch.delenv("M3_DISTILL_MODEL", raising=False)
    with caplog.at_level("WARNING"):
        assert await mm._distill_call_model("p") is None
    assert any("prompt too long" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_llm_selector_with_no_model_is_loud(monkeypatch):
    """The 'llm' selector returned None with NO log when no endpoint had a model."""
    import llm_failover
    import memory_maintenance as mm

    async def _none(client, token):
        return None

    monkeypatch.setattr(mm, "get_best_llm", _none)
    monkeypatch.setenv("M3_DISTILL_MODEL", "llm")
    monkeypatch.setattr(mm.ctx, "get_secret", lambda *a, **k: "tok")
    with pytest.raises(llm_failover.LLMUnavailable):
        await mm._distill_call_model("p")
