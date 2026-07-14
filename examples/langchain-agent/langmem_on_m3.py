"""Back LangMem with m3 — keep LangMem's tools + background manager, swap the store.

Verified against LangMem source (2026-07-13): LangMem calls only the store's
asearch/aput/adelete/aget, passes a raw NL query, and lets the store embed — which
is exactly what m3 does. So `store=M3Store()` needs NO index/embed config and NO
shim. Unlike InMemoryStore, m3 persists across restarts.

Run:  pip install "m3-memory[langchain]" langmem   # the extra already pulls langgraph
      export ANTHROPIC_API_KEY=...   &&   python langmem_on_m3.py
"""

from langgraph.prebuilt import create_react_agent
from langmem import (
    create_manage_memory_tool,
    create_memory_store_manager,
    create_search_memory_tool,
)

from m3_memory.langchain import M3Store  # ← the store change (was InMemoryStore(index=...))

MODEL = "anthropic:claude-3-5-sonnet-latest"

# m3 embeds in-process — no `index={"dims":..., "embed":...}` block needed.
store = M3Store()


# --- Pattern A: in-conversation memory tools (agent decides when to save) ----
def agent_with_memory_tools():
    agent = create_react_agent(
        MODEL,
        tools=[
            create_manage_memory_tool(namespace=("memories",)),
            create_search_memory_tool(namespace=("memories",)),
        ],
        store=store,  # ← m3 backs LangMem's tools
    )
    agent.invoke({"messages": [{"role": "user", "content": "Remember I prefer dark mode."}]})
    resp = agent.invoke({"messages": [{"role": "user", "content": "What are my UI preferences?"}]})
    print("agent:", resp["messages"][-1].content)


# --- Pattern B: background manager (auto extract/consolidate/update) ----------
# Verified signature: create_memory_store_manager(model, /, *, namespace=..., store=...)
# The manager searches, extracts, updates, and versions memories from a message
# stream. Passing store=M3Store() persists all of that in m3.
def background_extraction():
    manager = create_memory_store_manager(
        MODEL,
        namespace=("memories", "{langgraph_user_id}"),  # LangMem's default template
        store=store,                                     # ← m3 backs the manager
    )
    # Feed it a conversation; it extracts durable facts on its own.
    manager.invoke({
        "messages": [
            {"role": "user", "content": "I moved from Python to Rust for the perf work."},
            {"role": "assistant", "content": "Got it — Rust for the performance-critical path."},
        ]
    })
    # Those extracted memories are now searchable through the same m3 store.
    hits = store.search(("memories", "default"), query="programming language", limit=5)
    print("background-extracted facts in m3:")
    for it in hits:
        print(f"  - {it.value}")


if __name__ == "__main__":
    agent_with_memory_tools()
    background_extraction()
