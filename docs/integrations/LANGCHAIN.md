# Drop m3 into your LangChain stack

**Change one import. Keep every line of your mem0 code. Get memory that
remembers *better*.**

```diff
- from mem0 import Memory
+ from m3_memory.langchain import Memory
```

That's the whole migration. Your `Memory()`, your `.add()`, your `.search()` —
untouched. But now every fact is stored **on your machine** (no server, no vector
DB, no API key, no data leaving your laptop), and you get four things mem0 and
LangMem simply don't have: **contradiction handling, time-travel queries,
commanded forgetting, and hybrid + graph retrieval.**

m3 is a local-first agent-memory system — hybrid FTS5 + vector + MMR recall, a
bitemporal model, knowledge-graph supersession (real contradiction handling, not
flat dedup), and async fact extraction — that speaks five standard LangChain
surfaces:

| You use… | Swap in… | Section |
|---|---|---|
| **mem0** | `from m3_memory.langchain import Memory` — one import | [§1](#1-replacing-mem0) |
| **LangMem** / raw LangGraph | `store=M3Store()` | [§2](#2-replacing--backing-langmem) |
| chat history / RAG retriever | `M3ChatMessageHistory` · `M3Retriever` | [§2](#short-term-chat-history--rag-retrieval) |
| **LangGraph checkpointer** | `checkpointer=M3Saver()` — pause / resume / time-travel | [§2](#langgraph-checkpointing-with-m3saver) |
| *the extras nothing else has* | `.supersede` · `as_of=` · `.forget` · `.related` | [§3](#3-what-you-gain-the-m3-native-extras) |

> **Install** — `pip install "m3-memory[langchain]"`. m3 self-configures on first
> use (in-process embedder; the SQLite DB is created and migrated for you). No
> wizard, no infra, nothing to provision.

---

## 1. Replacing mem0

The migration is **one import line**. Everything below it is byte-identical to
your existing mem0 program — same class name, same constructor, same method calls,
same return shape.

### Before (mem0) → After (m3)
```python
from mem0 import Memory                    # ← delete this line
from m3_memory.langchain import Memory     # ← add this one. Nothing else changes.

memory = Memory()

memory.add(
    [
        {"role": "user", "content": "I prefer dark mode and I'm allergic to peanuts."},
        {"role": "assistant", "content": "Noted!"},
    ],
    user_id="alex",
)

relevant = memory.search("dark mode", user_id="alex", limit=3)
print("\n".join(f"- {m['memory']}" for m in relevant["results"]))
```

`.add(messages, *, user_id=…)` and `.search(query, *, user_id=…, limit=…,
filters=…)` mirror mem0's OSS signatures (`filters` narrows by `type`, like
mem0's metadata filter), and the return shape
(`{"results": [{"id", "memory", "score", ...}]}`) matches mem0 — so code that
reads mem0 responses keeps working unchanged.

> **Read-your-writes:** `memory.add(...)` writes the turn synchronously (so an
> immediate `.search()` finds it) *and* kicks off m3's deeper async fact-extraction
> in the background — you get mem0's instant behavior plus richer long-term memory a
> moment later. Pass `extract=False` to skip the async pass.

### `Memory.from_config(...)` — your existing mem0 config just works
m3 supplies its own embedder and store, so it **accepts and ignores** mem0's
`embedder` / `vector_store` / `llm` config blocks (logging one line about what it
overrode). You do not need to remove your config to migrate.
```python
from m3_memory.langchain import Memory

# a mem0-style config dict — infra keys are harmlessly ignored by m3
memory = Memory.from_config({
    "vector_store": {"provider": "qdrant", "config": {...}},   # ignored — m3 owns the store
    "embedder":     {"provider": "openai", "config": {...}},   # ignored — m3 embeds in-process
})
```

### Hosted mem0 (`MemoryClient`)
```python
# before:  from mem0 import MemoryClient
from m3_memory.langchain import MemoryClient      # same methods, local-first, no API key
client = MemoryClient()
```

---

## 2. Replacing / backing LangMem

LangMem runs on a LangGraph **`BaseStore`**. m3 provides one — `M3Store` — so you
keep LangMem's tools and managers and simply **point them at m3**. Verified against
LangMem source: it calls only `asearch` / `aput` / `adelete` / `aget`, all of which
`M3Store` implements, and it lets the store do the embedding — which is exactly what
m3 does. **No shim, no `index=` config needed.**

### Before (LangMem on the in-memory store)
```python
from langgraph.prebuilt import create_react_agent
from langgraph.store.memory import InMemoryStore
from langmem import create_manage_memory_tool, create_search_memory_tool

store = InMemoryStore(
    index={"dims": 1536, "embed": "openai:text-embedding-3-small"},  # needed: InMemoryStore can't embed itself
)

agent = create_react_agent(
    "anthropic:claude-3-5-sonnet-latest",
    tools=[
        create_manage_memory_tool(namespace=("memories",)),
        create_search_memory_tool(namespace=("memories",)),
    ],
    store=store,
)
```

### After (LangMem on m3) — swap the store; drop the embed config
```python
from langgraph.prebuilt import create_react_agent
from langmem import create_manage_memory_tool, create_search_memory_tool
from m3_memory.langchain import M3Store            # ← the store change

store = M3Store()                                  # m3 embeds in-process — no index/embed config

agent = create_react_agent(
    "anthropic:claude-3-5-sonnet-latest",
    tools=[
        create_manage_memory_tool(namespace=("memories",)),
        create_search_memory_tool(namespace=("memories",)),
    ],
    store=store,
)
```

Everything else — LangMem's manage/search tools — works unchanged, now persisted
locally in m3 (survives restart, unlike `InMemoryStore`).

#### LangMem's background manager on m3
LangMem's `create_memory_store_manager` auto-extracts, consolidates, and versions
memories from a message stream. It takes a `store` argument (or reads it from the
LangGraph config), so pointing it at m3 is one change. *(Signature verified against
langmem source, 2026-07-13.)*

```python
from langmem import create_memory_store_manager
from m3_memory.langchain import M3Store

store = M3Store()                                  # no index/embed config needed

manager = create_memory_store_manager(
    "anthropic:claude-3-5-sonnet-latest",
    namespace=("memories", "{langgraph_user_id}"), # LangMem's default template
    store=store,                                   # ← m3 backs the manager
)

# Feed it a conversation; it extracts durable facts on its own, into m3.
manager.invoke({
    "messages": [
        {"role": "user", "content": "I moved from Python to Rust for the perf work."},
        {"role": "assistant", "content": "Got it — Rust for the performance path."},
    ]
})

# The extracted memories are searchable through the same m3 store.
hits = store.search(("memories", "default"), query="programming language", limit=5)
```
Because m3 handles embedding, contradiction (supersession), and temporal versioning
natively, LangMem's "maintains a versioned history of all changes" runs on m3's real
bitemporal + KG model rather than a flat store.

### Raw LangGraph (no memory library)
`M3Store` is a plain `BaseStore`, so you can give any LangGraph agent persistent
cross-session memory in one line:
```python
from langgraph.prebuilt import create_react_agent
from m3_memory.langchain import M3Store

agent = create_react_agent(model, tools, store=M3Store())   # done
```

### Namespaces ⇄ users
m3 maps the LangGraph namespace tuple to its tenancy model. The only concept you
need is a **user id**:
```python
("alex",)                 # everything for user "alex"
("alex", "work-project")  # optional second element = an isolated scope within the user
```

### Short-term chat history & RAG retrieval
Two more standard slots, both backed by m3:

```python
from langchain_core.messages import HumanMessage, AIMessage
from m3_memory.langchain import M3ChatMessageHistory, M3Retriever, with_m3_history

# BaseChatMessageHistory — chat turns persist locally AND feed m3's async fact
# extraction. session_id / thread_id both map to m3's conversation_id.
history = M3ChatMessageHistory("session-42", user_id="alex")
history.add_messages([HumanMessage(content="I'm vegetarian"),
                      AIMessage(content="Noted.")])
history.messages          # oldest -> newest, roles preserved
history.clear()           # wipe just this conversation

# One-liner to wrap a runnable so its history persists to m3:
chain = with_m3_history(prompt | model, user_id="alex")
chain.invoke({"input": "hi"}, config={"configurable": {"session_id": "conv-1"}})

# BaseRetriever — hybrid recall as LangChain Documents, with m3's bitemporal +
# confidence signal in each Document.metadata.
retriever = M3Retriever(user_id="alex", k=4)
docs = retriever.invoke("deadline")   # -> [Document(page_content=…, metadata={id, score, confidence, valid_from, …})]
```

### LangGraph checkpointing with `M3Saver`

`M3Store` (above) is *long-term memory* — what your agent knows. A **checkpointer**
is a different slot: the *machine state* LangGraph needs to pause, resume, and
time-travel a run. `M3Saver` implements LangGraph's `BaseCheckpointSaver`, so a
graph can hit a human-in-the-loop `interrupt()`, the process can exit, and a later
run resumes exactly where it stopped — persisted to m3's local engine DB, no
external checkpoint store to run.

```python
from m3_memory.langchain import M3Saver
from langgraph.types import Command   # for resuming after an interrupt()

graph = builder.compile(checkpointer=M3Saver())
cfg = {"configurable": {"thread_id": "t-1"}}   # thread_id is the resume key

graph.invoke({"input": "…"}, cfg)              # runs until an interrupt() pauses it
graph.get_state(cfg)                           # inspect the persisted state
graph.invoke(Command(resume="yes"), cfg)       # resume to completion
list(graph.get_state_history(cfg))             # every super-step — replay / time-travel
```

A checkpoint is opaque graph state, not knowledge, so `M3Saver` stores it in its
own tables and deliberately **bypasses** m3's embedder and contradiction pipeline
(two checkpoints of a thread aren't contradictions to reconcile). An optional
`user_id` in `configurable` scopes reads to that user's threads. Sync and async
(`aget_tuple` / `aput` / `alist`) are both implemented. See a runnable end-to-end
example in [`examples/langchain-agent/graph_checkpointer.py`](../../examples/langchain-agent/graph_checkpointer.py).

---

## 3. What you gain — the m3-native extras

Ever had an agent confidently repeat a fact the user corrected three turns ago?
Surface a preference that changed months back? Get asked to "forget that" and have
no verb for it? These are the things flat vector memory can't do — and they're
first-class methods on the *same* `Memory` object, so your mem0-style code stays
untouched while these are just *there* when you reach for them.

> **On `.search()`:** the mem0-compatible call is `memory.search(query, *,
> user_id=…, limit=…, filters=…)` (§1). The extras below are either extra
> keyword args on that *same* method (`as_of=`, `recency_bias=`) or additional
> typed methods (`.supersede`, `.forget`, `.related`) — all optional, so mem0
> code keeps working untouched while new code reaches for them.

```python
from m3_memory.langchain import Memory
memory = Memory(user_id="alex")

# 1) CONTRADICTION — evolve a fact instead of stacking a conflicting copy
#    ("I use Python" -> "I switched to Rust" without both living forever)
memory.supersede(old_id, "I switched to Rust")

# 2) TEMPORAL — ask what was true at a point in time (native bitemporal query).
#    `as_of` is an m3-native keyword on the SAME .search() method.
memory.search("stack", as_of="2026-01-01")

# 3) FORGETTING — commanded erase (GDPR Article 17); mem0 has no forget verb.
memory.forget()                          # wipe THIS user's memories (forget(user_id=…) for another)

# 4) GRAPH retrieval — go beyond vector similarity. (Search is ALWAYS hybrid
#    FTS5 + vector + MMR — there is no vector-only mode to opt into.)
memory.related(memory_id)                # knowledge-graph neighbors of a memory

# 5) RECENCY — tilt ranking toward recent facts (an m3-native .search() arg)
memory.search("preferences", recency_bias=0.3)
```

Every `.search()` result also carries m3's real signal in its metadata —
`confidence`, `valid_from` / `valid_to` — so even plain mem0-style searches
silently benefit from temporal + confidence ranking.

**Storage topology is yours to choose.** With mem0 and LangMem, conversation history
and long-term memory are fixed, separate layers. m3 lets you run them as **one
unified store, two independent stores, or two stores searched together** — set by
configuration (the chatlog DB path), not code. `M3ChatMessageHistory` (short-term
turns) and `Memory`/`M3Store` (long-term facts) route through m3's topology
resolver automatically, so the same code works whether they share a DB or not.

### Escape hatch — reach *any* m3 tool
For power users: `.call()` dispatches any m3 catalog tool by name (typed methods
cover the 95% case; this is the other 5%).
```python
memory.call("memory_search_routed", query="...", k=5)
memory.call("chatlog_status")
```

> **Note — destructive tools via `.call()` are gated.** Unlike the typed methods
> (`.delete()`, `.forget()`, `.clear()`), which run your explicit request directly and
> are **not** gated, `.call()` goes through m3's guarded dispatcher — the same one the
> MCP/LLM surface uses. So a destructive call by raw name (e.g.
> `.call("memory_delete", id=…)`) returns `{"ok": false, "error": "destructive_gated"}`
> unless `MCP_PROXY_ALLOW_DESTRUCTIVE=1` is set in the environment. This is deliberate:
> the guard exists to stop an agent/LLM from deleting by surprise through the generic
> tool surface. **For deletes, prefer the typed `.delete()` / `.forget()` / `.clear()`
> methods** — they express explicit user intent and need no flag.

---

## Feature comparison

| | mem0 | LangMem | **m3** |
|---|---|---|---|
| Local-first, no server / no API key | partial | ✗ (needs store infra) | **✓** |
| Contradiction handling (supersession) | ✗ | ✗ | **✓** |
| Temporal / bitemporal `as_of` queries | ✗ | ✗ | **✓** |
| Commanded forgetting (GDPR) | ✗ | ✗ | **✓** |
| Hybrid (FTS5+vector+MMR) + graph retrieval | vector | vector | **✓** |
| Async fact extraction on write | ✓ | ✓ | **✓** |
| Storage topology: unify / separate / search-together, by config | ✗ | ✗ | **✓** |
| One-line drop-in for mem0 | — | ✗ | **✓** |
| Backs LangMem / any LangGraph `BaseStore` | ✗ | native | **✓** |

---

## Notes & caveats
- **Runnable examples:** see [`examples/langchain-agent/`](../../examples/langchain-agent/)
  — `mem0_migration.py`, `native_store.py`, `langmem_on_m3.py`,
  `history_and_retriever.py`, plus a `perf_baseline.py` and its committed numbers.
- **Versions:** the mem0-compat and LangMem surfaces are tested against **pinned**
  langchain-core / langgraph versions (see the `[langchain]` extra); other versions
  may drift. The mem0 mirror tracks mem0's OSS `.add`/`.search`/`.get_all`/`.delete`
  shapes — m3 never imports mem0.
- **Read-your-writes** is via m3's FTS index (a query sharing words with the stored
  text matches immediately); purely SEMANTIC matches sharpen a beat later as the
  async vector backfill completes. `get_all()` is deterministic and always current.
- **"Replacing LangMem"** means backing it with m3, not deleting it — your code still
  calls LangMem's functions, now stored in m3. mem0 is a true delete-and-replace.
- **Per-user isolation:** m3 enforces tenancy at the SQL layer, so a `user_id` is
  mandatory on every call — there is no anonymous or global mode, and omitting it
  raises rather than silently sharing memory across users.
