"""Graded feedback: post-answer, two counters, time-bounded, never silent.

`memory_grade` is the signal m3 lacked — the difference between a memory being
RETRIEVED and a memory being USED. access_count already says "seen"; a grade says
"I relied on this".

Three properties this suite pins, each of which was a design decision:

1. TWO COUNTERS, NEVER A NET. +5/-5 (contested, heavily used) and +0/-0 (never
   seen) net identically while meaning opposite things, and asymmetric weights
   cannot be applied to a pre-summed number. Same reasoning the confidence model
   already uses for corroboration_count / contradiction_count.

2. TIME-BOUNDED. Without a window, memory_id is an unauthenticated write
   primitive — anyone could grade any UUID having never retrieved it. The window
   makes the retrieval itself the capability.

3. A LATE GRADE IS REPORTED, NOT DROPPED. Silently accepting a stale grade would
   repeat the false-success defect this module was just fixed for
   (memory_feedback's not_useful returning "applied" while doing nothing).

⚠ THE BUG THIS SUITE CAUGHT DURING DEVELOPMENT, and why the fixture below writes
timestamps the way it does: the first implementation compared timestamps as
STRINGS via the now_minus_minutes seam. m3 writes at least three formats to these
columns — Python's `...+00:00` (memory/db.py's flusher, the production one),
SQLite's space-separated `datetime('now')`, and the seam's `...Z`. Because
ord('+') < ord('Z'), a real production timestamp sorts BEFORE an identical
instant written with Z, so every grade was rejected including ones milliseconds
old. A test seeded with `datetime('now')` would NOT have caught it. These
fixtures use `datetime.now(timezone.utc).isoformat()` — exactly what production
writes — and the fix was a new dialect.age_minutes_lt() that compares PARSED
instants.
"""
from __future__ import annotations

import datetime as dt
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

    db_path = tmp_path / "grade.db"
    shutil.copy2(main_db_template, db_path)
    monkeypatch.setenv("M3_DATABASE", str(db_path))
    mm._FEEDBACK_WINDOW_REJECTIONS.update({"accepted": 0, "rejected": 0})

    def add(*, accessed_minutes_ago=0, deleted=0):
        mid = str(uuid.uuid4())
        now = dt.datetime.now(dt.timezone.utc)
        # ⚠ PRODUCTION FORMAT — isoformat() with a +00:00 offset, exactly what
        # memory/db.py's access flusher writes. Seeding with SQLite's
        # datetime('now') instead would hide the string-compare bug.
        accessed = (now - dt.timedelta(minutes=accessed_minutes_ago)).isoformat()
        with mc._db() as db:
            db.execute(
                "INSERT INTO memory_items "
                "(id, content, title, type, importance, importance_raw, "
                " is_deleted, source, last_accessed_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, "probe", "probe", "belief", 0.5, 0.5, deleted, "agent",
                 accessed, now.isoformat()),
            )
        return mid

    return add


def _counts(mid: str):
    with mc._db() as db:
        row = db.execute(
            "SELECT COALESCE(helpful_count,0), COALESCE(unhelpful_count,0) "
            "FROM memory_items WHERE id = ?", (mid,)
        ).fetchone()
    return (row[0], row[1])


# ── the happy path ───────────────────────────────────────────────────────────

def test_a_fresh_grade_lands(store):
    mid = store(accessed_minutes_ago=0)
    res = mm.memory_grade_impl(grades=[{"memory_id": mid, "verdict": "helpful"}])
    assert res["applied"] is True and res["graded"] == 1
    assert _counts(mid) == (1, 0)


def test_helpful_and_unhelpful_are_separate_counters(store):
    """⚠ NEVER A NET. The two must be independently readable."""
    mid = store()
    for _ in range(5):
        mm.memory_grade_impl(grades=[{"memory_id": mid, "verdict": "helpful"}])
    for _ in range(5):
        mm.memory_grade_impl(grades=[{"memory_id": mid, "verdict": "unhelpful"}])

    helpful, unhelpful = _counts(mid)
    assert (helpful, unhelpful) == (5, 5), (
        "a +5/-5 memory must record BOTH counts. If these were netted it would "
        "be indistinguishable from a memory nobody ever saw."
    )


def test_contested_is_distinguishable_from_ignored(store):
    """The exact case a net count destroys."""
    contested = store()
    ignored = store()
    for _ in range(5):
        mm.memory_grade_impl(grades=[{"memory_id": contested, "verdict": "helpful"}])
        mm.memory_grade_impl(grades=[{"memory_id": contested, "verdict": "unhelpful"}])

    assert _counts(contested) == (5, 5)
    assert _counts(ignored) == (0, 0)
    assert _counts(contested) != _counts(ignored), (
        "contested and ignored memories are indistinguishable — the counters "
        "have been collapsed into a net somewhere."
    )


def test_one_call_grades_many(store):
    """Bulk by design: ten results must not be ten round trips (§4)."""
    ids = [store() for _ in range(10)]
    res = mm.memory_grade_impl(
        grades=[{"memory_id": m, "verdict": "helpful"} for m in ids]
    )
    assert res["graded"] == 10
    assert all(_counts(m) == (1, 0) for m in ids)


# ── the window ───────────────────────────────────────────────────────────────

def test_a_stale_grade_is_rejected_not_applied(store):
    mid = store(accessed_minutes_ago=60)
    res = mm.memory_grade_impl(grades=[{"memory_id": mid, "verdict": "helpful"}])
    assert res["applied"] is False
    assert res["rejected_stale"] == 1
    assert res["reason"] == "outside_feedback_window"
    assert _counts(mid) == (0, 0), (
        "the response said applied=false but the counter moved anyway — the "
        "same class of lie as a false success."
    )


def test_a_rejection_says_what_was_observed_and_what_to_do(store):
    """§3 evidence levels: state the observation, name the knob."""
    mid = store(accessed_minutes_ago=60)
    res = mm.memory_grade_impl(grades=[{"memory_id": mid, "verdict": "helpful"}])
    assert "observed" in res and "inspect" in res
    assert "feedback_window_minutes" in res["inspect"]


def test_the_window_boundary_holds_on_production_timestamps(store):
    """Ordinary in/out behaviour on production-format timestamps."""
    inside = store(accessed_minutes_ago=1)
    outside = store(accessed_minutes_ago=30)
    res = mm.memory_grade_impl(
        grades=[{"memory_id": inside, "verdict": "helpful"},
                {"memory_id": outside, "verdict": "helpful"}],
        window_minutes=5,
    )
    assert res["graded"] == 1 and res["rejected_stale"] == 1
    assert _counts(inside) == (1, 0)
    assert _counts(outside) == (0, 0)


def test_a_timestamp_in_the_same_second_as_the_cutoff_is_not_judged_stale(store):
    """⚠ THE REGRESSION GUARD for the string-compare bug, at its real boundary.

    ⚠ I FIRST DESCRIBED THIS BUG TOO BROADLY. The claim was "every grade is
    rejected"; that is wrong. A string comparison mostly works, because the
    date/time digits dominate and a 1-minute-old timestamp still sorts after a
    5-minute-ago cutoff. The divergence bites only when the second-level
    prefixes are IDENTICAL:

        production  2026-09-19T17:01:08.500000+00:00   (0.5s after the cutoff)
        cutoff      2026-09-19T17:01:08Z
        '.' (46) < 'Z' (90)  ->  judged STALE despite being newer

    So it is a ~1-in-300 boundary case for a 5-minute window, not a total
    failure — rare enough to survive casual testing and reappear as an
    intermittent "my feedback sometimes doesn't count". That is exactly the kind
    of bug a parsed comparison eliminates by construction.

    ⚠ TESTED AT THE SEAM, NOT THROUGH THE TOOL. Reproducing the collision
    end-to-end needs a timestamp in the same rendered SECOND as the cutoff —
    which is also the parsed boundary, where a correct implementation may
    legitimately reject too. The two failure modes overlap exactly at the edge,
    so an end-to-end test cannot separate them without being flaky.

    The property under test belongs to the dialect anyway: does the predicate
    compare PARSED instants or strings? That is decidable directly and
    deterministically.
    """
    from memory.backends.dialect import dialect_for

    for backend in ("sqlite", "postgres"):
        sql = dialect_for(backend).age_minutes_lt("last_accessed_at", "?")
        assert ">=" not in sql and "now_minus_minutes" not in sql, (
            f"{backend}: age_minutes_lt is string-comparing timestamps. m3 "
            "writes at least three formats to these columns and ord('.') < "
            "ord('Z'), so a sub-second timestamp in the cutoff's second sorts "
            "as older than it actually is."
        )

    # SQLite must parse via julianday; Postgres via interval arithmetic.
    assert "julianday" in dialect_for("sqlite").age_minutes_lt("c", "?")
    assert "INTERVAL" in dialect_for("postgres").age_minutes_lt("c", "?")

    # And the real production format must survive a round trip through it.
    import sqlite3

    conn = sqlite3.connect(":memory:")
    ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat()
    pred = dialect_for("sqlite").age_minutes_lt("?", "?")
    got = conn.execute(f"SELECT {pred}", (ts, 5)).fetchone()[0]
    assert got, (
        f"a production-format timestamp 1 minute old ({ts}) was judged outside "
        "a 5-minute window"
    )


def test_the_grade_query_uses_the_parsed_seam_not_a_string_compare():
    """A correct seam is worthless if the call site stops using it.

    Pins the call site as well as the primitive: planting the string comparison
    back into memory_grade_impl passes every behavioural test above (the bug is
    a ~1-in-300 boundary case), so only reading the source catches it. §2 —
    enforce at the convergence point, then prove the convergence point is what
    is actually called.
    """
    import inspect

    src = inspect.getsource(mm.memory_grade_impl)
    assert "age_minutes_lt" in src, (
        "memory_grade_impl stopped using the parsed-instant seam"
    )
    assert "now_minus_minutes" not in src, (
        "memory_grade_impl is string-comparing timestamps again — see "
        "dialect.age_minutes_lt for why that silently rejects fresh grades."
    )


def test_window_is_configurable_per_call(store):
    """A long tool-using turn must be able to widen the window."""
    mid = store(accessed_minutes_ago=30)
    assert mm.memory_grade_impl(
        grades=[{"memory_id": mid, "verdict": "helpful"}], window_minutes=5
    )["graded"] == 0
    assert mm.memory_grade_impl(
        grades=[{"memory_id": mid, "verdict": "helpful"}], window_minutes=60
    )["graded"] == 1


def test_never_retrieved_memories_cannot_be_graded(store):
    """The window IS the capability: no retrieval, no grade."""
    mid = str(uuid.uuid4())
    with mc._db() as db:
        db.execute(
            "INSERT INTO memory_items (id, content, title, type, importance, "
            "source, last_accessed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, datetime('now'))",
            (mid, "never read", "probe", "belief", 0.5, "agent"),
        )
    res = mm.memory_grade_impl(grades=[{"memory_id": mid, "verdict": "helpful"}])
    assert res["graded"] == 0
    assert _counts(mid) == (0, 0)


def test_unknown_ids_are_counted_not_silently_dropped(store):
    res = mm.memory_grade_impl(
        grades=[{"memory_id": str(uuid.uuid4()), "verdict": "helpful"}]
    )
    assert res.get("unknown") == 1


# ── instrumentation ──────────────────────────────────────────────────────────

def test_rejection_rate_is_measurable(store):
    """§5: a window nobody can evaluate is not a tunable knob."""
    mm.memory_grade_impl(
        grades=[{"memory_id": store(accessed_minutes_ago=0), "verdict": "helpful"}])
    mm.memory_grade_impl(
        grades=[{"memory_id": store(accessed_minutes_ago=90), "verdict": "helpful"}])

    stats = mm.memory_feedback_stats_impl()
    assert stats["accepted"] == 1
    assert stats["rejected_stale"] == 1
    assert stats["rejection_rate"] == pytest.approx(0.5)


def test_window_default_is_five_minutes_and_configurable():
    assert mm._FEEDBACK_WINDOW_MINUTES_DEFAULT == 5
    assert mm._feedback_window_minutes() >= 1


# ── the window ceiling ───────────────────────────────────────────────────────

def test_an_over_wide_window_cannot_reach_an_ancient_retrieval(store):
    """⚠ THE CEILING IS WHAT KEEPS RETRIEVAL THE CAPABILITY.

    window_minutes is a documented tuning knob, so it is not a bypass in the
    authorization sense -- but unbounded it dissolves the property the window
    exists for. A caller passing a year grades anything retrieved in that year,
    which is most of the store, and the grade stops being evidence that this
    caller used this memory.
    """
    mid = store(accessed_minutes_ago=60 * 24 * 30)          # 30 days ago
    res = mm.memory_grade_impl(
        grades=[{"memory_id": mid, "verdict": "helpful"}],
        window_minutes=525600,                              # one year
    )
    assert res["graded"] == 0, (
        "a year-wide window reached a 30-day-old retrieval: the clamp is not "
        "being applied"
    )
    assert _counts(mid) == (0, 0)


def test_the_clamp_is_reported_not_silent(store):
    """§3: applying a different window than asked, silently, is a false success."""
    mid = store(accessed_minutes_ago=0)
    res = mm.memory_grade_impl(
        grades=[{"memory_id": mid, "verdict": "helpful"}], window_minutes=525600
    )
    assert res["window_minutes"] == mm._FEEDBACK_WINDOW_MINUTES_MAX
    assert res["window_clamped_from"] == 525600
    assert "note" in res, "the clamp happened with no word to the caller"


def test_a_window_under_the_ceiling_is_untouched(store):
    """The clamp must not cost the legitimate widening case."""
    mid = store(accessed_minutes_ago=30)
    res = mm.memory_grade_impl(
        grades=[{"memory_id": mid, "verdict": "helpful"}], window_minutes=60
    )
    assert res["graded"] == 1
    assert res["window_minutes"] == 60
    assert "window_clamped_from" not in res


def test_a_config_file_cannot_exceed_the_ceiling_either(monkeypatch):
    """Every route resolves through one owner -- env included."""
    monkeypatch.setenv("M3_FEEDBACK_WINDOW_MINUTES", "525600")
    mm._fb_window_cache.update({"ts": 0.0, "mtime": None, "minutes": None})
    assert mm._feedback_window_minutes() == mm._FEEDBACK_WINDOW_MINUTES_MAX


def test_a_non_integer_window_returns_a_structured_error(store):
    """Consistency with the rest of the tool: an error dict, not a ValueError.

    Every other rejection path here returns {"ok": false, "error": ...}; raising
    from one of them makes the caller handle two shapes for one tool.
    """
    mid = store()
    res = mm.memory_grade_impl(
        grades=[{"memory_id": mid, "verdict": "helpful"}], window_minutes="abc"
    )
    assert res["ok"] is False
    assert res["error"] == "bad_window_minutes"
    assert _counts(mid) == (0, 0)
