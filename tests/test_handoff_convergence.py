"""Handoff code convergence: the queue owns state, memory_items owns payload.

P3 was originally a data migration merging handoffs into `notifications`. That
was cancelled after measuring the live store: all 15 handoffs carry
memory_embeddings and memory_items_fts rows, and `notifications` has neither, so
moving them would have silently ended semantic search over handoffs. The layers
stay separate and the CODE converges instead -- shared predicate owners, and a
notification that carries the memory_id rather than a copy of the payload.
"""
from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import memory_core  # noqa: E402
from memory.backends import dialect  # noqa: E402
from memory.orchestration import _db  # noqa: E402

_PREFIX = "pytest-conv"


@pytest.fixture
def clean():
    def _purge():
        with _db() as db:
            p = dialect().param()
            db.execute(
                f"DELETE FROM memory_items WHERE type = 'handoff' AND "
                f"(agent_id = {p} OR agent_id LIKE {p})",
                (_PREFIX, _PREFIX + "@%"),
            )
    _purge()
    yield
    _purge()


def _handoff_to(target: str, task: str) -> str:
    from datetime import datetime, timezone
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        p = dialect().param()
        db.execute(
            f"INSERT INTO memory_items (id, type, title, content, agent_id, "
            f"scope, metadata_json, created_at, updated_at, is_deleted) "
            f"VALUES ({p}, 'handoff', {p}, {p}, {p}, 'agent', {p}, {p}, {p}, 0)",
            (new_id, f"Handoff to {target}", task, target,
             json.dumps({"from_agent": "pytest"}), now, now),
        )
    return new_id


def _read_at(mid: str):
    with _db() as db:
        p = dialect().param()
        row = db.execute(
            f"SELECT read_at FROM memory_items WHERE id = {p}", (mid,)
        ).fetchone()
    return row["read_at"] if row else None


def test_ack_refuses_a_handoff_addressed_to_someone_else(clean):
    """THE gap. The ack took only a memory_id, so any agent could ack any
    handoff -- including one a sister was mid-flight on. notifications_ack_all
    has guarded this since #170; the handoff ack did not."""
    victim = _handoff_to(f"{_PREFIX}@s1", "work for s1")

    out = memory_core.memory_inbox_ack_impl(victim, agent_id=f"{_PREFIX}@s2")

    assert "Error" in out, f"a sister acked another's handoff: {out!r}"
    assert _read_at(victim) is None, "the row was acked despite the refusal"


def test_ack_accepts_the_addressee(clean):
    mine = _handoff_to(f"{_PREFIX}@s1", "work for s1")
    out = memory_core.memory_inbox_ack_impl(mine, agent_id=f"{_PREFIX}@s1")
    assert "Error" not in out, out
    assert _read_at(mine) is not None


def test_a_bare_type_may_ack_its_instances_mail(clean):
    """Same fan-out rule as the read that produced the list: a bare type covers
    the type and its instances, or an orchestrator could not clear a queue it
    can legitimately see."""
    mid = _handoff_to(f"{_PREFIX}@s1", "work for s1")
    out = memory_core.memory_inbox_ack_impl(mid, agent_id=_PREFIX)
    assert "Error" not in out, out
    assert _read_at(mid) is not None


def test_ack_without_an_agent_id_keeps_working(clean):
    """agent_id is optional by design: the ToolSpec injects it so the guard is
    on at the boundary, but internal callers that ack by id alone must not
    break at a distance."""
    mid = _handoff_to(f"{_PREFIX}@s1", "work for s1")
    out = memory_core.memory_inbox_ack_impl(mid)
    assert "Error" not in out, out
    assert _read_at(mid) is not None


def test_the_notification_carries_the_id_not_a_copy_of_the_payload():
    """Payload decoupling. The queue row must reference the memory, so the text
    keeps ONE owner -- the row that also has the FTS and embedding entries."""
    import inspect
    src = inspect.getsource(memory_core.memory_handoff_impl)
    assert '"memory_id": new_id' in src, (
        "the handoff notification must carry the memory_id"
    )
    assert '"content": ' not in src.split("notify_impl")[1][:400], (
        "the notification is duplicating the payload instead of referencing it"
    )


def test_a_failed_dispatch_is_reported_to_the_caller(monkeypatch, clean):
    """The memory row is committed before the notify, so a swallowed failure
    leaves a handoff nobody was told about while the sender reads 'created'."""
    def _boom(*a, **k):
        raise RuntimeError("queue down")

    monkeypatch.setattr(memory_core, "notify_impl", _boom)
    memory_core.agent_register_impl(f"{_PREFIX}@from", "test")
    memory_core.agent_register_impl(f"{_PREFIX}@to", "test")

    out = memory_core.memory_handoff_impl(
        from_agent=f"{_PREFIX}@from", to_agent=f"{_PREFIX}@to", task="t",
    )
    assert "NOT notified" in out, (
        f"a failed dispatch was reported as a clean success: {out!r}"
    )
