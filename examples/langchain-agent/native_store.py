"""Give any LangGraph agent persistent cross-session memory in one line.

M3Store is a plain LangGraph BaseStore, so no memory library is required — just
pass it to create_react_agent. Memory survives restarts and is local-first.

Run:  pip install "m3-memory[langchain]"   # includes langchain-core + langgraph
      export ANTHROPIC_API_KEY=...   &&   python native_store.py
"""

from m3_memory.langchain import M3Store

# from langgraph.prebuilt import create_react_agent   # for the agent line below
MODEL = "anthropic:claude-3-5-sonnet-latest"  # noqa: F841 — referenced in the comment


def main() -> None:
    store = M3Store()

    # That's the whole setup — one arg gives the agent persistent memory.
    # (Building the agent needs a chat-model key; the store demo below does not.)
    #     agent = create_react_agent(MODEL, tools=[], store=store)

    # BaseStore is namespaced by a tuple; m3 maps it to (user_id[, scope]).
    ns = ("alex",)

    # Write a durable memory, then read it back across a fresh search.
    store.put(ns, key="pref-1", value={"content": "Prefers metric units."})
    hits = store.search(ns, query="units", limit=3)
    print("recall:")
    for it in hits:
        print(f"  - {it.value['content']}   (score={it.score})")

    # Run this file twice: the memory from the first run is still here on the
    # second — that's the difference from InMemoryStore.


if __name__ == "__main__":
    main()
