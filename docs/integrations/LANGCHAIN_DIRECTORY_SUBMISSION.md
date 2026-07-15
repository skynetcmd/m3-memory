# LangChain integration-directory submission — m3-memory

Everything needed to register **m3-memory** in the official LangChain integration
directory. LangChain does **not** accept new integrations as PRs into its own
repos — packages are published independently to PyPI, then registered via a small
PR to the langchain monorepo. m3-memory is already on PyPI
([`m3-memory`](https://pypi.org/project/m3-memory/)), so this is the registration
step only.

Sources for the process:
[Providers overview](https://docs.langchain.com/oss/python/integrations/providers/overview) ·
[Contributing guide](https://docs.langchain.com/oss/python/contributing) ·
[Publishing an integration](https://python.langchain.com/docs/contributing/how_to/integrations/publish/)

---

## 1. Registration entry — append to `libs/packages.yml`

In a fork of [`langchain-ai/langchain`](https://github.com/langchain-ai/langchain),
add this to the **end** of `libs/packages.yml`:

```yaml
  - name: m3-memory
    repo: skynetcmd/m3-memory
    path: .
    provider_page: m3_memory
```

Notes:
- The registered name is **`m3-memory`** (the canonical package). LangChain
  permits non-`langchain-*` names (e.g. `databricks-langchain`); m3's LangChain
  surface ships inside the main package under the `[langchain]` extra rather than
  a separate distribution, because that surface is a thin in-process façade over
  the local m3 engine — splitting the code would only add a redundant package.
- `path: .` — the package is at the repo root.
- `provider_page` points at the provider doc added in step 2.

### Discoverable alias — `m3-langchain` (optional, recommended)

For the conventional, `databricks-langchain`-style discoverable name, a thin
alias package lives at [`packages/m3-langchain/`](../../packages/m3-langchain).
It ships **one module** that re-exports `m3_memory.langchain` and declares a
single dependency, `m3-memory[langchain]==<same version>` — so `pip install
m3-langchain` gives the full integration under the recognizable name, with no
code duplication and no version skew.

Publish it alongside a release (built + `twine check`-verified):
```bash
cd packages/m3-langchain && python -m build && twine upload dist/*
```
The directory registration still points at `m3-memory` (the canonical provider);
`m3-langchain` is purely for PyPI name-discoverability.

---

## 2. Provider page — `docs/docs/integrations/providers/m3_memory.mdx`

Add this page to the langchain monorepo (the directory the docs site renders
from). Keep it factual — every capability below maps to a shipped class.

````markdown
# m3-memory

[m3-memory](https://github.com/skynetcmd/m3-memory) is a local-first, MCP-native
memory layer with hybrid retrieval (SQLite FTS5 + BGE-M3 vector + MMR),
bitemporal history, deterministic contradiction supersession, and GDPR
forget/export — all offline, no server or API key. It exposes five standard
LangChain surfaces plus LCEL-native components, and can also back LangMem.

## Installation

```bash
pip install "m3-memory[langchain]"
```

m3 self-configures on first use (in-process embedder; the SQLite DB is created
and migrated automatically). No external service to run.

## Memory / store (drop-in Mem0 replacement, and LangGraph `BaseStore`)

```python
from m3_memory.langchain import Memory, M3Store

# mem0-compatible surface — a one-line import swap from `from mem0 import Memory`
mem = Memory(user_id="alex")
mem.add([{"role": "user", "content": "I prefer dark mode."}])
mem.search("appearance preferences", limit=3)

# LangGraph BaseStore — also backs LangMem (pass store=M3Store())
store = M3Store()
```

## Chat message history

```python
from m3_memory.langchain import M3ChatMessageHistory
history = M3ChatMessageHistory("session-1", user_id="alex")
```

## Retriever (RAG)

```python
from m3_memory.langchain import M3Retriever
retriever = M3Retriever(user_id="alex", k=4)
docs = retriever.invoke("deadline")  # Documents carry score, confidence, valid_from/to
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

Contradiction supersession (`.supersede`), temporal queries (`as_of=`),
commanded forgetting (`.forget`), and hybrid + knowledge-graph retrieval
(`.related`) — first-class methods on the same objects, all local.

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
| `with_m3_history` / `with_m3_memory` | *(helpers)* | one-line wrappers |

Compatibility: `langchain-core>=0.3.0,<1`, `langgraph>=0.2.0,<1` (verified against
langgraph 0.6.x / langgraph-checkpoint 3.x).

---

## 4. Suggested PR

- **Target:** `langchain-ai/langchain`, files `libs/packages.yml` +
  `docs/docs/integrations/providers/m3_memory.mdx` only (per the contributing
  guide, a registration PR touches nothing else).
- **Title:** `docs: add m3-memory integration provider`
- **Body:**

  > Registers [m3-memory](https://pypi.org/project/m3-memory/) — a local-first,
  > MCP-native memory layer — as an integration provider. It ships five standard
  > LangChain surfaces (`Memory` mem0 drop-in, `M3Store` `BaseStore`, `M3Saver`
  > `BaseCheckpointSaver`, `M3ChatMessageHistory`, `M3Retriever`) plus LCEL
  > components, installable via `pip install "m3-memory[langchain]"`. Already
  > published to PyPI; this PR adds the `packages.yml` entry and provider page
  > only. Apache-2.0.

---

## Pre-submit checklist

- [x] Package published to PyPI (`m3-memory`, latest release carries the
      `[langchain]` extra and all surfaces)
- [x] Public repo with the integration guide + runnable examples
      (`examples/langchain-agent/`)
- [x] Apache-2.0 licensed
- [ ] Fork `langchain-ai/langchain`, add the two files above, open the PR
- [ ] (Optional, strengthens the listing) Add a runnable example notebook under
      `docs/docs/integrations/` per the contributing guide — LangChain's docs are
      generated from notebooks, so an `.ipynb` renders as an example page
