"""Tests for Lever 3b — cross-agent isolation (opt-in SQL enforcement).

`memory_search_scored_impl` accepts a `requesting_agent` kwarg. When set, the
generated SQL adds `(mi.scope != 'agent' OR mi.agent_id = ?)` to the WHERE
clause: private (`scope='agent'`) rows are restricted to the requesting
agent's own rows, while every shared scope (`org`, `user`, `session`) stays
visible regardless of which agent wrote it. When `requesting_agent` is not
passed at all, behavior is unchanged (back-compat) — every row is visible
regardless of scope/agent, exactly as before this feature existed.

Follows the same pattern as test_confidence_ranking.py: drives
`memory_search_scored_impl` against a real full-schema DB with seeded rows +
embeddings (identical query/document vectors so ranking noise can't hide a
leaked row), with the query embedder stubbed for determinism. This exercises
the actual SQL-layer enforcement rather than reimplementing the WHERE clause
in the test.
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


# A single fixed unit vector reused for the query and every seeded row, so
# every row is equally "relevant" and the isolation WHERE clause is the only
# thing that can exclude a row from the result set.
def _vec(primary: float, dim: int):
    v = [0.0] * dim
    v[0] = primary
    v[1] = (1.0 - primary * primary) ** 0.5
    return v


def _seed(conn, mid, content, vec, *, scope, agent_id, importance=0.5):
    from memory.config import EMBED_MODEL
    conn.execute(
        "INSERT INTO memory_items (id, type, title, content, source, change_agent, "
        "created_at, importance, confidence, is_deleted, scope, agent_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,?,?)",
        (mid, "fact", content[:20], content, "agent", "claude",
         "2026-01-01T00:00:00Z", importance, 0.9, scope, agent_id),
    )
    conn.execute(
        "INSERT INTO memory_embeddings (memory_id, embedding, embed_model, dim) VALUES (?,?,?,?)",
        (mid, _pack(vec), EMBED_MODEL, len(vec)),
    )


def _patch(monkeypatch, db_path, qvec):
    import memory_core

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
        return (qvec, "test-embed")

    monkeypatch.setattr(memory_core, "_db", fake_db)
    monkeypatch.setattr(memory_core, "_embed", fake_embed)
    return memory_core


async def _ranked_ids(mc, **kw):
    res = await mc.memory_search_scored_impl(
        query="anything", k=20, mmr=False, search_mode="vector", **kw
    )
    out = []
    for r in res:
        if isinstance(r, tuple):
            out.append(r[1]["id"])
        elif isinstance(r, dict):
            out.append(r["id"])
    return out


def _seed_two_agents(conn, qv):
    """Seed rows for two agents ('planner', 'implementer') across scopes."""
    _seed(conn, "planner-private", "planner scratch note", qv,
          scope="agent", agent_id="planner")
    _seed(conn, "implementer-private", "implementer scratch note", qv,
          scope="agent", agent_id="implementer")
    _seed(conn, "shared-org", "shared org requirement", qv,
          scope="org", agent_id="planner")
    _seed(conn, "shared-user", "shared user preference", qv,
          scope="user", agent_id="implementer")


@pytest.mark.asyncio
async def test_requesting_agent_excludes_other_agents_private_rows(monkeypatch, tmp_path):
    """requesting_agent='implementer' must not see planner's private scope='agent'
    row, but must see its own private row plus every shared-scope row."""
    from memory.config import EMBED_DIM
    db = tmp_path / "t.db"
    _full_db(db)
    qv = _vec(1.0, EMBED_DIM)
    with sqlite3.connect(str(db)) as conn:
        _seed_two_agents(conn, qv)
        conn.commit()

    mc = _patch(monkeypatch, db, qv)

    ids = set(await _ranked_ids(mc, requesting_agent="implementer"))

    assert "planner-private" not in ids, "leak: saw another agent's private note"
    assert "implementer-private" in ids, "own private row must remain visible"
    assert "shared-org" in ids, "shared org-scoped row must remain visible"
    assert "shared-user" in ids, "shared user-scoped row must remain visible"


@pytest.mark.asyncio
async def test_no_requesting_agent_sees_everything(monkeypatch, tmp_path):
    """Back-compat: omitting requesting_agent entirely must not change existing
    behavior — every row is visible regardless of scope/agent."""
    from memory.config import EMBED_DIM
    db = tmp_path / "t.db"
    _full_db(db)
    qv = _vec(1.0, EMBED_DIM)
    with sqlite3.connect(str(db)) as conn:
        _seed_two_agents(conn, qv)
        conn.commit()

    mc = _patch(monkeypatch, db, qv)

    ids = set(await _ranked_ids(mc))

    assert ids == {"planner-private", "implementer-private", "shared-org", "shared-user"}, (
        f"default (no requesting_agent) must be byte-identical to pre-isolation "
        f"behavior — full visibility; got {ids}"
    )


@pytest.mark.asyncio
async def test_explicit_scope_narrows_and_is_not_widened_by_requesting_agent(monkeypatch, tmp_path):
    """An explicit scope='org' filter combined with requesting_agent set must
    still only return org-scoped rows — enforcement narrows, it never widens
    an explicit scope filter to let other scopes back in."""
    from memory.config import EMBED_DIM
    db = tmp_path / "t.db"
    _full_db(db)
    qv = _vec(1.0, EMBED_DIM)
    with sqlite3.connect(str(db)) as conn:
        _seed_two_agents(conn, qv)
        conn.commit()

    mc = _patch(monkeypatch, db, qv)

    ids = set(await _ranked_ids(mc, requesting_agent="implementer", scope="org"))

    assert ids == {"shared-org"}, (
        f"explicit scope='org' + requesting_agent must return only org rows; got {ids}"
    )


@pytest.mark.asyncio
async def test_requesting_agent_symmetric_for_other_agent(monkeypatch, tmp_path):
    """Symmetric check from the other agent's perspective: requesting_agent='planner'
    excludes the implementer's private row but keeps planner's own + shared rows."""
    from memory.config import EMBED_DIM
    db = tmp_path / "t.db"
    _full_db(db)
    qv = _vec(1.0, EMBED_DIM)
    with sqlite3.connect(str(db)) as conn:
        _seed_two_agents(conn, qv)
        conn.commit()

    mc = _patch(monkeypatch, db, qv)

    ids = set(await _ranked_ids(mc, requesting_agent="planner"))

    assert "implementer-private" not in ids, "leak: saw another agent's private note"
    assert ids == {"planner-private", "shared-org", "shared-user"}
