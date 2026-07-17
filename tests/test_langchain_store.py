"""Live integration tests for M3Store(BaseStore) — the LangGraph/LangMem surface (PR-2).

Runs the real ``m3_memory.langchain.M3Store`` against a real (tmp) m3 DB (repo
conftest autouse fixture isolates paths). Requires langgraph (the [langchain]
extra); skipped cleanly if absent.

Codifies the load-bearing PR-2 guarantees:
  * put→get round-trip incl. metadata_json split/merge (§2.4)
  * idempotent-by-key put = supersede (no duplicate row)
  * search returns SearchItem with score
  * delete via put(value=None) and via delete()
  * batch coalescing (multiple PutOps in one abatch)
  * list_namespaces returns (user_id, scope) tuples, never content (§6)
  * namespace tenancy enforced (empty user_id raises)
  * content-level tenant isolation through the store (the leak this PR found)
  * both sync (batch) and async (abatch) paths
"""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph", reason="needs the [langchain] extra")

from m3_memory.langchain import M3Store  # noqa: E402

# Warning surfaced, NOT suppressed (repo policy: never hide a warning; annotate it):
# a ".*non-main thread.*" UserWarning can appear because M3Store shares M3Client's
# process-wide DAEMON asyncio loop-thread (§8), which is still alive at test end by
# design (torn down at interpreter exit, not per test). Benign — not a leak. Left
# visible on purpose; do NOT kill the shared loop per test to silence it.


def test_put_get_roundtrip_and_metadata():
    s = M3Store()
    ns = ("alice", "user")
    s.put(ns, "pref1", {"content": "Alice likes dark mode", "category": "ui"})
    item = s.get(ns, "pref1")
    assert item is not None
    assert item.value["content"] == "Alice likes dark mode"
    assert item.value["category"] == "ui"           # metadata_json round-trip
    assert item.namespace == ns
    assert item.key == "pref1"


def test_idempotent_put_supersedes_no_duplicate():
    s = M3Store()
    ns = ("alice", "user")
    s.put(ns, "pref1", {"content": "dark mode"})
    s.put(ns, "pref1", {"content": "light mode"})    # same key -> supersede
    item = s.get(ns, "pref1")
    assert "light" in item.value["content"]
    # exactly one live row under this key
    hits = s.search(ns, query="mode", limit=10)
    same_key = [h for h in hits if h.key == "pref1"]
    assert len(same_key) <= 1


def test_search_returns_scored_items():
    s = M3Store()
    ns = ("alice", "user")
    s.put(ns, "k", {"content": "hiking in the mountains"})
    hits = s.search(ns, query="mountains", limit=5)
    assert len(hits) >= 1
    assert hits[0].score is not None


def test_delete_paths():
    s = M3Store()
    ns = ("alice", "user")
    s.put(ns, "k1", {"content": "to be deleted"})
    s.put(ns, "k1", None)                              # delete via put(None)
    assert s.get(ns, "k1") is None
    s.put(ns, "k2", {"content": "delete via method"})
    s.delete(ns, "k2")
    assert s.get(ns, "k2") is None


def test_namespace_tenancy_enforced():
    s = M3Store()
    with pytest.raises(ValueError):
        s.get((), "x")
    with pytest.raises(ValueError):
        s.put(("",), "x", {"content": "y"})


def test_list_namespaces_ids_only():
    s = M3Store()
    s.put(("alice", "user"), "a", {"content": "x"})
    s.put(("bob", "user"), "b", {"content": "y"})
    ns = s.list_namespaces()
    assert ("alice", "user") in ns
    assert ("bob", "user") in ns
    # tuples only — never content
    assert all(isinstance(n, tuple) for n in ns)


def test_content_level_tenant_isolation():
    """The leak this PR uncovered: a specific query must not cross tenants."""
    s = M3Store()
    s.put(("bob", "user"), "secret", {"content": "bob secret vault 9999"})
    alice = s.search(("alice", "user"), query="secret vault 9999", limit=5)
    assert not any("9999" in h.value.get("content", "") for h in alice)
    bob = s.search(("bob", "user"), query="secret vault 9999", limit=5)
    assert any("9999" in h.value.get("content", "") for h in bob)


async def test_async_abatch_path():
    """The path LangGraph/LangMem actually drive (aput/aget/asearch/adelete)."""
    s = M3Store()
    ns = ("alice", "user")
    await s.aput(ns, "k1", {"content": "async fact"})
    item = await s.aget(ns, "k1")
    assert item is not None and "async" in item.value["content"]
    hits = await s.asearch(ns, query="async fact", limit=5)
    assert len(hits) >= 1
    await s.adelete(ns, "k1")
    assert await s.aget(ns, "k1") is None


async def test_abatch_coalesces_multiple_puts():
    from langgraph.store.base import PutOp

    s = M3Store()
    ops = [PutOp(("carol", "user"), f"k{i}", {"content": f"carol {i}"})
           for i in range(5)]
    results = await s.abatch(ops)
    assert results == [None] * 5           # PutOp returns None
    # all five landed
    got = await s.asearch(("carol", "user"), query="carol", limit=10)
    assert len(got) == 5


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
