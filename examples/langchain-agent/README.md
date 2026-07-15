# LangChain / LangGraph agent on m3 memory

Five runnable scripts, each a different way to put **m3** under your LangChain
stack — from a one-line mem0 swap to a full LangGraph agent with persistent,
local-first memory. Two of them run with **no API key**; start there.

Full guide: [`docs/integrations/LANGCHAIN.md`](../../docs/integrations/LANGCHAIN.md).

| Script | Shows | Replaces / backs |
|---|---|---|
| `mem0_migration.py` | One-line import swap from mem0 → m3; `.add()` / `.search()` / extras | **mem0** (delete-and-replace) |
| `native_store.py` | Plain LangGraph `create_react_agent(store=M3Store())` | raw LangGraph store |
| `langmem_on_m3.py` | LangMem tools + background manager, backed by `M3Store()` | **LangMem** (backs it) |
| `history_and_retriever.py` | `M3ChatMessageHistory` + `M3Retriever` — short-term chat + RAG | chat history / vector retriever |
| `graph_checkpointer.py` | `M3Saver()` — pause/resume/time-travel a LangGraph run | LangGraph checkpointer (Sqlite/Postgres saver) |
| `agent_with_memory_and_persistence.py` | `create_react_agent` with **both** `M3Store` (memory) + `M3Saver` (persistence) | a full agent's memory + state layer |

`mem0_migration.py`, `history_and_retriever.py`, and `graph_checkpointer.py` run
with **no API key** (they exercise memory/state directly). `native_store.py` /
`langmem_on_m3.py` build a real agent, so they need a chat-model key.

## Setup
```bash
pip install "m3-memory[langchain]"       # the integration surface + langchain-core + langgraph
pip install langmem                      # only for langmem_on_m3.py
export ANTHROPIC_API_KEY=...             # only the agent examples; m3 needs no key

python mem0_migration.py                 # start here — the one-line swap
python history_and_retriever.py          # short-term history + RAG
```
m3 self-configures on first run (in-process embedder, auto-created SQLite DB, and
it self-heals the chatlog schema). No server, no vector DB, no wizard.

## The point
Every script stores memory **locally** and gains what mem0/LangMem can't do —
contradiction handling (`.supersede`), temporal queries (`as_of=`), commanded
forgetting (`.forget`), and hybrid + graph retrieval (`.related`). See
[§3 "What you gain — the m3-native extras"](../../docs/integrations/LANGCHAIN.md#3-what-you-gain--the-m3-native-extras)
of the integration guide.

## Migrating an existing mem0 codebase

Point the scanner at your project to see the swap before you touch anything —
it's AST-based, so it flags real mem0 usage, not the substring in a comment:

```bash
python bin/mem0_scan.py path/to/your/app        # report: imports + per-call notes
python bin/mem0_scan.py path/to/your/app --fix  # rewrite `from mem0 import ...` in place
```

Each call site is classified: **OK** (drop-in, no change), **MAP** (works, but an
m3-native verb like `.supersede`/`.forget` is stronger), or **STOP** (no
equivalent — handle by hand). `--fix` only rewrites import lines; MAP/STOP call
sites are left for you to review.
