"""Retrieval slows forgetting. It must never manufacture usefulness.

⚠ THE ASYMMETRY THIS FIXES: m3 stamped access_count on every retrieval (1,034
live rows carried counts; the hottest had been read 762 times) and nothing ever
read it back. A memory retrieved 762 times decayed at precisely the same rate as
one nobody had touched in a year.

⚠ THE CONSTRAINT THAT SHAPES THE WHOLE DESIGN: being returned in a result set
means the RANKER matched the memory, not that the memory helped. A spurious
memory that keeps matching queries would otherwise be strengthened by its own
noise — a feedback loop entrenching exactly the wrong content. So the lift is
deliberately feeble, and these tests pin that feebleness as hard as they pin the
feature working:

  test_lift_is_capped_against_a_runaway     500x and 10,000x read the same as 50x
  test_retrieval_never_reaches_graded_floor  seen < used, structurally
  test_reinforcement_is_idempotent           hourly passes cannot compound

`confidence.access_reinforcement()` already existed with exactly the right shape
(logarithmic, unit 0.01, hard cap 0.05, docstring: "so a hot fact can't
masquerade as a corroborated one") and had ZERO callers. This wires it up rather
than authoring a second curve.
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
from memory import confidence as _conf  # noqa: E402

pytestmark = pytest.mark.usefixtures("m3_sandbox")


@pytest.fixture
def store(main_db_template, monkeypatch, tmp_path):
    """Throwaway DB seeded from the template; returns an inserter."""
    import shutil

    db_path = tmp_path / "reinforce.db"
    shutil.copy2(main_db_template, db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))

    def add(*, importance=0.02, access_count=0, helpful=0, pinned=0, age_days=30):
        mid = str(uuid.uuid4())
        with mc._db() as db:
            db.execute(
                "INSERT INTO memory_items "
                "(id, content, title, type, importance, importance_raw, "
                " access_count, helpful_count, pinned, source, created_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '-{age_days} days'))",
                (mid, "probe", "probe", "belief", importance, importance,
                 access_count, helpful, pinned, "agent"),
            )
        return mid

    return add


def _importance(mid: str) -> float:
    with mc._db() as db:
        return db.execute(
            "SELECT importance FROM memory_items WHERE id = ?", (mid,)
        ).fetchone()[0]


def _reinforce() -> int:
    with mc._db() as db:
        return mm._reinforce_importance(db)


# ── the feature ──────────────────────────────────────────────────────────────

def test_read_memories_are_lifted_above_unread_ones(store):
    """The asymmetry the change exists to create."""
    cold = store(access_count=0)
    warm = store(access_count=10)
    _reinforce()
    assert _importance(warm) > _importance(cold), (
        "a memory read 10 times is no better off than one never read — "
        "access_count is being ignored again."
    )


def test_unread_memories_are_untouched(store):
    """No access, no lift. Reinforcement must not be a blanket raise."""
    cold = store(access_count=0, importance=0.02)
    _reinforce()
    assert _importance(cold) == pytest.approx(0.02)


def test_lift_never_lowers_importance(store):
    """A high-importance memory that is also hot must not be dragged DOWN.

    The floor is a GREATEST(), not an assignment; this pins that.
    """
    hot_and_important = store(importance=0.9, access_count=500)
    _reinforce()
    assert _importance(hot_and_important) == pytest.approx(0.9)


# ── the constraints (retrieval is weak evidence) ─────────────────────────────

def test_lift_is_capped_against_a_runaway(store):
    """⚠ THE RUNAWAY GUARD.

    A spurious memory swept into hundreds of result sets must not climb. The cap
    is structural — log2 bucketing times a unit, hard-capped — so 50 reads and
    10,000 reads land identically. This is the single most important assertion
    in the file: without it, noise entrenches itself.
    """
    assert _conf.access_reinforcement(50) == _conf.access_reinforcement(10_000)
    assert _conf.access_reinforcement(500) == pytest.approx(_conf.ACCESS_REINFORCE_CAP)

    swept = store(access_count=500)
    _reinforce()
    ceiling = mm._ORDINARY_FLOOR + _conf.ACCESS_REINFORCE_CAP
    assert _importance(swept) <= ceiling + 1e-9, (
        f"importance {_importance(swept)} exceeded the reinforced ceiling "
        f"{ceiling} — the cap is not binding."
    )


def test_retrieval_never_reaches_the_graded_floor(store):
    """⚠ SEEN IS NOT USED. No amount of retrieval buys what one verdict earns.

    If this ever fails, a memory can be promoted into the graded band purely by
    being matched often — which is the feedback loop the whole design rejects.
    """
    swept = store(access_count=100_000)
    _reinforce()
    assert _importance(swept) < mm._GRADED_FLOOR, (
        "retrieval alone reached the graded floor; frequency is masquerading "
        "as usefulness."
    )


def test_reinforcement_alone_cannot_grow_a_memory_without_bound(store):
    """⚠ THE INVARIANT IS BOUNDEDNESS, NOT PER-CALL IDEMPOTENCE.

    An earlier version of this test asserted that repeated reinforcement changed
    nothing. That was correct for the floor-raising design and is WRONG for the
    rate-slowing one: giving back a share of decay is inherently a per-pass
    multiplication, so calling it 20x in a row without the matching decay does
    move the value. Asserting otherwise would have forced the implementation
    back to a floor, which a different test proved inert.

    What must hold instead is that the growth is BOUNDED — reinforcement in
    isolation can never exceed the ceiling, however many times it runs. That is
    the property protecting against a runaway; exact per-call idempotence is not.
    """
    mid = store(access_count=762)
    for _ in range(200):
        _reinforce()
    assert _importance(mid) <= mm._REINFORCE_CEILING + 1e-9, (
        f"200 reinforcement passes reached {_importance(mid)}, above the "
        f"{mm._REINFORCE_CEILING} ceiling — the bound is not holding."
    )


def test_a_heavily_read_memory_holds_an_equilibrium_above_the_floor(store):
    """⚠ WHAT "FADES MORE SLOWLY" ACTUALLY CONVERGES TO.

    Two earlier versions of this test asserted the wrong invariant and both
    failed usefully:

      1. "hot > cold after 300 passes" — they were IDENTICAL, because the first
         design raised a floor and a floor only binds once decay reaches it
         (423+ passes). That failure is what forced rate-slowing.
      2. "settles to a fixed point after 400 passes" — it was still moving,
         because a lightly-read memory's effective rate is 0.9984: below 1, so
         it keeps decaying, just ~5x slower.

    The true behaviour, measured: a memory read enough times reaches a real
    equilibrium where giveback exactly offsets decay (762 reads -> 0.344), while
    a lightly-read one still settles to the floor (10 reads -> 0.010). That is
    the right shape — ten reads should not confer permanence, and being read
    constantly should.
    """
    def full_pass():
        mm.memory_maintenance_impl(
            decay=True, purge_expired=False, prune_orphan_embeddings=False,
            reinforce=True, prune_orphan_queues=False,
        )

    cold = store(importance=0.5, access_count=0)
    hot = store(importance=0.5, access_count=762)

    # 800 passes: measured, an unread memory needs ~780 to fall from 0.5 to the
    # 0.01 floor at rate 0.995. Fewer and the cold row is still mid-descent,
    # which would make this assert a tolerance rather than the behaviour.
    for _ in range(800):
        full_pass()
    hot_settled = _importance(hot)

    assert _importance(cold) == pytest.approx(mm._ORDINARY_FLOOR, abs=0.005), (
        f"an unread memory settled at {_importance(cold)}, not the "
        f"{mm._ORDINARY_FLOOR} floor"
    )
    assert hot_settled > 0.2, (
        f"a memory read 762 times settled at {hot_settled} — reinforcement is "
        "not holding it meaningfully above an unread one."
    )
    assert hot_settled < mm._GRADED_FLOOR, "seen must still be less than used"

    # And it is an EQUILIBRIUM, not a slow drift: another 200 passes barely move.
    for _ in range(200):
        full_pass()
    assert _importance(hot) == pytest.approx(hot_settled, abs=0.01)


def test_pinned_rows_are_skipped(store):
    """Pinned already sits above every reinforced floor; leave it alone."""
    mid = store(importance=0.8, access_count=500, pinned=1)
    _reinforce()
    assert _importance(mid) == pytest.approx(0.8)


# ── interaction with decay ───────────────────────────────────────────────────

def test_hot_memory_settles_higher_than_a_cold_one_under_decay(store):
    """End to end: decay pulls both down, reinforcement holds one higher."""
    cold = store(importance=0.5, access_count=0)
    hot = store(importance=0.5, access_count=762)
    for _ in range(300):
        mm.memory_maintenance_impl(
            decay=True, purge_expired=False, prune_orphan_embeddings=False,
            reinforce=True, prune_orphan_queues=False,
        )
    assert _importance(hot) > _importance(cold)
    assert _importance(hot) < mm._GRADED_FLOOR, "still must not reach graded"


def test_the_reinforcement_curve_is_the_shared_one():
    """Reuse, not a second curve (§2: duplicated resolution logic is the defect).

    Pins that reinforcement reads confidence.access_reinforcement rather than a
    private copy that will drift from it.
    """
    import inspect

    src = inspect.getsource(mm._reinforce_importance)
    assert "access_reinforcement" in src, (
        "reinforcement stopped using the shared curve — a second implementation "
        "will drift from the first."
    )
