"""Live integration tests for the mem0-compat LangChain surface (PR-1).

Runs the real ``m3_memory.langchain.Memory`` against a real (tmp) m3 DB — the
repo conftest's autouse ``_isolate_db_paths`` fixture points M3_DATABASE /
CHATLOG_DB_PATH at a per-test tmp dir, so these never touch production data.

These codify the load-bearing PR-1 guarantees so a regression is caught in CI:
  * read-your-writes (§0a — the #1 mem0 drop-in trap)
  * mem0 field shape ``memory`` not ``content`` (§2.4 rename trap)
  * temporal/confidence surfaced via direct-impl ``extra_columns`` (§2.4)
  * content-level tenant isolation, both directions (§6/§7 privacy)
  * fail-loud on missing user_id (§3)
  * the two dispatch paths: typed ``.delete()`` ungated, ``.call()`` gated (§2.1b)
  * bulk ``.add(list)`` coalesces + returns ids (§4/§11)
  * m3-native extras: ``.supersede`` / ``.forget`` / ``.related``

No LangChain import is required — the mem0-compat surface is pure m3.
"""

from __future__ import annotations

import pytest

# The repo conftest puts bin/ on sys.path and isolates DB paths (autouse).
from m3_memory.langchain import M3Memory, Memory, MemoryClient

# The M3Client shares ONE persistent daemon event-loop thread process-wide (§8
# performance — pool + embedder affinity). It's a daemon (dies with the process)
# and intentionally long-lived, so pytest's "left non-main thread alive" check is
# expected here, not a leak.
pytestmark = pytest.mark.filterwarnings(
    "ignore:.*non-main thread.*:UserWarning"
)


def test_class_aliases_are_the_same():
    assert Memory is M3Memory is MemoryClient


def test_read_your_writes_and_mem0_shape():
    m = Memory(user_id="alex")
    added = m.add("I love hiking in the Alps", user_id="alex")
    new_id = added["results"][0]["id"]
    # id is a real uuid, not the raw "Created: ..." string
    assert new_id and "Created" not in str(new_id) and len(str(new_id)) == 36

    # read-your-writes: the just-written memory is immediately retrievable via
    # the deterministic listing (independent of FTS tokenization / async embed).
    listed = m.get_all(user_id="alex")["results"]
    assert any(r["id"] == new_id for r in listed)
    r0 = listed[0]
    # mem0 field shape — the rename trap
    assert "memory" in r0 and "content" not in r0
    assert r0["memory"]


def test_temporal_fields_surface():
    m = Memory(user_id="alex")
    m.add("The sky is a deep azure blue today", user_id="alex")
    # Use the deterministic listing for the existence check (search word-matching
    # on a fresh DB is subject to FTS tokenization + async embedding backfill;
    # the temporal-surfacing contract is what we're asserting, not FTS recall).
    r0 = m.get_all(user_id="alex")["results"][0]
    md = r0.get("metadata", {})
    # confidence + valid_from are always present on a fresh write; valid_to is
    # empty (still-valid) and correctly omitted.
    assert "confidence" in md
    assert "valid_from" in md


def test_content_level_tenant_isolation():
    m = Memory()
    m.add("Bob secret: the vault code is 4429", user_id="bob")
    # alex must NOT see bob's content — via search AND the deterministic listing
    # (isolation is enforced on user_id at the SQL layer for both paths).
    alex_search = m.search("vault code 4429", user_id="alex")["results"]
    assert not any("4429" in r.get("memory", "") for r in alex_search)
    alex_all = m.get_all(user_id="alex")["results"]
    assert not any("4429" in r.get("memory", "") for r in alex_all)
    # bob CAN find his own (deterministic listing — always retrievable)
    bob_all = m.get_all(user_id="bob")["results"]
    assert any("4429" in r.get("memory", "") for r in bob_all)


def test_missing_user_id_raises():
    with pytest.raises(ValueError):
        Memory().add("anonymous fact")
    with pytest.raises(ValueError):
        Memory().search("q")


def test_bulk_add_returns_ids():
    m = Memory(user_id="alex")
    res = m.add(["python fact", "rust fact", "go fact"], user_id="alex")
    assert len(res["results"]) == 3
    assert all(r["id"] for r in res["results"])


def test_get_all_and_get_by_id():
    m = Memory(user_id="alex")
    added = m.add("fact for retrieval", user_id="alex")
    nid = added["results"][0]["id"]
    allr = m.get_all(user_id="alex")
    assert len(allr["results"]) >= 1
    one = m.get(nid)
    assert one is not None and "memory" in one


def test_two_dispatch_paths_gate_behavior():
    """The crux of §2.1b: same memory_delete tool is GATED through .call()
    (LLM-facing passthrough) but UNGATED through the typed .delete() (explicit
    user API) — with no MCP_PROXY_ALLOW_DESTRUCTIVE set."""
    m = Memory(user_id="alex")
    nid = m.add("deletable fact", user_id="alex")["results"][0]["id"]

    # .call() passthrough: destructive tool is gated
    gated = m.call("memory_delete", id=nid)
    assert isinstance(gated, dict) and gated.get("error") == "destructive_gated"

    # read-only tool through .call() works and returns the envelope
    status = m.call("chatlog_status")
    assert isinstance(status, dict) and status.get("ok") is True

    # typed .delete() is ungated — succeeds without the env flag
    out = m.delete(nid)
    assert "deleted" in out.get("message", "").lower()


def test_extras_supersede_forget_related():
    m = Memory(user_id="alex")
    first = m.add("I use Python for everything", user_id="alex")["results"][0]["id"]

    sup = m.supersede(first, "I switched to Rust", user_id="alex")
    assert sup["old_id"] == first and sup["new_id"]

    rel = m.related(first)
    assert "graph" in rel

    forgotten = m.forget(user_id="alex")
    assert forgotten["forgotten_user"] == "alex"
    # after forget, alex has nothing
    assert m.get_all(user_id="alex")["results"] == []


def test_from_config_accepts_and_ignores():
    # mem0-style config with infra keys must not raise
    m = Memory.from_config({
        "embedder": {"provider": "openai"},
        "vector_store": {"provider": "qdrant"},
        "user_id": "carol",
    })
    assert isinstance(m, Memory)
    # the known key (user_id) became the default
    assert m._default_user_id == "carol"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
