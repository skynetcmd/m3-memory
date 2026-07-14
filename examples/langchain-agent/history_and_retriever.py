"""Short-term chat history + RAG retrieval on m3 — the other two drop-ins.

Two more m3 surfaces, no LLM required to run this file:
  * M3ChatMessageHistory / with_m3_history — a BaseChatMessageHistory. Chatbot
    turns persist locally AND feed m3's async fact extraction for free.
  * M3Retriever — a BaseRetriever. Hybrid FTS5+vector recall as LangChain
    Documents, with m3's bitemporal + confidence signal in each Document.metadata.

Run:  pip install "m3-memory[langchain]"  &&  python history_and_retriever.py
"""

from langchain_core.messages import AIMessage, HumanMessage

from m3_memory.langchain import M3ChatMessageHistory, M3Retriever, Memory

USER = "alex"


def chat_history_demo() -> None:
    # session_id (classic) / thread_id (LangGraph) both map to conversation_id.
    history = M3ChatMessageHistory("session-42", user_id=USER)
    history.add_messages([
        HumanMessage(content="Book me a window seat next time."),
        AIMessage(content="Noted — window seat preference saved."),
        HumanMessage(content="And I'm vegetarian."),
    ])

    print("conversation (chronological, roles preserved):")
    for msg in history.messages:                 # oldest → newest
        print(f"  [{msg.type}] {msg.content}")

    # clear() wipes just this conversation (two-step, hidden behind one call).
    # history.clear()


def retriever_demo() -> None:
    # Seed a couple of facts (any m3 write is retrievable).
    mem = Memory(user_id=USER)
    mem.add("The project deadline is March 15th.")
    mem.add("The staging server hostname is staging.example.com.")

    retriever = M3Retriever(user_id=USER, k=3)
    docs = retriever.invoke("deadline")
    print("\nretrieved documents (with m3's extra metadata):")
    for d in docs:
        md = d.metadata
        print(f"  - {d.page_content}")
        print(f"      score={md.get('score')}, confidence={md.get('confidence')}, "
              f"valid_from={md.get('valid_from')}")


if __name__ == "__main__":
    chat_history_demo()
    retriever_demo()
