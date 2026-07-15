"""LCEL-native m3 memory — compose reads/writes into a chain, no API key.

Shows the write side of memory as a first-class `Runnable`:

  * MemoryRetrieve(...)   — recall as the head of an LCEL pipe
  * MemoryWrite(...)      — persist-and-pass-through at the tail
  * with_m3_memory        — a decorator that captures a callable's I/O
  * M3Retriever.explain() — the real retrieval signal, for LangSmith / logging

Runs with **no API key** — it exercises memory directly (no chat model). To keep
it self-contained, the "llm" step is a plain lambda; swap in a real model to see
the same pipe drive an agent.

    pip install "m3-memory[langchain]"
    python lcel_memory.py

Tip: `export M3_DEFAULT_USER_ID=alex` and you can drop `user_id=` below.
"""

from langchain_core.runnables import RunnableLambda

from m3_memory.langchain import (
    M3Retriever,
    MemoryRetrieve,
    MemoryWrite,
    with_m3_memory,
)

USER = "alex"


def main() -> None:
    # Seed a couple of memories so retrieval has something to find.
    seed = MemoryWrite(user_id=USER)
    seed.invoke("The deploy window is Friday at 3pm.")
    seed.invoke("Alex prefers terse, bulleted answers.")

    # --- an LCEL pipe: recall -> "reason" -> persist the turn ---------------
    # MemoryRetrieve is the head; a lambda stands in for prompt|llm; MemoryWrite
    # is the tail (it returns its input unchanged, so it composes at the end).
    def summarize(docs) -> str:
        facts = "; ".join(d.page_content for d in docs) or "(nothing on file)"
        return f"Answer, grounded in memory: {facts}"

    chain = (
        MemoryRetrieve(user_id=USER, k=4)
        | RunnableLambda(summarize)
        | MemoryWrite(user_id=USER)          # persists the produced answer
    )
    answer = chain.invoke("when is the deploy and how should I answer?")
    print("chain output:", answer)

    # --- the decorator: capture a function's input + output automatically ---
    @with_m3_memory(user_id=USER)
    def handle(question: str) -> str:
        return "Deploy is Friday 3pm; keeping it terse."

    print("decorated:", handle("remind me of the deploy time?"))

    # --- honest observability: the real retrieval signal --------------------
    r = M3Retriever(user_id=USER, k=3)
    explanation = r.explain("deploy")
    print("\nretrieval explanation (attach to a LangSmith run's metadata):")
    print("  config:", explanation["config"])
    for hit in explanation["results"]:
        print(f"  - score={hit['score']:.3f} conf={hit.get('confidence')} "
              f"type={hit.get('type')} :: {hit['preview']}")


if __name__ == "__main__":
    main()
