# m3-memory ↔ PydanticAI

Give a [PydanticAI](https://ai.pydantic.dev) agent **persistent, local-first,
cross-agent memory** — backed by m3.

```bash
pip install "m3-memory[pydantic-ai]"
```

PydanticAI ships **no built-in persistent memory** (each run starts fresh). This
adapter adds it two ways — pick whichever fits.

## Tier 1 — tools + auto-recall (the quick path)

```python
from pydantic_ai import Agent
from m3_memory.pydantic_ai import M3Deps, register_m3_tools, m3_recall_processor

agent = Agent(
    "anthropic:claude-sonnet-5",
    deps_type=M3Deps,
    history_processors=[m3_recall_processor()],   # optional: auto-inject recalled memories
)
register_m3_tools(agent)                            # adds remember / recall / forget tools

agent.run_sync("remember I prefer dark roast", deps=M3Deps(user_id="alice"))
```

- **`register_m3_tools(agent)`** attaches three tools the model can call:
  `remember(content, importance)`, `recall(query, limit)`, `forget(memory_id)`.
- **`m3_recall_processor(k=5)`** is a
  [history processor](https://ai.pydantic.dev): on each turn it searches m3 for
  the latest user message and prepends the most relevant memories as context —
  automatic recall with no glue code. Bounded (latest turn only, top-`k`), and
  never raises into the run.

## Tier 2 — a first-class toolset (formal conformance)

`M3MemoryToolset` subclasses PydanticAI's concrete `FunctionToolset`, so it **is**
a PydanticAI `AbstractToolset` — attach it like any native toolset, compose it
(`.prefixed()`, `.filtered()`), introspect it:

```python
from pydantic_ai import Agent
from m3_memory.pydantic_ai import M3Deps, M3MemoryToolset

agent = Agent(
    "anthropic:claude-sonnet-5",
    deps_type=M3Deps,
    toolsets=[M3MemoryToolset()],
)
agent.run_sync("remember I like dark roast", deps=M3Deps(user_id="alice"))
```

`isinstance(M3MemoryToolset(), AbstractToolset)` is `True`.

## `M3Deps` — the injected memory service

`user_id` is **required** — m3 enforces per-tenant isolation (there is no
anonymous/global mode). Pass one `M3Deps` per user/session:

```python
M3Deps(
    user_id="alice",     # required — the tenant key
    scope="agent",       # where this agent's memories live (default)
    call_timeout=30.0,
)
```

## Why m3 for PydanticAI

- **Cross-agent memory.** A memory your PydanticAI agent writes is immediately
  searchable by every other m3 agent (Claude Code, a CrewAI crew, a LangChain
  app) sharing the store — and vice-versa. One store, every agent.
- **Real memory dynamics** for free: contradiction-aware supersession, recency
  that refreshes on recall, bitemporal history, commanded forgetting (GDPR),
  hybrid FTS+vector+MMR retrieval — all local, no server, no API key.
- **Backend-agnostic.** The adapter only speaks m3's tool dispatch, so it works
  over SQLite, PostgreSQL, and (future) MariaDB with no per-backend code.

## Requirements

- **pydantic-ai ≥ 2.0, < 3** (either `pydantic-ai` or the lighter
  `pydantic-ai-slim`). Older versions fail loud with an upgrade hint.
- **No Python cap.** Unlike the CrewAI adapter, PydanticAI is built on Pydantic v2
  (no chromadb / pydantic-v1), so it installs and runs on **Python 3.14** with a
  normal `pip install` — same interpreter m3 itself runs on.

> **Verified 2026-07-17 on Python 3.14.6** against **pydantic-ai-slim 2.12.0**:
> `import` succeeds with a plain `pip install` (no override), `isinstance(
> M3MemoryToolset(), AbstractToolset)` holds, all three tools register, an `Agent`
> built with the toolset runs every tool via `TestModel`, and a `remember()` →
> `recall()` round-trip returns the written memory.

## How it maps

| PydanticAI | m3 |
|---|---|
| `remember` tool / `M3Deps.remember` | `memory_write` (auto-classified, tenant-stamped) |
| `recall` tool / `M3Deps.recall` | `memory_search_scored` (hybrid FTS+vector+MMR; m3 embeds the text query) |
| `forget` tool / `M3Deps.forget` | `memory_delete_bulk` (bi-temporal soft-delete) |
| `m3_recall_processor` | `memory_search_scored` on the latest user turn → prepended context |
| `deps=M3Deps(user_id=…)` | per-tenant isolation (§7) |

See the repo's [`docs/EXTENDING.md`](../../../docs/EXTENDING.md) for the general
framework-adapter recipe this follows (Recipe 2).
