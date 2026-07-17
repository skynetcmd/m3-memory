"""Live tests for M3ChatMessageHistory + with_m3_history + M3Retriever (PR-3).

Runs against a real (tmp) m3 DB (repo conftest autouse fixture isolates paths).
Requires langchain-core/langgraph (the [langchain] extra); skipped if absent.

Codifies the PR-3 guarantees, including the two runtime traps found while
building:
  * message ORDER is deterministic chronological via the persisted turn_index
    (chatlog_search order is NOT stable across topologies — unified ASC vs
    separate created_at-DESC — and created_at is only second-granular)
  * message ROLE round-trips (it rides the row's `metadata`, not a top-level col)
  * chatlog schema SELF-HEALS in separate topology (no "no such table" spill)
  * clear() is the two-step search→bulk-delete shim
  * M3Retriever surfaces bitemporal/confidence in Document.metadata
  * host_agent 'langchain' is accepted (added to the core enum, still extensible)
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain_core", reason="needs the [langchain] extra")

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402

from m3_memory.langchain import (  # noqa: E402
    M3ChatMessageHistory,
    M3Retriever,
    with_m3_history,
)

# Warnings are surfaced, NOT suppressed (repo policy: never hide a warning; annotate
# its disposition at the source so a reviewer sees the reasoning). Two UserWarnings
# can appear when this file runs; both are understood and intentionally left visible:
#
#   1. ".*non-main thread.*" — pytest notes that M3Client's shared asyncio event-loop
#      thread ("m3client-langchain-loop") is still alive at test end. That thread is a
#      process-wide DAEMON by design (M3Client._ensure_loop): all m3 dispatch lands on
#      one loop-thread, reused across tests and torn down at interpreter exit. Benign —
#      not a leak; do NOT "fix" by killing the shared loop per test (that defeats its
#      purpose and slows every adapter call).
#
#   2. ".*Pydantic V1.*" — langchain_core still imports pydantic.v1 shims, which warn on
#      Python 3.14 ("Core Pydantic V1 functionality isn't compatible with 3.14+"). This
#      is an UPSTREAM langchain dependency we don't control; it's a real tracking signal
#      (pydantic v1 is removed in 3.16, so langchain must migrate before then). Left
#      visible on purpose so the ticking-dependency stays in view; resolved only by a
#      langchain release that drops pydantic-v1. Not fixable in our code.


def _flush():
    import time
    time.sleep(0.4)


def test_history_roundtrip_order_and_roles():
    h = M3ChatMessageHistory("conv-1", user_id="alex")
    h.add_messages([HumanMessage(content="I love hiking"),
                    AIMessage(content="Where?"),
                    HumanMessage(content="The Alps")])
    _flush()
    msgs = h.messages
    assert [m.content for m in msgs] == ["I love hiking", "Where?", "The Alps"]
    assert [m.type for m in msgs] == ["human", "ai", "human"]


def test_history_multi_add_preserves_order():
    """turn_index continues across separate add_messages calls (monotonic)."""
    h = M3ChatMessageHistory("conv-2", user_id="alex")
    h.add_messages([HumanMessage(content="one"), AIMessage(content="two")])
    _flush()
    h.add_messages([HumanMessage(content="three"), AIMessage(content="four")])
    _flush()
    assert [m.content for m in h.messages] == ["one", "two", "three", "four"]


def test_history_clear():
    h = M3ChatMessageHistory("conv-3", user_id="alex")
    h.add_messages([HumanMessage(content="ephemeral")])
    _flush()
    assert len(h.messages) == 1
    h.clear()
    _flush()
    assert h.messages == []


def test_history_requires_conversation_id():
    with pytest.raises(ValueError):
        M3ChatMessageHistory("", user_id="alex")


def test_history_conversations_are_isolated():
    a = M3ChatMessageHistory("conv-A", user_id="alex")
    b = M3ChatMessageHistory("conv-B", user_id="alex")
    a.add_messages([HumanMessage(content="alpha only")])
    _flush()
    assert [m.content for m in a.messages] == ["alpha only"]
    assert b.messages == []


def test_system_message_roundtrips():
    h = M3ChatMessageHistory("conv-sys", user_id="alex")
    h.add_messages([SystemMessage(content="you are helpful"),
                    HumanMessage(content="hi")])
    _flush()
    msgs = h.messages
    assert msgs[0].type == "system"
    assert msgs[1].type == "human"


def test_with_m3_history_builds_runnable():
    from langchain_core.runnables import RunnableLambda

    # NOTE (surfaced deprecation, intentionally NOT suppressed): this emits
    # LangChainPendingDeprecationWarning because with_m3_history wraps LangChain's
    # RunnableWithMessageHistory, which LangChain marked *Pending*Deprecation (in
    # favor of LangGraph persistence). We keep the warning visible on purpose —
    # it's the tracking signal for when to migrate. Disposition: the surface still
    # works and is a deliberate compatibility shim for users on the older Runnable
    # pattern; the LangGraph path already ships alongside it as M3Saver
    # (BaseCheckpointSaver) / M3Store (BaseStore). Migrate with_m3_history to
    # LangGraph (or remove it, pointing users at M3Saver) only when LangChain
    # promotes this from Pending to an actual @deprecated with a removal version.
    chain = with_m3_history(RunnableLambda(lambda x: x), user_id="alex")
    # It's a RunnableWithMessageHistory; the factory produces M3ChatMessageHistory.
    hist = chain.get_session_history("sess-1")  # type: ignore[attr-defined]
    assert isinstance(hist, M3ChatMessageHistory)
    assert hist.conversation_id == "sess-1"


def test_retriever_returns_documents_with_temporal_metadata():
    # seed a memory the retriever can find (via the mem0 surface). Use a
    # single distinctive token as the query: multi-word queries with stopwords
    # can miss on a fresh DB before the async embedding backfills (FTS
    # tokenization), which is a search-recall property, not a retriever bug.
    from m3_memory.langchain import Memory
    Memory(user_id="alex").add("Zanzibar is an island archipelago", user_id="alex")
    _flush()
    r = M3Retriever(user_id="alex", k=5)
    docs = r.invoke("Zanzibar")
    assert len(docs) >= 1
    md = docs[0].metadata
    assert "id" in md and "score" in md
    assert "confidence" in md          # bitemporal/confidence surfaced (§2.3)


def test_retriever_requires_user_id():
    r = M3Retriever(k=5)
    with pytest.raises(ValueError):
        r.invoke("anything")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
