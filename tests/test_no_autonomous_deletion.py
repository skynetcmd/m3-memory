"""An autonomous pass deranks. It does not delete.

⚠ THE DEFECT: `memory_maintenance_impl` ran a sweep that selected every memory
with `importance < 0.05` older than 30 days and set `is_deleted = 1`. It had
removed **5,001 memories** this way — every row in memory_archive carries
archive_reason='low_importance', and not one is a user deletion.

Forgetting is DERANKING. Decay already handles it: importance feeds the score
directly, so a stale memory sinks continuously and stays retrievable by content.
Removal is a decision a human or an explicitly-invoked tool makes.

⚠ IT WAS ALSO A HIDDEN COUPLING, which is how it went unnoticed. The `0.05`
literal in the delete silently tracked the decay floor. While the floor was also
0.05, decay asymptoted exactly AT the delete line and nothing ever crossed it —
so the destructive path looked dormant. Lowering the floor to 0.01 (a tuning
change to a *different* constant, made for unrelated reasons) would have
re-opened it against 3,742 aged rows. Two constants, no expressed relationship.

This suite pins both halves: the sweep must not delete, and what it already
deleted must be restorable.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import memory_core as mc  # noqa: E402
import memory_maintenance as mm  # noqa: E402

pytestmark = pytest.mark.usefixtures("m3_sandbox")


@pytest.fixture
def store(main_db_template, monkeypatch, tmp_path):
    import shutil

    db_path = tmp_path / "noautodel.db"
    shutil.copy2(main_db_template, db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    def add(*, importance=0.001, age_days=60, deleted=0, reason=None):
        mid = str(uuid.uuid4())
        with mc._db() as db:
            db.execute(
                "INSERT INTO memory_items "
                "(id, content, title, type, importance, importance_raw, "
                " is_deleted, source, created_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '-{age_days} days'))",
                (mid, "probe body", "probe", "belief", importance, importance,
                 deleted, "agent"),
            )
            if reason:
                db.execute(
                    "INSERT INTO memory_archive "
                    "(id, type, title, content, archive_reason, archived_at) "
                    "VALUES (?, ?, ?, ?, ?, datetime('now'))",
                    (mid, "belief", "probe", "probe body", reason),
                )
        return mid

    return add


def _alive(mid: str) -> bool:
    with mc._db() as db:
        row = db.execute(
            "SELECT is_deleted FROM memory_items WHERE id = ?", (mid,)
        ).fetchone()
    return row is not None and not row[0]


def _full_pass():
    return mm.memory_maintenance_impl(
        decay=True, purge_expired=False, prune_orphan_embeddings=False,
        reinforce=True, prune_orphan_queues=False,
    )


# ── the sweep must not delete ────────────────────────────────────────────────

@pytest.mark.parametrize("importance", [0.0, 0.001, 0.009, 0.02, 0.049])
def test_a_memory_below_every_threshold_is_never_auto_deleted(store, importance):
    """Any importance, any age — an autonomous pass must not remove it."""
    mid = store(importance=importance, age_days=365)
    for _ in range(5):
        _full_pass()
    assert _alive(mid), (
        f"a memory at importance={importance} was auto-deleted by the "
        "maintenance pass. Forgetting is deranking, not removal."
    )


def test_the_pass_reports_candidates_instead_of_acting(store):
    """Surfaced, not swept: the operator is told, and decides."""
    for _ in range(3):
        store(importance=0.001, age_days=90)
    out = str(_full_pass())
    assert "CANDIDATE" in out.upper(), (
        f"the pass did not surface archive candidates: {out}"
    )
    assert "archived 3" not in out.lower()


def test_the_candidate_threshold_tracks_the_floor():
    """The 0.05 literal was an undocumented duplicate of the decay floor.

    Deriving it makes the dependency explicit: retune the floor and the candidate
    band follows, instead of silently re-enabling a destructive path.
    """
    assert mm._ARCHIVE_CANDIDATE_THRESHOLD == pytest.approx(mm._ORDINARY_FLOOR * 5)
    assert mm._ARCHIVE_CANDIDATE_THRESHOLD > mm._ORDINARY_FLOOR, (
        "candidates must sit ABOVE the floor, or nothing is ever surfaced"
    )


# ── what was already deleted must come back ──────────────────────────────────

def test_autonomously_archived_memories_are_restorable(store):
    mid = store(importance=0.001, deleted=1, reason="low_importance")
    assert not _alive(mid)

    res = mm.memory_restore_impl(memory_id=mid, dry_run=False)
    assert res["ok"] and res["restored"] == 1
    assert _alive(mid), "an autonomously-removed memory could not be restored"


def test_restore_returns_them_deranked_not_promoted(store):
    """Back in the corpus, still at the bottom — not resurrected to prominence."""
    mid = store(importance=0.0, deleted=1, reason="low_importance")
    mm.memory_restore_impl(memory_id=mid, dry_run=False)
    with mc._db() as db:
        imp = db.execute(
            "SELECT importance FROM memory_items WHERE id = ?", (mid,)
        ).fetchone()[0]
    assert imp == pytest.approx(mm._ARCHIVE_CANDIDATE_THRESHOLD)
    assert imp < mm._GRADED_FLOOR, "restoring must not promote a memory"


def test_user_deletions_are_never_restored(store):
    """⚠ THE SAME PRINCIPLE, INVERTED.

    Overriding a human's deliberate deletion with a machine's restore is the
    identical category error as auto-deleting: a process deciding something that
    was a person's to decide.
    """
    mid = store(importance=0.9, deleted=1, reason="user_delete")
    res = mm.memory_restore_impl(dry_run=False)
    assert _alive(mid) is False, (
        "a user-deleted memory was swept back in by the bulk restore"
    )
    assert mid not in str(res)


def test_restore_rejects_a_non_autonomous_reason():
    res = mm.memory_restore_impl(reason="user_delete", dry_run=True)
    assert res["ok"] is False
    assert res["error"] == "reason_not_autonomous"
    assert res["accepted"] == list(mm._AUTONOMOUS_ARCHIVE_REASONS)


def test_restore_defaults_to_dry_run(store):
    """A write to the memory store should not happen because someone forgot."""
    mid = store(importance=0.001, deleted=1, reason="low_importance")
    res = mm.memory_restore_impl(memory_id=mid)
    assert res["applied"] is False
    assert res["would_restore"] == 1
    assert not _alive(mid), "dry_run restored the row anyway"
