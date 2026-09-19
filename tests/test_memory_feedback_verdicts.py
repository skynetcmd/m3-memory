"""Every advertised `memory_feedback` verdict must do something observable.

⚠ THE DEFECT THIS PINS (found 2026-09-19): the ToolSpec enum has always been
("useful", "not_useful", "misleading"), but the impl branched on `useful` and
`wrong`. So `not_useful` and `misleading` fell through both branches and returned
"Feedback 'x' applied to <id>" having changed nothing. Two of three documented
signals were collected, reported as applied, and discarded.

A false success is a §3 violation exactly like a false alarm: the caller cannot
tell its signal was thrown away, so the data is lost AND nobody learns it is lost.
That is worse than an error, which at least surfaces.

The three verdicts are NOT three magnitudes of one dial — they are different
claims, and the tests below pin the distinction:

  useful      importance UP        "this did its job"
  misleading  importance DOWN      a claim about CORRECTNESS
  not_useful  importance UNCHANGED a RETRIEVAL MISS, not a memory defect

`not_useful` is the subtle one. A correct memory that keeps matching the wrong
query is the RANKER's failure; lowering its importance degrades good content to
punish a targeting error. test_not_useful_does_not_touch_importance is the
enforcement site for that.
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


@pytest.fixture
def memory_row():
    """One throwaway memory at a mid importance, cleaned up after."""
    mid = str(uuid.uuid4())
    with mc._db() as db:
        db.execute(
            "INSERT INTO memory_items (id, content, title, type, importance, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mid, "feedback verdict probe", "probe", "belief", 0.5, "agent"),
        )
    yield mid
    with mc._db() as db:
        db.execute("DELETE FROM memory_items WHERE id = ?", (mid,))


def _importance(mid: str) -> float:
    with mc._db() as db:
        return db.execute(
            "SELECT importance FROM memory_items WHERE id = ?", (mid,)
        ).fetchone()[0]


# ── each verdict must be observable ──────────────────────────────────────────

def test_useful_raises_importance(memory_row):
    """Unchanged from the original behaviour — existing workflows must not move."""
    before = _importance(memory_row)
    res = mm.memory_feedback_impl(memory_row, "useful")
    assert res["applied"] is True
    assert _importance(memory_row) > before


def test_misleading_lowers_importance(memory_row):
    """Was a silent no-op. A correctness claim must cost the memory something."""
    before = _importance(memory_row)
    res = mm.memory_feedback_impl(memory_row, "misleading")
    assert res["applied"] is True
    after = _importance(memory_row)
    assert after < before, (
        "`misleading` did not lower importance — this is the exact silent no-op "
        "the fix exists to remove."
    )


def test_not_useful_does_not_touch_importance(memory_row):
    """⚠ THE LOAD-BEARING ASSERTION.

    `not_useful` means the RANKER surfaced the wrong memory, not that the memory
    is bad. Lowering importance here degrades correct content because retrieval
    mis-targeted it. Ten misses in a row must still leave it untouched.
    """
    before = _importance(memory_row)
    for _ in range(10):
        res = mm.memory_feedback_impl(memory_row, "not_useful")
        assert res["applied"] is True
    assert _importance(memory_row) == pytest.approx(before), (
        "`not_useful` changed importance — a correct memory is being punished "
        "for a retrieval miss."
    )


def test_every_advertised_verdict_is_handled():
    """The enum and the impl must not drift apart again.

    Greps the ToolSpec rather than trusting a literal: the original defect was
    precisely that the spec and the impl disagreed about which strings exist.
    """
    import mcp_tool_catalog as cat

    spec = next(t for t in cat.TOOLS if t.name == "memory_feedback")
    advertised = set(spec.parameters["properties"]["feedback"]["enum"])
    assert advertised == set(mm._FEEDBACK_VERDICTS), (
        f"ToolSpec enum {sorted(advertised)} != impl verdicts "
        f"{sorted(mm._FEEDBACK_VERDICTS)} — the drift that caused the original "
        "silent no-ops."
    )


def test_verdicts_are_distinguishable(memory_row):
    """The three must produce three different outcomes, not two.

    Guards a future 'simplification' that collapses misleading and not_useful
    into one negative branch — which would re-introduce the retrieval-miss bug.
    """
    deltas = {}
    for verdict in mm._FEEDBACK_VERDICTS:
        before = _importance(memory_row)
        mm.memory_feedback_impl(memory_row, verdict)
        deltas[verdict] = round(_importance(memory_row) - before, 6)

    assert deltas["useful"] > 0
    assert deltas["misleading"] < 0
    assert deltas["not_useful"] == 0
    assert len(set(deltas.values())) == 3, f"verdicts collapsed: {deltas}"


# ── failure modes ────────────────────────────────────────────────────────────

def test_unknown_verdict_is_reported_not_silently_ignored(memory_row):
    """A rejected verdict must say so and name what IS accepted (§3)."""
    before = _importance(memory_row)
    res = mm.memory_feedback_impl(memory_row, "thumbs_sideways")
    assert res["ok"] is False
    assert res["applied"] is False
    assert res["error"] == "unknown_verdict"
    assert set(res["accepted"]) == set(mm._FEEDBACK_VERDICTS)
    assert _importance(memory_row) == pytest.approx(before)


def test_legacy_wrong_penalises_and_no_longer_deletes(memory_row):
    """`wrong` was an undocumented SOFT-DELETE reachable only from the CLI.

    It was never in the enum, so a schema-honouring client could not send it —
    but `m3 memory memory_feedback --feedback wrong` could, silently deleting a
    memory from a tool marked default_allowed=True. It is now an alias for
    `misleading`: penalise, never delete. Deletion is memory_delete's job and is
    not recoverable from one negative verdict.
    """
    res = mm.memory_feedback_impl(memory_row, "wrong")
    assert res["applied"] is True
    assert res["verdict"] == "misleading"
    with mc._db() as db:
        deleted = db.execute(
            "SELECT is_deleted FROM memory_items WHERE id = ?", (memory_row,)
        ).fetchone()[0]
    assert not deleted, "`wrong` still soft-deletes — the destructive path is back."


def test_importance_stays_in_range_under_repeated_verdicts(memory_row):
    """The clamps must hold: 30 pushes each way cannot escape [0, 1]."""
    for _ in range(30):
        mm.memory_feedback_impl(memory_row, "useful")
    assert _importance(memory_row) <= 1.0

    for _ in range(30):
        mm.memory_feedback_impl(memory_row, "misleading")
    assert _importance(memory_row) >= 0.0
