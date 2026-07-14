"""Cross-tenant isolation regression tests for memory_search_scored / _routed.

Guards the 2026-07-14 security fix: the FTS short-circuit branch in
bin/memory/search.py and the graph/session/entity-graph expansion in
bin/memory/graph.py used to return OTHER users' rows because they skipped the
user_id/scope filter that the main search branch applies. These tests write two
users' data (with a deliberate cross-tenant KG edge) and assert user A can NEVER
surface user B's content through ANY search path or expansion.

Run against a real tmp DB (repo conftest autouse fixture isolates paths).
"""

from __future__ import annotations

import re

import pytest

# bin/ is on sys.path via the repo conftest.
from memory_core import (
    memory_link_impl,
    memory_search_routed_impl,
    memory_search_scored_impl,
    memory_write_impl,
)

pytestmark = pytest.mark.asyncio

_BOB_SECRET = "bob confidential merger target Acme Corp 4429"
_ALICE_NOTE = "alice public project planning roadmap notes"


async def _seed(link: bool = False):
    """Write bob's secret + alice's note; optionally link them cross-tenant."""
    rb = await memory_write_impl(type="fact", content=_BOB_SECRET,
                                 user_id="bob", scope="user", auto_classify=False)
    ra = await memory_write_impl(type="fact", content=_ALICE_NOTE,
                                 user_id="alice", scope="user", auto_classify=False)
    bid = re.search(r"[0-9a-f-]{36}", rb).group(0)
    aid = re.search(r"[0-9a-f-]{36}", ra).group(0)
    if link:
        memory_link_impl(from_id=aid, to_id=bid, relationship_type="related")
    return aid, bid


def _leaks_bob(result) -> bool:
    s = str(result).lower()
    return "merger" in s or "acme" in s or "4429" in s


async def test_scored_fts_short_circuit_no_leak():
    """A specific query (triggers the FTS short-circuit) must not cross tenants."""
    await _seed()
    for mode in ("hybrid", "fts5"):
        rows = await memory_search_scored_impl(
            query="merger target Acme Corp", user_id="alice", scope="user",
            k=5, search_mode=mode, extra_columns=["user_id"],
        )
        assert not any(it.get("user_id") == "bob" for _s, it in rows), \
            f"FTS short-circuit leaked bob's row to alice in mode={mode}"


async def test_scored_cross_scope_no_leak():
    """user-scope query must not surface org-scope rows of another user."""
    await memory_write_impl(type="fact", content="org roadmap Q3 launch secret",
                            user_id="carol", scope="org", auto_classify=False)
    await memory_write_impl(type="fact", content="alice tea preference",
                            user_id="alice", scope="user", auto_classify=False)
    rows = await memory_search_scored_impl(
        query="roadmap Q3 launch secret", user_id="alice", scope="user",
        k=5, extra_columns=["user_id", "scope"],
    )
    assert not any(it.get("scope") == "org" for _s, it in rows)
    assert not any(it.get("user_id") == "carol" for _s, it in rows)


async def test_bob_still_finds_own():
    """The fix must not over-block: bob still retrieves his own row."""
    await _seed()
    rows = await memory_search_scored_impl(
        query="merger target Acme Corp", user_id="bob", scope="user",
        k=5, extra_columns=["user_id"],
    )
    assert any(it.get("user_id") == "bob" for _s, it in rows)


@pytest.mark.parametrize(
    "graph_depth,expand_sessions,entity_graph",
    [(2, False, False), (0, True, False), (0, False, True), (2, True, True)],
)
async def test_routed_expansion_no_leak(graph_depth, expand_sessions, entity_graph):
    """Graph / session / entity-graph expansion follows edges that may CROSS
    tenants — with a real cross-tenant KG edge present, alice's routed search
    must still never surface bob's content through the expansion."""
    await _seed(link=True)
    result = await memory_search_routed_impl(
        query="project planning roadmap", user_id="alice", scope="user", k=5,
        graph_depth=graph_depth, expand_sessions=expand_sessions,
        entity_graph=entity_graph,
    )
    assert not _leaks_bob(result), (
        f"routed expansion leaked bob's content "
        f"(graph={graph_depth}, sessions={expand_sessions}, entity={entity_graph})"
    )
