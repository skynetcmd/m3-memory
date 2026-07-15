"""Persist LangGraph runs with m3 — pause, resume, and time-travel, no API key.

``M3Saver`` is a LangGraph ``BaseCheckpointSaver`` backed by m3's local SQLite
engine DB. Drop it into ``builder.compile(checkpointer=M3Saver())`` and every
super-step's state is persisted locally — so a graph can hit a human-in-the-loop
``interrupt()``, the process can exit, and a later run resumes exactly where it
stopped. This is a DIFFERENT surface from long-term memory (``M3Store``) and chat
history (``M3ChatMessageHistory``): a checkpoint is machine state, not knowledge,
so it never touches m3's embedder or contradiction pipeline.

Runs with **no API key** — the graph here is plain Python, no chat model.

Run:  pip install "m3-memory[langchain]"  &&  python graph_checkpointer.py
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from m3_memory.langchain import M3Saver  # noqa: F401  (see note below if this fails)

# NOTE: `from m3_memory.langchain import ...` requires an m3 build that ships the
# integration payload (m3_memory/integrations/langchain/). If you installed m3
# before the LangChain integration landed, upgrade: `pip install -U m3-memory`.


class State(TypedDict):
    count: int
    log: list


def bump(state: State) -> dict:
    return {"count": state["count"] + 1, "log": state["log"] + ["bump"]}


def approval_gate(state: State) -> dict:
    # interrupt() pauses the graph and persists state via the checkpointer. The
    # run stops here until it's resumed with an answer.
    decision = interrupt({"question": "approve continue?", "count": state["count"]})
    return {"log": state["log"] + [f"gate:{decision}"]}


def finish(state: State) -> dict:
    return {"count": state["count"] + 100, "log": state["log"] + ["finish"]}


def main() -> None:
    builder = StateGraph(State)
    builder.add_node("bump", bump)
    builder.add_node("gate", approval_gate)
    builder.add_node("finish", finish)
    builder.add_edge(START, "bump")
    builder.add_edge("bump", "gate")
    builder.add_edge("gate", "finish")
    builder.add_edge("finish", END)

    graph = builder.compile(checkpointer=M3Saver())
    cfg = {"configurable": {"thread_id": "demo-thread", "user_id": "alex"}}

    # 1) Run until the human-in-the-loop interrupt.
    result = graph.invoke({"count": 0, "log": []}, cfg)
    print("paused at the gate:", "__interrupt__" in result)

    # 2) State is now durable in m3. Inspect where the run stopped — this would
    #    work identically after a full process restart (same DB + thread_id).
    snap = graph.get_state(cfg)
    print(f"persisted count={snap.values['count']}, next node={snap.next}")

    # 3) Resume with the human's answer; the graph runs to completion.
    final = graph.invoke(Command(resume="yes"), cfg)
    print("final count:", final["count"], "log:", final["log"])

    # 4) Time-travel: every super-step left a checkpoint you can list/replay.
    history = list(graph.get_state_history(cfg))
    print(f"{len(history)} checkpoints recorded for this thread (replayable).")


if __name__ == "__main__":
    main()
