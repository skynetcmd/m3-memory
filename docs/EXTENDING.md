# Extending M3 Memory

M3 has **two orthogonal extension seams**. Most of the codebase — the `*_impl`
business logic (write, search, entity resolution, GDPR, …) — is *shared* between
them and is single-sourced: you extend at a seam, you do not fork the core.

| You want to add… | Seam | Shape | Template |
|---|---|---|---|
| A new **SQL DB backend** | Storage seam | One `<name>_backend.py` implementing ~15 varying methods | [`bin/memory/backends/sqlite_backend.py`](../bin/memory/backends/sqlite_backend.py) |
| A new **agent framework** (LlamaIndex, AutoGen, …) | Framework seam | A thin adapter over `_dispatch_one` + a `mapping.py` | [`m3_memory/integrations/langchain/`](../m3_memory/integrations/langchain/) |

These are independent: a new backend works under every framework, and a new
framework works over every backend, because both meet the same shared `*_impl`
core. Do not move `*_impl` logic into a backend or a framework adapter — that
breaks the other seam. (Design authority: [DESIGN_PHILOSOPHIES.md](DESIGN_PHILOSOPHIES.md) §1, §2.)

---

## Recipe 1 — Add a DB backend

The storage seam is **SQL / DB-API only** and deliberately narrow. It targets
relational engines reached through a `connection().execute(sql, params)` surface.
A document store (MongoDB) does **not** fit and must not be forced into it — that
is the fat-abstraction failure mode the seam exists to avoid.

Adding a backend is **one self-contained file**. Nothing in the shared modules
changes — the backend registers itself and the readers discover it.

1. **Create `bin/memory/backends/<name>_backend.py`.** Copy `sqlite_backend.py`
   as the template. It contains three things:

   - **A `Dialect` subclass** (co-located in the same file). Override only the
     *divergent* SQL fragments — the base `Dialect` in
     [`dialect.py`](../bin/memory/backends/dialect.py) leaves each one abstract
     (raising `NotImplementedError`) so a forgotten override fails loud rather
     than inheriting another backend's SQL. The divergent surface is ~13 methods:
     placeholders/params, `insert_or_ignore` + `on_conflict_*`, `now` /
     `now_minus_days`, `returning_id_clause` / `last_insert_id`, the JSON
     extracts, `ci_equals`, the temporal-open clauses, and the `table_exists` /
     `columns_of` introspection probes.
   - **A `StorageBackend` class** — the `name`, `capabilities()`, `connection()`
     / `open_readonly()`, `ensure_schema()` / `schema_version()`, and the two
     search methods `keyword_search` + `vector_search`. Both search methods MUST
     return the seam-identical shape (`list[KeywordHit]` / `list[VectorHit]`) —
     an accelerator may change *speed*, never *result shape* (see
     [`base.py`](../bin/memory/backends/base.py)). The add-on-free baseline
     (native full-text + Rust cosine over packed embeddings) must always work
     with no extension installed — that is the universal floor.
   - **One registration line** — decorate the backend class:

     ```python
     from .registry import register_backend

     MYDB = MyDbDialect()

     @register_backend("mydb", dialect=MYDB)
     class MyDbBackend:
         name = "mydb"
         ...
     ```

2. **Add the name to the allow-list.** `BackendName` in `base.py` and `_VALID`
   in `selector.py` are the authoritative, fail-loud set of *selectable*
   backends. A registered name that isn't allow-listed still raises when
   selected — registration does not widen the allow-list.

3. **That's it.** `active_backend()` / `dialect()` / `dialect_for()` read the
   registry, so no `if name == …` ladder anywhere needs editing. The behavioral
   conformance test ([`tests/test_backend_conformance.py`](../tests/test_backend_conformance.py))
   is registry-driven — it discovers your backend automatically and asserts it
   satisfies the Protocol, overrides every divergent dialect method, and retrieves
   a written row via both keyword and vector search on the CPU-only floor.

**Accelerators are opt-in, behind a probe.** `vector_search` dispatches on a
per-connection capability probe with a single baseline arm today; a native ANN
index (pgvector HNSW, sqlite-vec) is a *new arm in your backend file*, not a
change to the seam signature.

---

## Recipe 2 — Add an agent framework

An agent framework (LangChain is the shipped example) is a **thin adapter** — it
does not reimplement memory logic. It translates the framework's calls into
`m3` tool dispatch and maps the structured rows back into the framework's shapes.

1. **Dispatch through `_dispatch_one`.** Every m3 tool is reachable through
   [`bin/catalog/dispatch.py`](../bin/catalog/dispatch.py)'s `_dispatch_one`
   (the same entry the MCP server uses). Your adapter calls it with a tool name
   and args and gets the structured result back — you never touch the DB or the
   `*_impl` functions directly.

2. **Write a `mapping.py`.** The one framework-specific file: it converts m3's
   structured rows (memory items, search hits, history) into the framework's
   objects (e.g. LangChain `Document` / `BaseMessage`) and back. Keep ALL
   framework-shape knowledge here so the rest of the adapter stays generic.

3. **Follow the LangChain layout.** [`m3_memory/integrations/langchain/`](../m3_memory/integrations/langchain/)
   is ~350 lines and is the template: `m3client.py` (dispatch), `mapping.py`
   (row↔shape), and the framework-facing surfaces (`retriever.py`, `history.py`,
   `store.py`, `checkpoint.py`). Model your adapter on it.

Because the adapter only speaks tool-dispatch, it works over **every** storage
backend with no per-backend code.

All three shipped adapters reuse the framework-agnostic **`M3Client`** dispatch core (`m3_memory/integrations/langchain/m3client.py`) — it owns the shared event-loop thread and the `_dispatch_one` bridge, so a new adapter never re-implements dispatch.

**Three shipped examples of this recipe:**

- **LangChain / LangGraph** — [`m3_memory/integrations/langchain/`](../m3_memory/integrations/langchain/). Shadows mem0's `Memory` API + native `BaseStore`/checkpointer surfaces.
- **CrewAI (v1.10+)** — [`m3_memory/integrations/crewai/`](../m3_memory/integrations/crewai/). Implements CrewAI's `StorageBackend` protocol. A good template when the framework defines its *own* backend interface (rather than you subclassing its classes): `backend.py` implements the protocol, `mapping.py` converts `MemoryRecord ⇄ m3 row`, and the same `M3Client` dispatch core is reused. It also shows how a memory written through one framework can stay searchable by every other m3 agent — by keeping the framework's own vector *plus* m3's native vector under a second `embed_model` identity on the same item. That cross-framework reach is only possible because m3's `memory_embeddings` table is multi-identity (see Recipe 1); a single-vector store can't do it.
- **PydanticAI** — [`m3_memory/integrations/pydantic_ai/`](../m3_memory/integrations/pydantic_ai/). The template when a framework has **no memory interface at all** (PydanticAI ships none). Two tiers: Tier 1 is deps-injected tools + a history-processor (`register_m3_tools(agent)` adds `remember`/`recall`/`forget`; `m3_recall_processor()` auto-injects recalled memories); Tier 2 is `M3MemoryToolset`, which subclasses PydanticAI's concrete `FunctionToolset` so `isinstance(ts, AbstractToolset)` is `True` — a formal, attachable toolset without hand-rolling `ToolsetTool` objects. Since PydanticAI hands the adapter *text* (not a vector, unlike CrewAI), recall uses the standard search path — no dual-vector handling. Runs on Python 3.14 (no interpreter cap).

Points to watch when adapting a new framework:
- **Tenancy (§7):** if the framework's contract has no `user_id` (CrewAI's doesn't), take the tenant at *construction* and stamp it on every call — raise if absent, never fall back to a global scope.
- **Version-guard + lazy import (§3):** import the framework lazily and fail loud with an install/upgrade hint if it's missing or too old (see the crewai package `__init__.py`).
- **Implement only what's called; stub the rest cheaply.** Verify against the framework's real call sites which methods are hot vs. never-invoked, rather than implementing an entire protocol blind.

---

*See [DESIGN_PHILOSOPHIES.md](DESIGN_PHILOSOPHIES.md) for the tenets these seams
uphold. Out of scope today (documented future paths, not built): a fully
per-backend `keyword_search`/`vector_search`, and a pgvector ANN accelerator.*
