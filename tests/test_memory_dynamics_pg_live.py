"""Live-PG tests for the memory-dynamics release: decay floors, reinforcement,
graded feedback and the forensic read.

⚠ WHY THIS FILE EXISTS. Every other suite for this feature
(test_decay_floor_and_rate, test_importance_reinforcement,
test_memory_grade_window, test_memory_feedback_verdicts,
test_no_autonomous_deletion, test_forensic_decay_off) runs on SQLite only. That
is the §0.4 hermeticity trap verbatim: a green SQLite run proves nothing about
the backend half of the contract, and this change is ALL new SQL -- a CASE-based
floor, a rate-slowing UPDATE, a new dialect primitive (age_minutes_lt), and a
conditional column in the search projection.

The risk is not hypothetical here. The PG half of migration 049 shipped missing
three columns on chat_log_items, because on PG the chatlog is a CLONE table in
the same database while on SQLite it is a separate file carrying the same names.
Only a live cluster surfaced it (test_schema_parity_pg_live).

What is PG-specific and therefore worth a live assertion rather than a sqlite
one:
  * age_minutes_lt renders NOW() - INTERVAL on PG and julianday() arithmetic on
    SQLite -- two genuinely different implementations of one predicate.
  * The decay floor is a CASE expression combined with greatest(); PG is strict
    about numeric types where SQLite coerces.
  * importance_raw is appended to the SELECT conditionally, and search_pg.py
    hydrates its own column list independently of search.py.

Skips cleanly without a reachable cluster (requires_pg). See
~/.m3-private/runbooks/WSL_POSTGRES_TEST_DB_RUNBOOK.md to stand one up.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import sys
import uuid
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

pytestmark = pytest.mark.requires_pg


@pytest.fixture()
def pg(monkeypatch, pg_url):
    """A migrated, empty PG store. Mirrors test_memory_maintenance_pg_live.pg."""
    _DSN = pg_url
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PG_URL", _DSN)
    monkeypatch.setenv("M3_PRIMARY_PG_URL", _DSN)
    monkeypatch.setenv("M3_CORE_RS_DISABLE", "1")
    from memory.backends import selector as _selector

    _selector._reset_for_tests()
    from memory.backends.postgres_backend import PostgresBackend

    b = PostgresBackend(dsn=_DSN)
    with b.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        for (t,) in cur.fetchall():
            cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
    b._schema_ready = False
    b.ensure_schema()
    import migrate_pg

    with b.connection() as c:
        migrate_pg.run_pending_pg_migrations(c)

    import memory_maintenance as MM
    MM._FEEDBACK_WINDOW_REJECTIONS.update({"accepted": 0, "rejected": 0})
    yield b
    b.close()


def _write(uid, **kw):
    from memory.write import memory_write_impl
    raw = asyncio.run(memory_write_impl(embed=False, source="test", user_id=uid, **kw))
    return str(raw).replace("Created: ", "").strip().strip('"').split()[0]


def _sql(query, params=(), fetch=True):
    """⚠ fetch=False for writes: psycopg2 raises "no results to fetch" on an
    UPDATE, where sqlite3 returns an empty list. A helper that always fetched
    would make every write in this file look like a product failure."""
    from memory.db import _db
    with _db() as db:
        cur = db.execute(query, params)
        rows = cur.fetchall() if fetch else []
        if hasattr(db, "commit"):
            db.commit()
    return rows


def _p():
    from memory.backends import dialect
    return dialect().param()


def _set(mid, **cols):
    """Set columns directly, bypassing the write path (backdating, counters)."""
    p = _p()
    assigns = ", ".join(f"{k} = {p}" for k in cols)
    _sql(f"UPDATE memory_items SET {assigns} WHERE id = {p}",
         (*cols.values(), mid), fetch=False)


def _get(mid, col):
    p = _p()
    return _sql(f"SELECT {col} FROM memory_items WHERE id = {p}", (mid,))[0][0]


_OLD = "2020-01-01T00:00:00+00:00"   # far older than the 7-day decay window


# ── the migration reached BOTH tables ────────────────────────────────────────

def test_the_chatlog_clone_carries_the_dynamics_columns(pg):
    """⚠ THE DEFECT THIS FILE CAUGHT. On PG the chatlog is chat_log_items, a
    clone in the SAME database; on SQLite it is a separate file using the core
    names. So the SQLite migration reaches both stores and the PG one does not,
    unless it names the clone explicitly. pg_058 originally did not.

    test_schema_parity_pg_live guards the general class; this pins the specific
    columns so a future edit to pg_058 cannot quietly drop half of it.
    """
    rows = _sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='chat_log_items' "
        "AND column_name IN ('importance_raw','helpful_count','unhelpful_count')"
    )
    assert {r[0] for r in rows} == {
        "importance_raw", "helpful_count", "unhelpful_count"
    }, "pg_058 did not reach the chatlog clone"


# ── decay floors on PG ───────────────────────────────────────────────────────

def test_decay_stops_at_the_ordinary_floor_on_pg(pg):
    """greatest() + the CASE floor must render and bind on PG.

    Seeded BELOW the floor: if the floor expression were dropped or mis-rendered
    the row would keep multiplying downward, and if greatest() picked the wrong
    argument it would jump. Either way the assertion moves.
    """
    import memory_maintenance as MM
    uid = f"dyn-{uuid.uuid4().hex[:8]}"
    # Seeded just above the floor ON PURPOSE. Decay is geometric (x0.995), so
    # reaching 0.01 from 0.02 takes 139 passes -- running those against a live
    # cluster would test patience, not the floor. Starting one pass away means
    # the FLOOR is what stops the row rather than the pass count, which is the
    # property this asserts. The rate itself is covered separately.
    mid = _write(uid, type="note", content="floor", importance=0.0101)
    _set(mid, created_at=_OLD, last_accessed_at=None)

    for _ in range(10):
        MM.memory_maintenance_impl(decay=True, purge_expired=False, reinforce=False,
                                   prune_orphan_embeddings=False,
                                   prune_orphan_queues=False)

    imp = _get(mid, "importance")
    assert imp == pytest.approx(MM._ORDINARY_FLOOR, abs=1e-9), (
        f"decay left importance at {imp}, not the {MM._ORDINARY_FLOOR} floor: "
        f"either the floor is not rendering on PG, or greatest() picked the "
        f"wrong argument"
    )


def test_a_graded_memory_holds_the_higher_floor_on_pg(pg):
    """The floor is a CASE over helpful/unhelpful counts -- PG is strict about
    the types in a CASE where SQLite coerces, so render it for real."""
    import memory_maintenance as MM
    uid = f"dyn-{uuid.uuid4().hex[:8]}"
    # Each seeded just above ITS OWN floor, for the reason given in the
    # ordinary-floor test: the point is which floor binds, not how many passes
    # geometric decay needs to arrive.
    graded = _write(uid, type="note", content="graded", importance=0.404)
    plain = _write(uid, type="note", content="plain", importance=0.0101)
    _set(graded, created_at=_OLD, helpful_count=3, last_accessed_at=None)
    _set(plain, created_at=_OLD, last_accessed_at=None)

    for _ in range(10):
        MM.memory_maintenance_impl(decay=True, purge_expired=False, reinforce=False,
                                   prune_orphan_embeddings=False,
                                   prune_orphan_queues=False)

    assert _get(graded, "importance") == pytest.approx(MM._GRADED_FLOOR, abs=1e-9)
    assert _get(plain, "importance") == pytest.approx(MM._ORDINARY_FLOOR, abs=1e-9)
    assert _get(graded, "importance") > _get(plain, "importance")


def test_decay_rate_is_recorded_on_pg(pg):
    """The column existed since 001 with no writer. Assert PG gets one too."""
    import memory_maintenance as MM
    uid = f"dyn-{uuid.uuid4().hex[:8]}"
    mid = _write(uid, type="note", content="rate", importance=0.8)
    _set(mid, created_at=_OLD, last_accessed_at=None)
    MM.memory_maintenance_impl(decay=True, purge_expired=False, reinforce=False,
                               prune_orphan_embeddings=False,
                               prune_orphan_queues=False)
    assert _get(mid, "decay_rate") == pytest.approx(MM._DECAY_RATE)


# ── reinforcement on PG ──────────────────────────────────────────────────────

def test_reinforcement_discriminates_by_access_on_pg(pg):
    """Pre-registered metric 1, on the other backend: a heavily-read memory must
    retain more than an untouched one of the same age and starting importance.
    A zero gap means the feature is inert on PG."""
    import memory_maintenance as MM
    uid = f"dyn-{uuid.uuid4().hex[:8]}"
    # ⚠ SEEDED BELOW _REINFORCE_CEILING (0.39), which is where reinforcement
    # operates at all -- the UPDATE is gated `importance < ceiling`. That gate
    # IS the design (retrieval slows forgetting in the low band; it can never
    # push a memory up into the band an explicit helpful verdict earns), so
    # seeding at 0.8 measures nothing: both rows sit above the gate and decay
    # identically, which reads as "inert" while the feature is working.
    hot = _write(uid, type="note", content="hot", importance=0.3)
    cold = _write(uid, type="note", content="cold", importance=0.3)
    recent = dt.datetime.now(dt.timezone.utc).isoformat()
    _set(hot, created_at=_OLD, access_count=500, last_accessed_at=recent)
    _set(cold, created_at=_OLD, access_count=0, last_accessed_at=None)

    for _ in range(30):
        MM.memory_maintenance_impl(decay=True, purge_expired=False, reinforce=True,
                                   prune_orphan_embeddings=False,
                                   prune_orphan_queues=False)

    assert _get(hot, "importance") > _get(cold, "importance"), (
        "reinforcement is inert on PG: 500 reads decayed the same as zero"
    )


def test_retrieval_alone_cannot_outrun_decay_on_pg(pg):
    """⚠ THE RUNAWAY GUARD, on PG. Decision 4: being seen often must never become
    being useful. A memory read constantly, never graded, must still fall -- and
    must stay strictly under the floor an explicit helpful verdict earns."""
    import memory_maintenance as MM
    uid = f"dyn-{uuid.uuid4().hex[:8]}"
    # Started inside the band where reinforcement actually applies (below
    # _REINFORCE_CEILING), so the cap is under real pressure rather than never
    # being consulted. 10,000 reads is far past where access_reinforcement
    # saturates -- the curve is logarithmic and hard-capped at 0.05.
    start = 0.3
    mid = _write(uid, type="note", content="hammered", importance=start)
    recent = dt.datetime.now(dt.timezone.utc).isoformat()
    _set(mid, created_at=_OLD, access_count=10_000, last_accessed_at=recent)

    for _ in range(300):
        MM.memory_maintenance_impl(decay=True, purge_expired=False, reinforce=True,
                                   prune_orphan_embeddings=False,
                                   prune_orphan_queues=False)

    imp = _get(mid, "importance")
    assert imp < start, "retrieval alone outran decay on PG"
    assert imp < MM._GRADED_FLOOR, (
        f"an ungraded but heavily-read memory reached {imp}, at or above the "
        f"{MM._GRADED_FLOOR} floor that an explicit helpful verdict earns"
    )


# ── the grading window: age_minutes_lt on PG ─────────────────────────────────

def test_a_fresh_grade_lands_on_pg(pg):
    """⚠ THE DIALECT PRIMITIVE. age_minutes_lt renders NOW() - INTERVAL here and
    julianday() arithmetic on SQLite. It replaced a string comparison that
    rejected fresh grades whenever the timestamp formats differed -- exactly the
    kind of bug that is backend-shaped, so assert it on the live backend."""
    import memory_maintenance as MM
    uid = f"dyn-{uuid.uuid4().hex[:8]}"
    mid = _write(uid, type="note", content="fresh", importance=0.5)
    _set(mid, last_accessed_at=dt.datetime.now(dt.timezone.utc).isoformat())

    res = MM.memory_grade_impl(grades=[{"memory_id": mid, "verdict": "helpful"}])
    assert res["applied"] is True and res["graded"] == 1
    assert _get(mid, "helpful_count") == 1


def test_a_stale_grade_is_rejected_on_pg(pg):
    import memory_maintenance as MM
    uid = f"dyn-{uuid.uuid4().hex[:8]}"
    mid = _write(uid, type="note", content="stale", importance=0.5)
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).isoformat()
    _set(mid, last_accessed_at=old)

    res = MM.memory_grade_impl(grades=[{"memory_id": mid, "verdict": "helpful"}])
    assert res["graded"] == 0
    assert res["reason"] == "outside_feedback_window"
    assert _get(mid, "helpful_count") == 0, (
        "the response said it was rejected but the counter moved anyway"
    )


def test_the_window_ceiling_holds_on_pg(pg):
    """The clamp is Python-side, but the INTERVAL it produces is rendered by PG.
    Assert the whole path, not just the arithmetic."""
    import memory_maintenance as MM
    uid = f"dyn-{uuid.uuid4().hex[:8]}"
    mid = _write(uid, type="note", content="ancient", importance=0.5)
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()
    _set(mid, last_accessed_at=old)

    res = MM.memory_grade_impl(
        grades=[{"memory_id": mid, "verdict": "helpful"}], window_minutes=525600
    )
    assert res["graded"] == 0
    assert res["window_minutes"] == MM._FEEDBACK_WINDOW_MINUTES_MAX
    assert res["window_clamped_from"] == 525600
    assert _get(mid, "helpful_count") == 0


def test_counters_stay_separate_on_pg(pg):
    """⚠ NEVER A NET. Two INTEGER columns incremented by separate UPDATEs."""
    import memory_maintenance as MM
    uid = f"dyn-{uuid.uuid4().hex[:8]}"
    mid = _write(uid, type="note", content="contested", importance=0.5)
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    for verdict in ("helpful",) * 5 + ("unhelpful",) * 5:
        _set(mid, last_accessed_at=now)
        MM.memory_grade_impl(grades=[{"memory_id": mid, "verdict": verdict}])

    assert (_get(mid, "helpful_count"), _get(mid, "unhelpful_count")) == (5, 5), (
        "a +5/-5 memory must remain distinguishable from an ungraded one"
    )


# ── feedback verdicts on PG ──────────────────────────────────────────────────

def test_every_verdict_is_observable_on_pg(pg):
    """Pre-registered metric 2b. useful lifts, misleading drops, not_useful
    records without touching importance -- all three through PG's least()/
    greatest() clamps."""
    import memory_maintenance as MM
    uid = f"dyn-{uuid.uuid4().hex[:8]}"

    up = _write(uid, type="note", content="u", importance=0.5)
    MM.memory_feedback_impl(up, "useful")
    assert _get(up, "importance") > 0.5

    down = _write(uid, type="note", content="m", importance=0.5)
    MM.memory_feedback_impl(down, "misleading")
    assert _get(down, "importance") < 0.5

    miss = _write(uid, type="note", content="n", importance=0.5)
    MM.memory_feedback_impl(miss, "not_useful")
    assert _get(miss, "importance") == pytest.approx(0.5), (
        "a retrieval miss lowered importance: that punishes a correct memory "
        "for matching the wrong query"
    )


# ── no autonomous deletion, and restore ──────────────────────────────────────

def test_the_maintenance_pass_does_not_delete_on_pg(pg):
    """An autonomous pass deranks; removal is a person's decision."""
    import memory_maintenance as MM
    uid = f"dyn-{uuid.uuid4().hex[:8]}"
    mid = _write(uid, type="note", content="doomed", importance=0.01)
    _set(mid, created_at=_OLD, last_accessed_at=None)

    for _ in range(20):
        MM.memory_maintenance_impl(decay=True, purge_expired=False, reinforce=True,
                                   prune_orphan_embeddings=False,
                                   prune_orphan_queues=False)

    assert _get(mid, "is_deleted") == 0, (
        "the maintenance pass soft-deleted a low-importance memory on PG"
    )


def test_restore_refuses_a_deliberate_deletion_on_pg(pg):
    """The allowlist is checked before SQL, but assert it end-to-end on PG: the
    archive row and its reason are what make the distinction safe."""
    import memory_maintenance as MM
    res = MM.memory_restore_impl(reason="user_delete", dry_run=True)
    assert res.get("ok") is False
    assert res.get("error") == "reason_not_autonomous"


# ── the forensic read ────────────────────────────────────────────────────────

def _search(query, uid, apply_decay):
    """Return {id: row_dict} for a search.

    ⚠ THE SCORE IS NOT USABLE AS THE ASSERTION HERE. The retrieval path in front
    of the ranker has an FTS exact-phrase short-circuit that returns score 1.0
    without consulting the scorer (the same trap test_forensic_decay_off
    documents on SQLite, which is why THAT file tests _ranking_importance
    directly). Measured on PG: an exact-phrase hit comes back (1.0, {...}).
    So assert on the PROJECTED ROW -- which is the PG-specific half anyway,
    since search_pg.py hydrates its own column list.
    """
    from memory.search import memory_search_scored_impl
    res = asyncio.run(memory_search_scored_impl(
        query=query, k=5, user_id=uid, apply_decay=apply_decay))
    return {row["id"]: row for _score, row in res}


def test_importance_raw_is_projected_only_for_a_forensic_read_on_pg(pg):
    """⚠ search_pg.py HYDRATES ITS OWN COLUMN LIST, independently of search.py,
    so the conditional projection has TWO implementations and only a live
    cluster exercises the second one.

    Both halves matter: present when apply_decay=False (or the forensic read
    silently falls back to the decayed value), and absent by default (§4 --
    an ordinary search must not pay for a column it never reads).
    """
    uid = f"dyn-{uuid.uuid4().hex[:8]}"
    mid = _write(uid, type="note", content="forensic marker alpha", importance=0.9)
    _set(mid, importance=0.05, importance_raw=0.9)

    forensic = _search("forensic marker alpha", uid, apply_decay=False)
    assert mid in forensic, "the seeded memory was not returned on the forensic path"
    assert forensic[mid].get("importance_raw") == pytest.approx(0.9), (
        "importance_raw did not reach the PG projection: a forensic read would "
        "silently rank on the decayed value"
    )

    ordinary = _search("forensic marker alpha", uid, apply_decay=True)
    assert mid in ordinary
    assert "importance_raw" not in ordinary[mid], (
        "the default search projected importance_raw: an ordinary read pays for "
        "a column only the forensic path uses"
    )


def test_the_ranking_selection_prefers_raw_on_a_pg_row(pg):
    """_ranking_importance is the single owner of the choice (extracted so tests
    stop validating their own copy of it). Feed it a REAL PG row rather than a
    dict, because psycopg2 rows raise different exception types on a missing
    key than sqlite3.Row does -- and the NULL fallback is written as an
    except clause."""
    from memory.search import _ranking_importance
    uid = f"dyn-{uuid.uuid4().hex[:8]}"
    mid = _write(uid, type="note", content="selection marker gamma", importance=0.05)
    _set(mid, importance_raw=0.9)

    row = _search("selection marker gamma", uid, apply_decay=False)[mid]
    assert _ranking_importance(row, apply_decay=False) == pytest.approx(0.9)
    assert _ranking_importance(row, apply_decay=True) == pytest.approx(0.05)


def test_a_null_importance_raw_falls_back_on_pg(pg):
    """Pre-049 rows have importance_raw NULL. Scoring those as zero would sink
    every older memory in a forensic query -- and NULL handling is exactly where
    the two drivers differ (psycopg2 gives None; the fallback must catch it
    rather than rely on a KeyError that never comes)."""
    from memory.search import _ranking_importance
    uid = f"dyn-{uuid.uuid4().hex[:8]}"
    mid = _write(uid, type="note", content="legacy marker beta", importance=0.7)
    _set(mid, importance_raw=None)

    row = _search("legacy marker beta", uid, apply_decay=False)[mid]
    assert row.get("importance_raw") is None, "the fixture did not seed a NULL"
    assert _ranking_importance(row, apply_decay=False) == pytest.approx(0.7), (
        "a NULL importance_raw scored as zero instead of falling back to "
        "importance -- every pre-049 row would sink in a forensic query"
    )
