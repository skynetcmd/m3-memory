"""Decay writes its rate, and forgetting asymptotes instead of erasing.

⚠ TWO DEFECTS THIS PINS (measured 2026-09-19 over 4,034 live rows):

1. `decay_rate REAL DEFAULT 0.0` was declared in migration 001 and had NO WRITER
   anywhere outside pg-sync plumbing. Average across every row: exactly 0.0. The
   maintenance pass computed the factor on every sweep and threw it away, so the
   per-row decay state was unobservable — §3's "a column that reports a condition
   must have a writer that can set it".

2. Decay ran toward 0.0, so importance was bounded only by how long a memory had
   been ignored. Nothing a memory had EARNED could stop it. A floor makes
   forgetting asymptote rather than erase.

The floors are deliberately set AT OR BELOW what the corpus already held, so
enabling them moves no row on the first pass. That is what makes the change
observable in isolation: movement on pass one would be a bug, not the feature.
`test_floor_is_inert_on_first_pass` is the enforcement site.
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
from memory.backends import dialect as _dialect  # noqa: E402


def _decay_only():
    """Run ONLY the decay pass — the other passes touch unrelated state."""
    return mm.memory_maintenance_impl(
        decay=True, purge_expired=False, prune_orphan_embeddings=False,
        reinforce=False, prune_orphan_queues=False,
    )


# ⚠ m3_sandbox is REQUIRED on every test here, not optional hygiene. These tests
# INSERT and then run a maintenance pass that UPDATEs importance across the whole
# table — without the sandbox they mutate the developer's live memory store, and
# a stray teardown against a different active DB fails with "no such table:
# memory_items". conftest's m3_sandbox is the single source of truth for a
# hermetic environment (roots + M3_DATABASE pinned to tmp).
pytestmark = pytest.mark.usefixtures("m3_sandbox")


@pytest.fixture
def aged_row(main_db_template, monkeypatch, tmp_path):
    """A 30-day-old unpinned memory in a throwaway DB: inside the >7d window."""
    import shutil

    db_path = tmp_path / "decay_probe.db"
    shutil.copy2(main_db_template, db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    mid = str(uuid.uuid4())
    with mc._db() as db:
        db.execute(
            "INSERT INTO memory_items "
            "(id, content, title, type, importance, importance_raw, source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '-30 days'))",
            (mid, "decay probe", "probe", "belief", 0.5, 0.5, "agent"),
        )
    return mid


def _row(mid: str):
    with mc._db() as db:
        return db.execute(
            "SELECT importance, decay_rate, importance_raw FROM memory_items WHERE id = ?",
            (mid,),
        ).fetchone()


# ── the dead column gets a writer ────────────────────────────────────────────

def test_decay_records_its_rate(aged_row):
    """decay_rate must stop being 0.0-forever."""
    assert _row(aged_row)[1] in (0.0, None), "fixture should start un-decayed"
    _decay_only()
    imp, rate, _ = _row(aged_row)
    assert rate == pytest.approx(mm._DECAY_RATE), (
        "decay ran but decay_rate was not written — the column is inert again."
    )
    assert imp < 0.5, "importance did not actually decay"


def test_importance_raw_is_never_decayed(aged_row):
    """The undecayed baseline must survive every pass, or forensic reads lie."""
    for _ in range(5):
        _decay_only()
    imp, _, raw = _row(aged_row)
    assert raw == pytest.approx(0.5), "importance_raw was decayed — it is the baseline"
    assert imp < raw, "importance should have fallen below its raw original"


# ── the floor ────────────────────────────────────────────────────────────────

def test_decay_asymptotes_to_the_floor_instead_of_zero(aged_row):
    """500 passes must not erase a memory — that is the point of a floor."""
    for _ in range(500):
        _decay_only()
    imp = _row(aged_row)[0]
    assert imp >= mm._ORDINARY_FLOOR, (
        f"importance {imp} fell below the ordinary floor {mm._ORDINARY_FLOOR} — "
        "decay is still heading to zero."
    )


def test_graded_memories_get_a_higher_floor(aged_row):
    """A memory someone called helpful must not sink into the ordinary band."""
    with mc._db() as db:
        db.execute(
            "UPDATE memory_items SET helpful_count = 3, unhelpful_count = 0 WHERE id = ?",
            (aged_row,),
        )
    for _ in range(500):
        _decay_only()
    imp = _row(aged_row)[0]
    assert imp >= mm._GRADED_FLOOR, (
        f"graded memory sank to {imp}, below its {mm._GRADED_FLOOR} floor"
    )


def test_unhelpful_outweighing_helpful_drops_to_the_ordinary_floor(aged_row):
    """The graded floor is EARNED. More downvotes than up forfeits it.

    Pins that the floor reads BOTH counters rather than just helpful_count — a
    memory graded down more than up has not earned protection.
    """
    with mc._db() as db:
        db.execute(
            "UPDATE memory_items SET helpful_count = 1, unhelpful_count = 5 WHERE id = ?",
            (aged_row,),
        )
    for _ in range(500):
        _decay_only()
    imp = _row(aged_row)[0]
    assert imp < mm._GRADED_FLOOR, (
        "a net-negative memory kept the graded floor — the floor is ignoring "
        "unhelpful_count."
    )
    assert imp >= mm._ORDINARY_FLOOR


def test_pinned_rows_are_untouched_by_decay(main_db_template, monkeypatch, tmp_path):
    """Pinned is excluded from the UPDATE entirely; nothing should move."""
    import shutil
    db_path = tmp_path / "pinned_probe.db"
    shutil.copy2(main_db_template, db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    mid = str(uuid.uuid4())
    with mc._db() as db:
        db.execute(
            "INSERT INTO memory_items "
            "(id, content, title, type, importance, importance_raw, source, pinned, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, datetime('now', '-400 days'))",
            (mid, "pinned probe", "probe", "belief", 0.8, 0.8, "agent"),
        )
    try:
        for _ in range(50):
            _decay_only()
        imp = _row(mid)[0]
        assert imp == pytest.approx(0.8), f"pinned memory decayed to {imp}"
    finally:
        with mc._db() as db:
            db.execute("DELETE FROM memory_items WHERE id = ?", (mid,))


# ── the floors must not disturb the existing corpus ──────────────────────────

def test_floor_is_inert_on_first_pass(main_db_template, monkeypatch, tmp_path):
    """⚠ THE SAFETY PROPERTY.

    Every floor was chosen at or below what the live corpus already held, so
    switching them on changes nothing immediately — it only bounds future decay.
    If this fails, a floor was raised above real data and rows will JUMP on the
    next maintenance pass rather than simply stopping their descent.

    ⚠ THE CLAIM HAD TO BE NARROWED. The first version of this test asserted "no
    row moves, ever" and FAILED — correctly. A floor does not merely stop a
    descent: it RAISES any row already sitting below it. Seeding a row at 0.02
    against a 0.05 floor moves it to 0.05 on the first pass, by design.

    So the real property is conditional, and it is the one that made the floors
    safe to enable on the live corpus: **no row moves IF no row starts below its
    floor.** Verified against the real 4,034-row store when the floors were
    chosen (min importance 0.0969, floor 0.05 — 0 of 3,904 in-scope rows moved).
    This test reproduces that precondition on seeded data so it holds in CI too,
    then asserts the sub-floor rows behave the other way.
    """
    import shutil

    db_path = tmp_path / "inert_probe.db"
    shutil.copy2(main_db_template, db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    # All ABOVE their floor — the live-corpus precondition.
    above = ((0.30, 0), (0.41, 3), (0.90, 0))
    # All BELOW their floor — these SHOULD move, and the second assertion pins it.
    below = ((0.02, 0), (0.35, 3))
    with mc._db() as db:
        for imp, helpful in above + below:
            db.execute(
                "INSERT INTO memory_items "
                "(id, content, title, type, importance, importance_raw, "
                " helpful_count, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '-30 days'))",
                (str(uuid.uuid4()), "inert probe", "probe", "belief",
                 imp, imp, helpful, "agent"),
            )

    _d = _dialect()
    floor = mm._decay_floor_sql(_d)
    new = _d.greatest(floor, f"importance * {mm._DECAY_RATE}")
    old = _d.greatest("0.0", f"importance * {mm._DECAY_RATE}")
    age7 = _d.age_days_gt("created_at", "7")
    scope = (f"FROM memory_items WHERE is_deleted = 0 AND {age7} "
             f"AND COALESCE(pinned, 0) = 0")

    with mc._db() as db:
        # (a) Rows at or above their floor are untouched — the live-corpus case.
        moved_above = db.execute(
            f"SELECT COUNT(*) {scope} AND importance >= ({floor}) "
            f"AND ({new}) != ({old})"
        ).fetchone()[0]
        # (b) Rows below their floor are RAISED to it — the floor working.
        raised = db.execute(
            f"SELECT COUNT(*) {scope} AND importance < ({floor}) "
            f"AND ({new}) != ({old})"
        ).fetchone()[0]

    assert moved_above == 0, (
        f"{moved_above} rows at or above their floor would move on the first "
        "floored pass. A floor must bound a descent, never perturb a row that "
        "is already clear of it."
    )
    assert raised == len(below), (
        f"expected the {len(below)} sub-floor rows to be raised to their floor, "
        f"got {raised}. A floor that leaves rows beneath it is not a floor."
    )


def test_floor_ordering_is_coherent():
    """pinned > graded > ordinary, and all inside [0, 1]."""
    assert 0.0 <= mm._ORDINARY_FLOOR < mm._GRADED_FLOOR < mm._PINNED_FLOOR <= 1.0
    assert 0.0 < mm._DECAY_RATE < 1.0, "a rate >= 1 would never decay"
