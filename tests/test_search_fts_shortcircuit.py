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


def test_shortcircuit_absent_phrase_falls_through_when_embedder_down(
    tmp_path, monkeypatch
):
    """When NO row contains the exact query phrase, the short-circuit must not
    fire (no fabricated exact hit). With the embedder also down, the whole search
    then legitimately returns nothing — proving the short-circuit isn't inventing
    a match from a mere FTS token overlap."""
    import asyncio

    mc = _build(tmp_path, monkeypatch, embed_ok=False)
    # "widgets tenets" never appears as a contiguous phrase in any row, though
    # "tenets" and "widgets" each appear separately (FTS would token-match).
    ids = asyncio.run(_ids(mc, "widgets tenets", search_mode="hybrid"))
    # No exact-phrase => short-circuit yields nothing => embedder-down => []. If
    # any id came back it would mean the short-circuit fabricated an exact hit
    # from token overlap, which is the bug in the opposite direction.
    assert ids == []
