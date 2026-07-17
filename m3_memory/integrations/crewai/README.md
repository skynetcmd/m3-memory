# m3-memory ↔ CrewAI

Give your CrewAI crew **persistent, local-first, cross-agent memory** — a drop-in
[`StorageBackend`](https://docs.crewai.com/en/concepts/memory) for CrewAI's unified
memory system (v1.10+).

```bash
pip install "m3-memory[crewai]"
```

```python
from crewai import Crew
from crewai.memory import Memory
from m3_memory.crewai import M3StorageBackend

crew = Crew(
    agents=[...],
    tasks=[...],
    memory=Memory(storage=M3StorageBackend(user_id="crew-alpha")),
)
```

That's the whole wire-up. `user_id` is **required** — m3 enforces per-tenant
isolation (there is no anonymous/global mode); use one backend per crew/tenant.

## Why m3 for CrewAI (what a single-vector store can't do)

Most backends (LanceDB, Qdrant, mem0) keep a CrewAI memory visible **only inside
that crew**. With m3, a memory your CrewAI crew learns is — if you want it —
**also searchable by your other agents**: Claude Code, Gemini CLI, a LangChain
app, all sharing one m3 store. A fact learned in a crew is instantly available to
your coding agents, and vice-versa. One memory, every agent.

This works because m3 keeps CrewAI's own vector *and* a local m3 vector for the
same memory (on by default; set `dual_embed=False` to keep memories CrewAI-only).
A single-vector store can't offer this — the reach is m3's, not extra work you
configure.

You also get m3's memory *dynamics* for free, surfacing through CrewAI's own
ranking and recall:

- **Contradiction-aware supersession** — `update()` records a real supersession
  edge (bi-temporal), not a flat overwrite.
- **Recency that actually refreshes** — m3 bumps `last_accessed` on every recall
  (via `touch_records`), so frequently-used memories rise over time.
- **Commanded forgetting + bitemporal history** — GDPR-grade delete; the past
  stays queryable `as_of` a point in time.
- **Fully local & offline** — m3's vector is a local bge-m3 embedding, no cloud
  call. (CrewAI's *own* embedder defaults to OpenAI; point CrewAI's `embedder=`
  at a local model — e.g. Ollama — if you want the CrewAI side offline too.)

## Options

```python
M3StorageBackend(
    user_id="crew-alpha",   # required — the tenant key (§ per-tenant isolation)
    dual_embed=True,        # default: make these memories searchable by your other
                            # m3 agents too. Set False to keep them CrewAI-only.
    call_timeout=30.0,
)
```

## Requirements

- **CrewAI ≥ 1.10** (the unified-memory `StorageBackend` protocol shipped in
  v1.10, Feb 2026; v1.0 GA predates it). Older versions fail loud with an upgrade
  hint. This adapter targets CrewAI **v1.x** only.
- **Python ≥ 3.10 and < 3.14 (default path).** This is a CrewAI constraint
  (every CrewAI 1.x release, through 1.15.4, declares `>=3.10,<3.14`), not m3's —
  a plain `pip install m3-memory[crewai]` can only resolve on a supported
  interpreter. m3 itself runs on 3.14; the simplest path is a 3.10–3.13
  environment for the crew that talks to it.
- No mem0 dependency — m3 satisfies CrewAI's contract natively.

### Python 3.14 escape hatch (unofficial — verified 2026-07-17)

CrewAI's `<3.14` cap is a **transitive dependency lag, not a code
incompatibility.** CrewAI 1.x is a pure-Python wheel; the actual blocker is that
it pins `chromadb~=1.1.0`, and `chromadb 1.1.x` imports `pydantic.v1`, which
raises on Python 3.14 (`Core Pydantic V1 functionality isn't compatible with
Python 3.14 or greater`). A newer chromadb (≥ 1.5) dropped that dependency.

If you must run the crew on 3.14, you can force it — at your own risk:

```bash
pip install --ignore-requires-python "crewai>=1.15,<2"
pip install --ignore-requires-python "chromadb>=1.5"   # overrides crewai's ~=1.1.0 pin
```

Verified 2026-07-17 on **Python 3.14.6** against **crewai 1.15.4** + **chromadb
1.5.9**: `import crewai` succeeds, m3's conformance suite passes 6/6
(`isinstance(M3StorageBackend(...), StorageBackend)` + field contracts), and a
real `save()` → `search()` round-trip returns the correct record ranked first.

Caveats: (1) the `chromadb>=1.5` bump **violates CrewAI's own `chromadb~=1.1.0`
pin** — m3's save/search path was verified, but CrewAI's *other* Chroma-backed
subsystems (e.g. its built-in short-term memory) were not exercised and may
misbehave under the override. (2) This is **not tested or supported by CrewAI**;
`--ignore-requires-python` bypasses their guard deliberately. Prefer the 3.10–3.13
path for production until CrewAI lifts the cap upstream (which only needs the
chromadb pin bumped).

## How it maps

| CrewAI | m3 |
|---|---|
| `save(records)` | `memory_write` per record (kept searchable by your other agents when enabled) + async Observer extraction |
| `search(query_embedding, …)` | `vector_search` against the CrewAI-space vectors (m3 never re-embeds — CrewAI supplies the query vector) |
| `update(record)` | `memory_supersede` (contradiction-aware edge) |
| `delete(…, older_than=…)` | bi-temporal soft-delete / `gdpr_forget` |
| `scope_prefix` (`/crew/research/…`) | a scoped sub-path within the tenant (prefix-matched) |
| `touch_records(ids)` | bumps `last_accessed` — feeds recency ranking |

See the repo's [`docs/EXTENDING.md`](../../../docs/EXTENDING.md) for the general
framework-adapter recipe this follows.
