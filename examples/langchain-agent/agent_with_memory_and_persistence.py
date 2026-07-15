"""A full LangGraph ReAct agent on m3: long-term memory + run persistence together.

The two m3 LangChain surfaces do DIFFERENT jobs, and a real agent wants both:

  * ``M3Store``  — long-term MEMORY (what the agent knows: facts, preferences).
                   LangGraph passes it to tools/nodes as ``store``; LangMem-style
                   managers and ``create_react_agent`` read/write it.
  * ``M3Saver``  — run PERSISTENCE (the checkpointer: pause/resume/time-travel the
                   graph's machine state). Passed as ``checkpointer``.

This example wires both into one ``create_react_agent`` so the agent remembers
across threads AND its runs survive a restart — all local, no server.

Needs a chat-model key (the agent calls an LLM):
    pip install "m3-memory[langchain]"
    export ANTHROPIC_API_KEY=...
    python agent_with_memory_and_persistence.py

Tip: set ``M3_DEFAULT_USER_ID`` once and you can drop ``user_id=`` from the
namespace tuples below — handy for a single-user app.
"""

from langchain.chat_models import init_chat_model
from langgraph.prebuilt import create_react_agent

from m3_memory.langchain import M3Saver, M3Store

USER = "alex"


def save_preference(preference: str) -> str:
    """Persist a user preference to long-term memory. Call this whenever the user
    states a durable preference (diet, tone, tools, working hours)."""
    # The agent's store is injected by LangGraph; we reach it via the closure in
    # a real app. Here we write directly to show the M3Store surface.
    store.put((USER, "user"), key=f"pref:{preference[:24]}", value={"text": preference})
    return f"Saved: {preference}"


def recall_preferences(query: str) -> str:
    """Search the user's long-term preferences for anything relevant to `query`."""
    hits = store.search((USER, "user"), query=query, limit=5)
    if not hits:
        return "No stored preferences matched."
    return "Found:\n" + "\n".join(f"- {h.value.get('text', '')}" for h in hits)


# ── the two m3 surfaces ──────────────────────────────────────────────────────
store = M3Store()          # long-term memory  (LangGraph BaseStore)
saver = M3Saver()          # run persistence   (LangGraph BaseCheckpointSaver)


def main() -> None:
    model = init_chat_model("anthropic:claude-sonnet-5")
    agent = create_react_agent(
        model,
        tools=[save_preference, recall_preferences],
        store=store,          # ← long-term memory
        checkpointer=saver,   # ← pause/resume persistence
    )

    cfg = {"configurable": {"thread_id": "conv-1"}}

    # Turn 1 — the agent stores a preference in long-term memory.
    r1 = agent.invoke(
        {"messages": [("user", "Remember that I'm vegetarian and prefer terse answers.")]},
        cfg,
    )
    print("assistant:", r1["messages"][-1].content)

    # Turn 2 — a LATER run on the SAME thread. Because M3Saver persisted the
    # thread state, the agent resumes with full conversation context; because
    # M3Store persisted the fact, it can recall the preference across threads too.
    r2 = agent.invoke(
        {"messages": [("user", "What do you know about my diet? Suggest a lunch.")]},
        cfg,
    )
    print("assistant:", r2["messages"][-1].content)

    # The run is checkpointed — inspect or replay it.
    history = list(agent.get_state_history(cfg))
    print(f"\n{len(history)} checkpoints recorded for this thread (replayable).")
    prefs = recall_preferences("food")
    print(prefs)


if __name__ == "__main__":
    main()
