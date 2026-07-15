# LangChain integration-directory submission — m3-memory

Everything needed to register **m3-memory** in the LangChain integration
directory. m3-memory is already on PyPI
([`m3-memory`](https://pypi.org/project/m3-memory/)), so this is the registration
step only.

> **Verified against the live LangChain docs repo (2026-07).** Registration moved
> out of the `langchain-ai/langchain` monorepo into the dedicated
> **[`langchain-ai/docs`](https://github.com/langchain-ai/docs)** repo (default
> branch `main`). The registry is **`packages.yml`** at that repo's root, and
> provider pages live under **`src/oss/integrations/providers/`**. (Older guides
> pointing at `langchain-ai/langchain` + `libs/packages.yml` are stale.)

Sources:
[Providers overview](https://docs.langchain.com/oss/python/integrations/providers/overview) ·
[langchain-ai/docs packages.yml](https://github.com/langchain-ai/docs/blob/main/packages.yml) ·
example PRs [#559](https://github.com/langchain-ai/docs/pull/559),
[#30573](https://github.com/langchain-ai/langchain/pull/30573)

---

## 1. Registration entry — append to `packages.yml` (in `langchain-ai/docs`)

Fork [`langchain-ai/docs`](https://github.com/langchain-ai/docs) and add this
under `packages:` in the root **`packages.yml`**:

```yaml
- name: m3-memory
  name_title: m3-memory
  repo: skynetcmd/m3-memory
  path: .
  provider_page: m3_memory
  js: n/a
```

Field notes (per the schema documented at the top of `packages.yml`):
- **`name: m3-memory`** — the published PyPI package. The directory's download
  automation (pepy.tech) queries this name, so it must be a live package;
  `m3-memory` is published, so use it (not the unpublished `m3-langchain` alias).
- **`name_title`** — display name; set explicitly since the default strips a
  `langchain-`/`-langchain` affix that `m3-memory` doesn't have.
- **`path: .`** — package at repo root.
- **`provider_page: m3_memory`** — points at the page added in step 2 (its
  `name_short` would otherwise be `m3-memory`; naming the page keeps it clean).
- **`js: n/a`** — Python-specific; no JS package.
- Do **not** set `highlight`, `downloads`, or `downloads_updated_at` — those are
  maintainer-only / auto-generated.

---

## 2. Provider page — `src/oss/integrations/providers/m3_memory.mdx`

Add this to the `langchain-ai/docs` fork. Match the style of existing pages
(e.g. `src/oss/integrations/providers/cala.mdx`): an intro blockquote, install,
then per-surface sections.

````markdown
# m3-memory

>[m3-memory](https://github.com/skynetcmd/m3-memory) is a local-first, MCP-native
>memory layer with hybrid retrieval (SQLite FTS5 + BGE-M3 vector + MMR),
>bitemporal history, deterministic contradiction supersession, and GDPR
>forget/export — all offline, no server or API key. It ships five standard
>LangChain surfaces plus LCEL-native components, and can also back LangMem.

## Installation

```bash
pip install "m3-memory[langchain]"
```

m3 self-configures on first use (in-process embedder; SQLite created and migrated
automatically). No external service to run.

## Memory / store (drop-in Mem0 replacement, and LangGraph `BaseStore`)

```python
from m3_memory.langchain import Memory, M3Store

mem = Memory(user_id="alex")                 # one-line swap from `from mem0 import Memory`
mem.add([{"role": "user", "content": "I prefer dark mode."}])
mem.search("appearance preferences", limit=3)

store = M3Store()                            # LangGraph BaseStore; also backs LangMem
```

## Chat message history

```python
from m3_memory.langchain import M3ChatMessageHistory
history = M3ChatMessageHistory("session-1", user_id="alex")
```

## Retriever (RAG)

```python
from m3_memory.langchain import M3Retriever
retriever = M3Retriever(user_id="alex", k=4)   # Documents carry score, confidence, valid_from/to
```

## Checkpointer (pause / resume / time-travel)

```python
from m3_memory.langchain import M3Saver
graph = builder.compile(checkpointer=M3Saver())
```

## LCEL-native components

```python
from m3_memory.langchain import MemoryRetrieve, MemoryWrite
chain = MemoryRetrieve(user_id="alex") | prompt | llm | MemoryWrite(user_id="alex")
```

## What it adds over plain LangChain memory

Contradiction supersession (`.supersede`), temporal queries (`as_of=`), commanded
forgetting (`.forget`), and hybrid + knowledge-graph retrieval (`.related`) —
first-class methods on the same objects, all local.

Full guide: [docs/integrations/LANGCHAIN.md](https://github.com/skynetcmd/m3-memory/blob/main/docs/integrations/LANGCHAIN.md).
````

---

## 3. Class → base-class reference (verified against the shipped package)

| m3 surface | LangChain / LangGraph base class | Job |
|---|---|---|
| `Memory` / `M3Memory` / `MemoryClient` | *(mem0 API shape — no base class)* | drop-in Mem0 replacement |
| `M3Store` | `langgraph.store.base.BaseStore` | long-term memory; backs LangMem |
| `M3Saver` | `langgraph.checkpoint.base.BaseCheckpointSaver` | run persistence / time-travel |
| `M3ChatMessageHistory` | `langchain_core.chat_history.BaseChatMessageHistory` | short-term chat history |
| `M3Retriever` | `langchain_core.retrievers.BaseRetriever` | RAG retrieval |
| `MemoryWrite` / `MemoryRetrieve` | `langchain_core.runnables.Runnable` | LCEL memory read/write |

Compatibility: `langchain-core>=0.3.0,<1`, `langgraph>=0.2.0,<1` (verified against
langgraph 0.6.x / langgraph-checkpoint 3.x).

---

## 4. The PR

- **Target repo:** `langchain-ai/docs` (NOT the `langchain` monorepo).
- **Files:** `packages.yml` + `src/oss/integrations/providers/m3_memory.mdx` only.
- **Title:** `docs: add m3-memory integration provider`
- **Body:**

  > Registers [m3-memory](https://pypi.org/project/m3-memory/) — a local-first,
  > MCP-native memory layer — as an integration provider. It ships five standard
  > LangChain surfaces (`Memory` mem0 drop-in, `M3Store` `BaseStore`, `M3Saver`
  > `BaseCheckpointSaver`, `M3ChatMessageHistory`, `M3Retriever`) plus LCEL
  > components, installable via `pip install "m3-memory[langchain]"`. Already on
  > PyPI; this PR adds the `packages.yml` entry and provider page only.
  > Apache-2.0.

---

## Pre-submit checklist

- [x] `m3-memory` published to PyPI (the entry's download automation needs a live package)
- [x] Public repo with integration guide + runnable examples (`examples/langchain-agent/`)
- [x] Apache-2.0
- [x] Registration mechanism verified against the current `langchain-ai/docs` repo
- [ ] Fork `langchain-ai/docs`, add the two files, open the PR
- [ ] (Optional) Publish `m3-langchain` alias to PyPI for the discoverable name
      — separate from this directory entry, which uses `m3-memory`
