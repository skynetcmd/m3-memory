"""Regression: the FTS short-circuit must not collapse many matches to one.

Bug (fixed 2026-07-03): `memory_search_scored_impl` had a pre-embedding FTS
"short-circuit" that fetched a single row (`LIMIT 1`) and returned it alone if
its content/title contained the exact query substring. When FTS matched many
rows (the common case for a specific lexical query), this:

  * returned only the single best-bm25 row and silently DROPPED the rest — a
    pinned, embedded, on-topic memory would not surface because a *different*
    row won the LIMIT 1; and
  * when that one row did NOT contain the exact substring, fell through to the
    embedding path, which returns [] if the embed backend is down — so a purely
    lexical, FTS-satisfiable query yielded nothing during an embedder outage.

The fix fetches the top-N bm25 matches and returns EVERY row whose content or
title contains the exact query phrase. These tests pin both properties, and the
embedder-down case (§1 offline-capable, §3 fail-safe).

Pattern mirrors test_agent_isolation.py: drive the real impl against a
full-schema temp DB with the query embedder stubbed for determinism.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from embedding_utils import pack as _pack  # noqa: E402


@pytest.fixture(autouse=True)
def _skip_migrations(monkeypatch):
    monkeypatch.setenv("M3_SKIP_MIGRATIONS", "1")


def _full_db(db_path):
    from conftest import create_full_main_schema

    create_full_main_schema(db_path)


def _seed(conn, mid, title, content):
    """Insert an item + a dim-correct embedding so the healthy path can join."""
    from memory.config import EMBED_DIM, EMBED_MODEL

    conn.execute(
        "INSERT INTO memory_items (id, type, title, content, source, change_agent, "
        "created_at, importance, confidence, is_deleted, scope, agent_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
        (mid, "reference", title, content, "agent", "claude",
         "2026-01-01T00:00:00Z", 0.5, 0.9, "user", None),
    )
    vec = [0.0] * EMBED_DIM
    vec[0] = 1.0
    conn.execute(
        "INSERT INTO memory_embeddings (memory_id, embedding, embed_model, dim) "
        "VALUES (?,?,?,?)",
        (mid, _pack(vec), EMBED_MODEL, EMBED_DIM),
    )


def _patch(monkeypatch, db_path, *, embed_ok=True):
    """Bind a temp DB and a deterministic (or failing) query embedder."""
    import memory_core
    from memory.config import EMBED_DIM

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

    async def fake_embed(_q):
        if not embed_ok:
            return (None, "embed-down")          # simulate backend outage
        return ([1.0] + [0.0] * (EMBED_DIM - 1), "test-embed")

    monkeypatch.setattr(memory_core, "_db", fake_db)
    monkeypatch.setattr(memory_core, "_embed", fake_embed)
    # memory.search holds its OWN module-level `_db`. The healthy/short-circuit
    # paths re-resolve it from memory_core via _mc_callable, but the
    # embedder-down fallback (_fts_only_results) uses the module global
    # directly — leaving it unpatched pointed that path at the REAL store and
    # produced "Backfill failed: no such table: memory_items", which is where
    # the 4th earlier attempt at this test died.
    from memory import search as _search_mod

    monkeypatch.setattr(_search_mod, "_db", fake_db)
    return memory_core


async def _ids(mc, query, **kw):
    res = await mc.memory_search_scored_impl(query=query, k=20, mmr=False, **kw)
    return [r[1]["id"] for r in (res or [])]


def _build(tmp_path, monkeypatch, *, embed_ok=True):
    db = tmp_path / "sc.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    # Three items all containing the exact phrase "core tenets" in content, plus
    # one that has it only in the title, plus a decoy that matches the FTS term
    # "tenets" but NOT the exact phrase.
    _seed(conn, "aaaaaaaa-0000-0000-0000-000000000001",
          "Alpha doc", "the seven core tenets are listed here")
    _seed(conn, "bbbbbbbb-0000-0000-0000-000000000002",
          "Beta doc", "we evaluate against the core tenets every change")
    _seed(conn, "cccccccc-0000-0000-0000-000000000003",
          "Gamma doc", "core tenets drive the design review")
    _seed(conn, "dddddddd-0000-0000-0000-000000000004",
          "core tenets in the title only", "unrelated body text about widgets")
    _seed(conn, "eeeeeeee-0000-0000-0000-000000000005",
          "Decoy", "this mentions tenets but not the exact two-word phrase")
    conn.commit()
    conn.close()
    return _patch(monkeypatch, db, embed_ok=embed_ok)


def test_shortcircuit_returns_all_exact_matches(tmp_path, monkeypatch):
    """The short-circuit must return EVERY exact-substring match, not LIMIT 1."""
    import asyncio

    mc = _build(tmp_path, monkeypatch, embed_ok=True)
    ids = asyncio.run(_ids(mc, "core tenets", search_mode="hybrid"))

    # All four rows containing the exact phrase (three in content, one in title)
    # must be present; the decoy (FTS-matches "tenets" only) must not.
    assert "aaaaaaaa-0000-0000-0000-000000000001" in ids
    assert "bbbbbbbb-0000-0000-0000-000000000002" in ids
    assert "cccccccc-0000-0000-0000-000000000003" in ids
    assert "dddddddd-0000-0000-0000-000000000004" in ids
    assert "eeeeeeee-0000-0000-0000-000000000005" not in ids
    # Regression guard: the old code returned exactly 1.
    assert len([i for i in ids if i.startswith(("aaaa", "bbbb", "cccc", "dddd"))]) >= 4


def test_shortcircuit_works_when_embedder_down(tmp_path, monkeypatch):
    """A purely lexical, FTS-satisfiable query must still return matches when the
    embed backend is unreachable (§1 offline-capable, §3 fail-safe)."""
    import asyncio

    mc = _build(tmp_path, monkeypatch, embed_ok=False)
    ids = asyncio.run(_ids(mc, "core tenets", search_mode="hybrid"))

    # With the embedder down the ONLY path that can return anything is the
    # short-circuit. It must still surface the exact-phrase matches.
    assert "aaaaaaaa-0000-0000-0000-000000000001" in ids
    assert len(ids) >= 4


def test_shortcircuit_absent_phrase_does_not_fabricate_an_exact_hit(
    tmp_path, monkeypatch
):
    """No row contains the exact phrase => the short-circuit must not fire.

    UPDATED 2026-07-26. This asserted `ids == []` — "embedder down and no exact
    phrase means the whole search returns nothing". That assertion was written
    before the embedder-down FTS fallback existed (this test came with 21ae00cf;
    the fallback with a28acf04), and it was passing for the WRONG REASON: the
    harness left `memory.search._db` pointing at the real store, so the fallback
    queried a database with none of these fixture rows and returned empty. Once
    the harness patched that binding too, the fixture became visible and the
    fallback correctly returned its keyword matches.

    Returning [] here would now be the BUG (a lexical, FTS-satisfiable query
    silently yielding nothing during an embedder outage — §1 offline-capable).
    What this test still pins is the original property, stated directly rather
    than inferred from emptiness: the short-circuit does not FABRICATE an exact
    hit from mere token overlap. Its signature is score 1.0 on every row; the
    fallback's rows are keyword-ranked instead.
    """
    import asyncio

    mc = _build(tmp_path, monkeypatch, embed_ok=False)
    # "widgets tenets" never appears as a contiguous phrase in any row, though
    # "tenets" and "widgets" each appear separately (FTS would token-match).
    from memory import search as S

    ran = {"n": 0}
    original = S._fts_only_results

    def spy(*a, **k):
        ran["n"] += 1
        return original(*a, **k)

    S._fts_only_results = spy
    try:
        res = asyncio.run(
            mc.memory_search_scored_impl(
                query="widgets tenets", k=20, mmr=False, search_mode="hybrid"
            )
        )
    finally:
        S._fts_only_results = original

    assert ran["n"] > 0, (
        "the FALLBACK should have served this query — the short-circuit must "
        "decline when no row contains the exact phrase"
    )
    assert res, (
        "a lexical query that FTS can satisfy must not return [] just because "
        "the embedder is down"
    )


# ---------------------------------------------------------------------------
# Regression: the short-circuit must honour the NARROWING filters, not just
# tenancy.
#
# Bug (fixed 2026-07-26): the short-circuit built its WHERE from user_id/scope
# only. type_filter and agent_filter were implemented in exactly ONE of the
# three candidate-fetch paths (the SQLite main branch), so any query that took
# the short-circuit — i.e. any query whose text appears verbatim in some row,
# which a one-word query almost always does — returned UNFILTERED rows.
#
# It surfaced as `memory_search(query="runbook", type_filter="procedure")`
# coming back with chat_log and belief rows, every one at score 1.0. Flat 1.0
# reads as "perfect matches", not as "your filter was dropped", which is why it
# went unnoticed. The tenancy fix of 2026-07-14 had patched user_id/scope into
# all three copies and missed these two.
#
# The fix routes every copy through Dialect.scope_predicates (the seam), so the
# three paths cannot drift again. These tests pin the behaviour, not the
# implementation: they assert the RESULT is narrowed.
# ---------------------------------------------------------------------------

def _build_typed(tmp_path, monkeypatch, *, embed_ok=True):
    """Rows that all contain the exact phrase, spread across several types.

    ``embed_ok=False`` simulates an embedder outage, which routes the query to
    the `_fts_only_results` degrade path instead of the vector path.

    Every row matches the query verbatim, so the short-circuit fires for all of
    them — which is precisely the condition under which the filter was dropped.
    """
    from memory.config import EMBED_DIM, EMBED_MODEL

    db = tmp_path / "typed.db"
    _full_db(db)
    conn = sqlite3.connect(str(db))
    rows = [
        ("11111111-0000-0000-0000-000000000001", "procedure", "agent-a", "P one"),
        ("22222222-0000-0000-0000-000000000002", "procedure", "agent-a", "P two"),
        ("33333333-0000-0000-0000-000000000003", "belief", "agent-a", "B one"),
        ("44444444-0000-0000-0000-000000000004", "chat_log", "agent-b", "C one"),
        ("55555555-0000-0000-0000-000000000005", "reference", "agent-b", "R one"),
    ]
    vec = [0.0] * EMBED_DIM
    vec[0] = 1.0
    for mid, typ, agent, title in rows:
        conn.execute(
            "INSERT INTO memory_items (id, type, title, content, source, "
            "change_agent, created_at, importance, confidence, is_deleted, "
            "scope, agent_id) VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
            (mid, typ, title, "the widget runbook lives here", "agent",
             "claude", "2026-01-01T00:00:00Z", 0.5, 0.9, "user", agent),
        )
        conn.execute(
            "INSERT INTO memory_embeddings (memory_id, embedding, embed_model, dim) "
            "VALUES (?,?,?,?)",
            (mid, _pack(vec), EMBED_MODEL, EMBED_DIM),
        )
    conn.commit()
    conn.close()
    return _patch(monkeypatch, db, embed_ok=embed_ok)


async def _typed(mc, query, **kw):
    res = await mc.memory_search_scored_impl(query=query, k=20, mmr=False, **kw)
    return [(r[1]["id"], r[1].get("type"), r[1].get("agent_id")) for r in (res or [])]


@pytest.mark.asyncio
async def test_short_circuit_honours_type_filter(tmp_path, monkeypatch):
    """The load-bearing assertion: a filtered search returns ONLY that type."""
    mc = _build_typed(tmp_path, monkeypatch)
    hits = await _typed(mc, "widget runbook", type_filter="procedure")

    assert hits, "short-circuit returned nothing; the fixture should match"
    wrong = [(i, t) for i, t, _ in hits if t != "procedure"]
    assert not wrong, (
        f"type_filter was dropped — got non-procedure rows: {wrong}. "
        "The short-circuit returned before the filtered SQL ran."
    )
    assert len(hits) == 2, f"expected both procedure rows, got {len(hits)}"


@pytest.mark.asyncio
async def test_short_circuit_honours_agent_filter(tmp_path, monkeypatch):
    """agent_filter shares the same code path — and the same original defect."""
    mc = _build_typed(tmp_path, monkeypatch)
    hits = await _typed(mc, "widget runbook", agent_filter="agent-b")

    # Assert on IDs, not on the returned agent_id: the short-circuit's SELECT
    # projection does not include mi.agent_id, so it reads back as None even
    # when the WHERE correctly filtered on it. Asserting on the field the API
    # does not return would fail against a WORKING fix (it did, first run).
    agent_b_ids = {
        "44444444-0000-0000-0000-000000000004",
        "55555555-0000-0000-0000-000000000005",
    }
    got = {i for i, _, _ in hits}
    assert got == agent_b_ids, (
        f"agent_filter was dropped or over-narrowed — expected exactly the "
        f"agent-b rows, got {got}"
    )


@pytest.mark.asyncio
async def test_short_circuit_unfiltered_still_returns_every_type(tmp_path, monkeypatch):
    """Guard the other direction: the fix must not over-narrow."""
    mc = _build_typed(tmp_path, monkeypatch)
    hits = await _typed(mc, "widget runbook")

    assert len(hits) == 5, f"expected all 5 rows unfiltered, got {len(hits)}"
    assert {t for _, t, _ in hits} == {"procedure", "belief", "chat_log", "reference"}


@pytest.mark.asyncio
async def test_short_circuit_type_and_agent_filters_compose(tmp_path, monkeypatch):
    """Both predicates AND together, not last-one-wins."""
    mc = _build_typed(tmp_path, monkeypatch)
    hits = await _typed(mc, "widget runbook",
                        type_filter="procedure", agent_filter="agent-b")
    assert hits == [], (
        "no row is both procedure AND agent-b; a non-empty result means the "
        f"predicates did not compose: {hits}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# The EMBEDDER-DOWN FALLBACK (_fts_only_results) — a different path from the
# short-circuit above, and the one that shipped unguarded.
#
# Bug (fixed 2026-07-26, a28acf04): `_fts_only_results` did not ACCEPT
# type_filter/agent_filter at all, so a filtered search returned UNFILTERED rows
# whenever the query embedder was unavailable.
#
# FOUR earlier attempts at this test each passed for the WRONG reason. The trap:
# the exact-substring SHORT-CIRCUIT fires FIRST and returns before the embedder
# is ever consulted, so a test that looks like it exercises the fallback is
# often exercising the short-circuit — and passes against broken code.
#
# `_fallback_hits` closes that hole by SPYING on _fts_only_results and asserting
# it actually ran. The query matters too: "runbook widget" is REVERSED relative
# to the fixture content ("the widget runbook lives here"), so FTS matches it
# but it is not an exact substring — the short-circuit declines and the fallback
# gets it. "widget runbook" (in-order) is served by the short-circuit; verified
# both ways before writing these.
# ──────────────────────────────────────────────────────────────────────────────

async def _fallback_hits(mc, query, **kw):
    """Drive a search and PROVE the embedder-down fallback served it.

    Returns (hits, fallback_ran). Callers must assert fallback_ran — otherwise
    "empty because filtered" and "empty because the path never ran" are
    indistinguishable, which is how earlier attempts passed against broken code.
    """
    from memory import search as S

    ran = {"n": 0}
    original = S._fts_only_results

    def spy(*a, **k):
        ran["n"] += 1
        return original(*a, **k)

    S._fts_only_results = spy
    try:
        res = await mc.memory_search_scored_impl(query=query, k=20, mmr=False, **kw)
    finally:
        S._fts_only_results = original
    hits = [(r[1]["id"], r[1].get("type"), r[1].get("agent_id")) for r in (res or [])]
    return hits, ran["n"] > 0


@pytest.mark.asyncio
async def test_fallback_path_is_actually_reached(tmp_path, monkeypatch):
    """Control. Everything below is meaningless if this does not hold."""
    mc = _build_typed(tmp_path, monkeypatch, embed_ok=False)
    hits, ran = await _fallback_hits(mc, "runbook widget")

    assert ran, (
        "the embedder-down fallback did NOT run — the short-circuit or the "
        "embedding path served this query, so any assertion below would be "
        "testing the wrong code path (this is how 4 earlier attempts failed)"
    )
    assert len(hits) == 5, f"unfiltered fallback should return all 5 rows, got {len(hits)}"


@pytest.mark.asyncio
async def test_fallback_honours_type_filter(tmp_path, monkeypatch):
    """The load-bearing assertion: filtered search stays filtered when the
    embedder is down."""
    mc = _build_typed(tmp_path, monkeypatch, embed_ok=False)
    hits, ran = await _fallback_hits(mc, "runbook widget", type_filter="procedure")

    assert ran, "fallback did not run; assertion below would be vacuous"
    assert hits, "fixture has 2 procedure rows — an empty result is a false pass"
    wrong = [(i, t) for i, t, _ in hits if t != "procedure"]
    assert not wrong, (
        f"type_filter was DROPPED on the embedder-down path — got {wrong}. "
        "This is the a28acf04 regression."
    )
    assert len(hits) == 2, f"expected both procedure rows, got {len(hits)}"


@pytest.mark.asyncio
async def test_fallback_honours_agent_filter(tmp_path, monkeypatch):
    """Asserted by ID, not by reading agent_id off the row.

    The fallback projects only the 5 base columns (id, content, title, type,
    importance) — `agent_id` is an opt-in `extra_columns` field and is absent
    here, so `row["agent_id"]` is None for EVERY row and an assertion on it
    passes vacuously whether or not the filter works. The fixture's agent-b
    rows are ids ...004 and ...005; membership is the real check.
    """
    mc = _build_typed(tmp_path, monkeypatch, embed_ok=False)
    hits, ran = await _fallback_hits(mc, "runbook widget", agent_filter="agent-b")

    assert ran, "fallback did not run; assertion below would be vacuous"
    assert hits, "fixture has agent-b rows — an empty result is a false pass"
    agent_b_ids = {
        "44444444-0000-0000-0000-000000000004",
        "55555555-0000-0000-0000-000000000005",
    }
    got = {i for i, _, _ in hits}
    assert got == agent_b_ids, (
        f"agent_filter was dropped or over-narrowed on the embedder-down path — "
        f"expected exactly the agent-b rows, got {got}"
    )


@pytest.mark.asyncio
async def test_fallback_filters_compose(tmp_path, monkeypatch):
    """Both predicates AND together on the degrade path too."""
    mc = _build_typed(tmp_path, monkeypatch, embed_ok=False)
    hits, ran = await _fallback_hits(
        mc, "runbook widget", type_filter="procedure", agent_filter="agent-b"
    )
    assert ran, "fallback did not run; assertion below would be vacuous"
    assert hits == [], (
        "no fixture row is both procedure AND agent-b; a non-empty result means "
        f"the predicates did not compose on the degrade path: {hits}"
    )
