# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory — Comparison Guide

> Last updated: July 2026. Corrections welcome via [issue](https://github.com/skynetcmd/m3-memory/issues).

Several tools address agentic memory. This document explains where M3 Memory fits relative to each, and when a different tool is the better choice.

> 💡 Looking for a head-to-head against other **sovereign / local-first memory substrates** (agentmemory, Chronos, Hindsight, Mastra OM, Memento, MemPalace)? See the [Sovereign Memory Systems comparison table](M3_Comparison_Table.md) (also available as an [interactive version](https://html-preview.github.io/?url=https://github.com/skynetcmd/m3-memory/blob/main/docs/M3_Comparison_Table.html) with sticky columns and tooltip glossary) — different cohort, different decision.

> 📊 **Retrieval accuracy (the metric that isolates the memory layer).** M3's **v3 core engine** reaches **99.2% retrieval session-hit-rate @ k=10 (496/500) and 100% @ k=20** on [LongMemEval-S](https://github.com/xiaowu0162/LongMemEval) — raw turns, hybrid FTS5 + BGE-M3 vector + MMR, no knowledge graph, no oracle metadata. SHR (session hit-rate) **is** retrieval accuracy: it measures whether the correct evidence session is surfaced, with no answer model involved — the like-for-like, retrieval-only metric memory systems publish as their headline. Separately, the same v3 config scores **92.0% end-to-end QA accuracy** (460/500, no oracle metadata) — a different, answer-model-dependent metric. Receipts, per-category breakdown, and full methodology: the [LME-S Benchmarking Report](../benchmarks/longmemeval/LME-S_Benchmarking_Report.md).

---

## 🧭 Where the cognition lives

The cleanest way to compare agentic memory tools is to ask **where in your stack does cognition belong**. Different products answer this differently:

- **Memory layer owns cognition** — the memory tool itself runs LLM-driven extraction, builds belief states, infers temporal relationships, and decides what to update. Mem0 takes this approach. The benefit: less to build. The cost: opinionated, harder to swap parts, every retrieval implicitly involves an LLM call.
- **Memory layer is infrastructure; cognition lives in the agent** — the memory tool gives you durable storage, deterministic retrieval, and graph primitives. Anything cognitive (extraction, conflict resolution, belief updates) is a separable layer you compose, swap, or skip. M3 takes this approach.

M3 is unusual in that **it ships both modes**. Out of the box, M3 includes a local-SLM extraction pipeline (`m3_enrich`), a reflector for conflict resolution (`run_reflector`), bitemporal valid-time / transaction-time, supersedes relationships, and 3-hop graph traversal. You can run M3 with all of that on, or run M3 as raw substrate and bring your own extraction stack. **The choice is yours, not the tool's.**

This composability is the actual differentiator — not "M3 has no cognition" (it does) and not "Mem0 is opinionated" (that's a feature for some teams). The split that matters: do you want cognition welded to the memory layer, or factored as an exchangeable component?

| Concern | M3 (composable) | Mem0 (welded-in) |
|---|---|---|
| Run as deterministic substrate, no LLM in retrieval path | ✅ Disable enrichment, use deterministic CRUD + graph walks | ❌ LLM is in the loop by design |
| Use M3's built-in local SLM extraction | ✅ `m3_enrich --profile enrich_local_qwen` (or Anthropic/Gemini) | n/a — Mem0 ships its own |
| Bring your own extraction pipeline | ✅ Ignore `m3_enrich`, write entities directly via MCP | ⚖️ Possible but cuts against the grain |
| Multiple agents writing simultaneously | ✅ SQLite WAL, atomic | ⚖️ Cloud version handles via API queueing; local less emphasized |
| Memory works fully offline / air-gapped | ✅ No external dependency in any mode | 🔻 Cloud version requires internet; self-host possible but not the happy path |

If "the LLM should decide what's worth remembering" matches your worldview, Mem0's tighter integration is a genuine win. If "extraction policy should be inspectable, swappable, and testable independent of storage" matches yours, M3's split is the right shape.

---

> **Legend:** 🏆 = the system has this capability and does it well · 👑 = best-in-class here — either a rare stand-out few offer (e.g. FIPS-ready crypto, bundled in-process embedder) or a shared capability M3 does better (e.g. deterministic contradiction supersession, native MCP, drop-in LangChain). Where a competitor also has a feature it earns 🏆; M3's 👑 marks where it leads. (Temporal/bitemporal is a genuine tie with graph-native systems like Zep/Graphiti — both earn 🏆; M3's edge there is doing it local-first with no graph DB to run.) Applies to every table below.

> **Auto-generated wiki — a note that applies across the field.** M3 *projects* its
> memory store into a browsable, human-readable **knowledge base**: `m3 wiki generate`
> compiles canonical memories + indexed files into an interlinked Markdown vault
> (GitHub-renderable, a self-contained offline HTML viewer, or an **Obsidian vault**
> with `--obsidian` for graph view + backlinks; also in the web dashboard). This is a
> different direction of data flow than the ecosystem norm (verified 2026-07-21):
> Mem0's export is [structured JSON](https://docs.mem0.ai/cookbooks/essentials/exporting-memories)
> for migration/compliance, not a readable wiki; [Letta's Obsidian plugin](https://github.com/letta-ai/letta-obsidian)
> reads an *existing* vault *into* the agent (Obsidian → Letta), the reverse of
> generating one *from* memory. Tools that treat markdown files *as* the store (e.g.
> Basic Memory) are the closest analog, but that's a different model — m3 keeps its
> hybrid-search engine and generates the vault as a downstream, disposable projection.
> Weigh this as an interop/ownership feature, not a retrieval-quality one.

---

## ⚔️ M3-Memory vs Mem0

Mem0 is a popular agentic memory library with broad ecosystem adoption. M3-Memory offers a **superset of Mem0's capabilities** and ships a drop-in Mem0-compatible surface (`from m3_memory.langchain import Memory` — a one-line import swap), so LangChain/LangGraph users get everything Mem0 does plus contradiction supersession, bitemporal history, commanded forgetting, and hybrid+graph retrieval — locally, with no server or API key. M3 also backs **CrewAI** (native `StorageBackend`) and **PydanticAI** (drop-in tools + a formal `M3MemoryToolset`) from the same store — so one local memory serves LangChain, CrewAI, and PydanticAI agents at once. And it serves developers using **desktop coding agents** (Claude Code, Cursor, Cline, Gemini CLI, Aider) who need memory that is private, offline-capable, and speaks MCP natively.

| Feature | M3-Memory | Mem0 |
|---------|-----------|------|
| **Primary deployment** | 👑 Local SQLite — works fully offline, zero data egress | Cloud API (self-host is possible but not the happy path) |
| **MCP support** | 👑 Native — 100+ tools, zero config in Claude Code / Cursor / Cline / Gemini CLI | No native MCP; requires a custom wrapper |
| **Search algorithm** | FTS5 (BM25) + vector cosine + MMR diversity re-ranking | Vector search + knowledge graph traversal |
| **Contradiction handling** | 👑 Automatic heuristic detection on write (cosine + title) **plus** a deterministic explicit `memory_supersede` — old memory soft-deleted, `supersedes` edge recorded, history preserved | Basic deduplication; no strong conflict resolution |
| **Bitemporal history** | 👑 `valid_from` / `valid_to` on every memory — query state as of any past date | No |
| **Auto-generated wiki** | 👑 `m3 wiki generate` compiles your memories into a browsable, interlinked Markdown vault (renders on GitHub, an offline HTML viewer, or as an **Obsidian vault** with `--obsidian` for graph view + backlinks) — a *projection* of the store, pruned on regen | No |
| **GDPR tooling** | 👑 `gdpr_forget` (Art. 17 hard delete) + `gdpr_export` (Art. 20 portable JSON) as MCP tools | Manual; no dedicated GDPR tooling |
| **Embeddings** | 👑 **Bundled in-process embedder** — BGE-M3 ships with M3 (GGUF, installed by `m3 setup`); no separate model server, no Ollama/LM Studio/vLLM required. Optional GPU or external endpoint if you want them | Cloud embedding APIs by default |
| **Setup** | 👑 One-command auto-configuring wizard (`m3 setup`) — detects agents, wires config + hooks, installs the embedder, runs a `doctor` verify | Manual SDK wiring / cloud dashboard config |
| **API keys required** | None | Yes (cloud version) |
| **Offline operation** | Full — SQLite + bundled embedder, no external services | No (cloud version) |
| **FIPS 140-3** | 👑 **Deployment-ready** crypto boundary (AES-256-GCM vault, PBKDF2-HMAC-SHA256, TLS 1.3 FIPS ciphersuites); point it at the CMVP-validated wolfSSL FIPS module for a validated deployment. Note: the validation belongs to that module — M3 is not itself a CMVP-validated cryptographic module (no application is) | No |
| **Storage backend** | 👑 SQLite (default, zero-infra) **or PostgreSQL as a first-class primary store** (`M3_DB_BACKEND=postgres`) for shared/high-concurrency deployments — same semantics on either | Single managed cloud store |
| **Cross-device sync** | Optionally sync/federate a SQLite deployment to a PostgreSQL warehouse tier, bi-directional delta sync | Managed by Mem0 cloud |
| **Storage topology** | 🏆 Chat-log and curated memory run as **one unified store, two independent stores, or two stores searched together** (`memory_search_multi_db`) — your choice by config, no rework | Single managed store |
| **Knowledge graph** | Yes — 9 relationship types, 3-hop traversal | Yes — strong point |
| **Multi-agent concurrent writes** | WAL mode + 30s busy_timeout + retry — concurrent writers serialize and wait, they don't fail; SQL-layer scope isolation keeps agents' private notes private; optional shared **PostgreSQL** pool for high-concurrency fleets (no single-writer limit) | Cloud version handles via API queueing; multi-writer correctness in self-host is not emphasized |
| **Cognition placement** | Composable — disable, replace, or use built-in SLM extraction | LLM-driven extraction is welded into the memory layer |
| **Multi-tenant** | Per-agent scoping (`agent_id`, `user_id`, `scope`) | Yes — production-grade |
| **LangChain integration** | 👑 **Drop-in replacement** — shadows Mem0's `Memory`/`MemoryClient` API; migrate with a one-line import swap. Plus native `M3Store` (LangGraph `BaseStore`), `M3Saver` (LangGraph checkpointer — pause/resume/time-travel), and full 100+ MCP tool access from any LangChain agent | 🏆 Native library |
| **CrewAI integration** | 👑 Native `StorageBackend` (CrewAI v1.10+): `Memory(storage=M3StorageBackend(...))`. A CrewAI memory can **also be searchable by every other m3 agent** (Claude Code, Gemini, LangChain) if you want — a shared cross-framework memory a single-vector store can't provide | 🏆 Native provider (CrewAI-only silo) |
| **PydanticAI integration** | 👑 Native — two tiers: drop-in tools + auto-recall (`register_m3_tools`, `m3_recall_processor`) **and** a formal `M3MemoryToolset` (a real PydanticAI `AbstractToolset`); `pip install m3-memory[pydantic-ai]`, runs on Python 3.14 | ❌ None |
| **Feature coverage** | **Superset of Mem0** — everything Mem0 does (`.add()`/`.search()`) plus contradiction supersession, bitemporal `as_of`, commanded forgetting, hybrid+graph retrieval | Baseline |
| **Cost** | Free, Apache 2.0 licensed | Free tier + $249/mo Pro |
| **Stars** | Newer project (fewer stars); 2,501-test codebase with SOTA local-first retrieval (99.2% SHR@10) | 20k+ (mindshare leader) |

### When to choose M3-Memory over Mem0

- You use Claude Code, Cursor, Cline, Gemini CLI, Aider, or any MCP-compatible agent
- Your data cannot leave your machine (enterprise, regulated industries, personal privacy)
- You need agents that stay factually consistent (contradiction detection matters)
- You want compliance tooling (GDPR forget/export) without building it yourself
- You're on a budget — no API costs, no subscriptions, runs on consumer hardware
- You need offline operation (no internet at the coffee shop, air-gapped environments)

### When to choose Mem0 over M3-Memory

- You need managed multi-tenant **cloud** memory with a hosted dashboard and don't want to run anything yourself
- You specifically want Mem0's SaaS platform (billing, org management, hosted UI)

> **Building on LangChain / LangGraph?** You no longer have to choose Mem0 for that reason. M3 is a **drop-in Mem0 replacement** (one-line import swap), is **compatible with LangMem** (pass `store=M3Store()`), and exposes M3's full **100+ MCP tool** surface to LangChain agents — while adding contradiction handling, temporal queries, and forgetting that Mem0 doesn't offer. See [`docs/integrations/LANGCHAIN.md`](integrations/LANGCHAIN.md).

---

## ⚔️ M3-Memory vs Letta

Letta (formerly MemGPT) is a **full stateful agent runtime** with memory built in — not just a memory layer. It uses hierarchical memory blocks (core / recall / archival) that the agent itself edits via tool calls. Letta Code adds git-backed agent state. It's a powerful platform for long-lived, self-improving agents.

M3-Memory is a **dedicated, lightweight memory layer** — a drop-in backend for agents you already have. It does one thing: give your agent persistent, private, consistent memory via 100+ MCP tools.

| Feature | M3-Memory | Letta |
|---------|-----------|-------|
| **Type** | Dedicated memory layer | Full agent runtime + memory |
| **Adoption** | Drop-in — one line in mcp.json | Full runtime adoption required |
| **MCP support** | 👑 Native — 100+ tools, zero config | Custom SDKs / REST API |
| **Memory model** | Semantic store + configurable knowledge graph (off-switchable; swappable entity-vocab via `M3_ENTITY_VOCAB_YAML`) | Tiered blocks (core / recall / archival) |
| **Search** | FTS5 + vector + MMR | Tiered recall with embeddings |
| **Contradiction handling** | 👑 Automatic heuristic detection + deterministic explicit supersede (bitemporal, auditable) | 🏆 Agent-driven — the agent must decide to update its own memory |
| **GDPR tooling** | 👑 Built-in `gdpr_forget` + `gdpr_export` | Not built-in |
| **Bitemporal history** | 👑 Yes — query state as of any past date | No |
| **Deployment** | 👑 100% local (SQLite) by default, bundled embedder — no external services | 🏆 Self-hosted or Letta Cloud |
| **FIPS 140-3** | 👑 Deployment-ready crypto boundary (validation belongs to the wolfSSL CMVP module, not M3 itself) | No |
| **Works with existing agents** | Yes — any MCP agent unchanged | No — must rebuild on Letta runtime |
| **Long-lived self-improving agents** | Supported | Core strength |
| **Git-backed agent state** | No | Yes (Letta Code) |
| **Cost** | Free, Apache 2.0 | OSS + Letta Cloud SaaS |

### When to choose M3-Memory over Letta

- You use Claude Code, Cursor, Cline, Gemini CLI, Aider, or any existing MCP agent and want to add memory **without rewriting your stack**
- You need **automatic** contradiction resolution, not agent-driven memory management
- You need GDPR forget/export as compliance tooling
- You want 100% local, offline-capable memory with no cloud dependency
- You're adding memory to an existing agent, not building a new one from scratch

### When to choose Letta over M3-Memory

- You're building a **new long-lived autonomous agent** from the ground up and want the runtime + memory in one package
- You want the agent to actively manage and rewrite its own memory blocks (agent self-improvement loops)
- Git-backed agent state (Letta Code) is important to your workflow
- You want a full stateful agent platform, not just a memory backend

### Can you use both?

Yes. Letta agents can call external MCP tools. You could run M3-Memory as the persistent memory backend for a Letta agent, using M3's hybrid search and GDPR tools while keeping Letta's runtime for agent orchestration.

---

## ⚔️ M3-Memory vs Zep

Zep focuses on temporal knowledge graphs for enterprise multi-agent systems. It has strong temporal reasoning capabilities, but requires more infrastructure than M3.

| Feature | M3-Memory | Zep |
|---------|-----------|-----|
| **Search** | FTS5 + vector + MMR | Vector + temporal knowledge graph |
| **Temporal model** | 🏆 Bitemporal (valid time + transaction time), item-grain — **local-first, no graph DB to run** | 🏆 Bitemporal at fact/edge grain in a temporal KG (requires Neo4j/FalkorDB) |
| **GDPR tooling** | 👑 Built-in MCP tools | 🏆 Partial |
| **MCP support** | 👑 Native — 100+ tools | No |
| **Deployment** | 👑 Local SQLite + bundled embedder — no external services | 🏆 Self-hosted or Zep Cloud |
| **FIPS 140-3** | 👑 Deployment-ready crypto boundary (validation belongs to the wolfSSL CMVP module, not M3 itself) | No |
| **Cost** | Free, OSS | OSS + SaaS |

---

## ⚔️ M3-Memory vs Graphiti

Graphiti (by the Zep team) is a framework for building **temporally-aware knowledge graphs** for agents. Its core abstraction is the graph: entities and relationships as first-class nodes/edges, with bi-temporal edge validity, typically backed by Neo4j (or FalkorDB). It's a strong fit when your problem is fundamentally *relational* — reasoning over how entities connect and how those connections change over time.

M3 is memory-first rather than graph-first: the primary store is a bitemporal SQLite knowledge base with hybrid retrieval, and an entity graph is *one* layer on top (`memory_graph`, entity extraction) rather than the central abstraction. Where Graphiti asks "what's the graph?", M3 asks "what should the agent remember, and is it still true?"

| Feature | M3-Memory | Graphiti |
|---------|-----------|----------|
| **Core abstraction** | Bitemporal memory store + hybrid retrieval | Temporal knowledge graph (nodes/edges) |
| **Backing store** | Single SQLite file (FTS5 + vector), or PostgreSQL as the primary backend | Graph DB (Neo4j / FalkorDB) |
| **Search** | FTS5 + vector + MMR | Graph traversal + semantic + BM25 |
| **Temporal model** | 🏆 Bitemporal (valid + transaction time), item-grain | 🏆 Bi-temporal edge validity (fact/edge grain) |
| **MCP support** | 👑 Native — 100+ tools | 🏆 Via a separate MCP server |
| **Infrastructure** | 👑 None to start (SQLite + bundled embedder) | Requires a graph database |
| **Local-first / offline** | 👑 100% — SQLite, fully offline, no external services | Depends on graph-DB deployment |
| **FIPS 140-3** | 👑 Deployment-ready crypto boundary (validation belongs to the wolfSSL CMVP module, not M3 itself) | No |
| **Cost** | Free, Apache 2.0 | Free, OSS |

### When to choose M3-Memory over Graphiti
- You want zero-infrastructure local memory (one SQLite file, no graph DB to run — or PostgreSQL as the primary backend if you need a shared/server store).
- Retrieval — "recall what's relevant and still valid" — matters more than graph reasoning.
- You need MCP-native tools and offline operation out of the box.

### When to choose Graphiti over M3-Memory
- Your problem is genuinely graph-shaped: multi-hop entity relationship reasoning is the point, not a side feature.
- You already run (or want) a graph database and want the graph as the primary substrate.

### Can you use both?
Yes — they operate at different altitudes. You can let M3 own memory/retrieval and offload deep relationship reasoning to a Graphiti graph if a workload needs it.

---

## ⚔️ M3-Memory vs A-MEM

A-MEM is a **research-oriented agentic memory** design: memories are "notes" that the system links into an evolving network (inspired by Zettelkasten), with the LLM generating structured attributes and dynamically updating links as new memories arrive. It's a compelling model for emergent, self-organizing memory and is primarily a research codebase rather than a production deployment target.

M3 is production-and-operations oriented: typed memories, bitemporal supersession, explicit GDPR/FIPS posture, an operational MCP tool surface, and a benchmarked retrieval stack. The *supersede operation* itself is deterministic and auditable (soft-delete + `supersedes` edge, not an LLM re-linking pass); automatic *detection* of which prior memory to supersede is a cosine+title heuristic (or you target it explicitly with `memory_supersede`).

| Feature | M3-Memory | A-MEM |
|---------|-----------|-------|
| **Orientation** | Production / operations | Research prototype |
| **Memory structure** | Typed items + entity graph layer | LLM-generated notes + evolving link network |
| **Contradiction handling** | 👑 Deterministic *explicit* supersede + heuristic auto-detect — bitemporal, auditable | 🏆 LLM-driven link/attribute updates |
| **Retrieval** | FTS5 + vector + MMR (benchmarked) | Embedding-based over the note network |
| **MCP / agent integration** | 👑 Native — 100+ tools, plugin, hooks | Library / research code |
| **Compliance tooling** | 👑 GDPR primitives + FIPS 140-3 deployment-ready posture (validation belongs to the wolfSSL CMVP module, not M3 itself) | Not a focus |
| **Local-first / offline** | 👑 100% — SQLite + bundled embedder, fully offline | Depends on the LLM/embeddings used |

### When to choose M3-Memory over A-MEM
- You need something to deploy and operate today — with MCP integration, compliance tooling, and predictable behavior.
- You want deterministic, auditable contradiction handling rather than emergent LLM re-linking.

### When to choose A-MEM over M3-Memory
- You're researching self-organizing / emergent memory structures and want the note-network model as the object of study.
- Deterministic operations and compliance posture are not your priority.

---

## ⚔️ M3-Memory vs LangChain Memory / LangMem

LangChain Memory (including LangGraph's thread/store memory and the newer LangMem library) is memory that lives inside the LangChain ecosystem. It covers short-term thread memory, long-term JSON stores, and LangMem's episodic/semantic/procedural memory types. It's the natural choice if you're already building LangGraph agents.

M3-Memory is framework-agnostic and MCP-native — it works with any agent via a single config line. It is also **compatible with LangMem**: `M3Store` implements LangGraph's `BaseStore`, so LangMem's tools and background manager run on M3 unchanged (`store=M3Store()`) — persisted locally with contradiction, temporal, and graph features underneath.

**For LangChain users, M3 is a superset.** You keep everything LangChain Memory / LangMem gives you — thread memory, the `BaseStore`, LangMem's episodic/semantic/procedural tools — and gain what they don't: automatic contradiction supersession, bitemporal `as_of` queries, commanded forgetting (GDPR), hybrid FTS5+vector+MMR retrieval, a bundled in-process embedder, and M3's full 100+ MCP tool surface exposed to your agent — all local-first, no external store to provision. Nothing is given up; capabilities are added.

| Feature | M3-Memory | LangChain Memory / LangMem |
|---------|-----------|---------------------------|
| **Ecosystem** | Any MCP agent **and** LangChain/LangGraph (backs LangMem via `M3Store`) | LangChain / LangGraph only |
| **Drop-in surfaces** | 👑 All five standard slots: mem0-compatible `Memory`, LangGraph `M3Store` (`BaseStore`), `M3Saver` (`BaseCheckpointSaver` — pause/resume/time-travel), `M3ChatMessageHistory` (short-term), `M3Retriever` (RAG) | Native (its own classes) |
| **MCP support** | 👑 Native — 100+ tools, also exposed to LangChain agents | No |
| **Memory types** | 30+ types + auto-classification | Thread, store, episodic, semantic, procedural |
| **Procedural memory** | 👑 First-class `procedure` type (skill/runbook/how_to/checklist) **auto-distilled from successful task runs**, with `distills_from` provenance and a procedural retrieval boost | 🏆 `procedural` type, manually authored/updated |
| **Storage topology** | 🏆 Short-term chat-log and long-term memory can be **unified, kept separate, or searched together** by config — retrieve conversation turns and curated facts independently or in one merged query | Thread memory + store are distinct layers, not user-configurable as one |
| **Contradiction handling** | 👑 Automatic heuristic detect + deterministic explicit supersede (bitemporal) | 🏆 Manual / LLM-driven via procedural memory |
| **GDPR tooling** | 👑 Built-in `gdpr_forget` + `gdpr_export` | Custom implementation required |
| **Search** | FTS5 + vector + MMR | Depends on configured backend store |
| **Local-first** | 👑 100% — SQLite + bundled embedder, fully offline | 🏆 Good — depends on backend store choice |
| **Embeddings** | 👑 Bundled in-process (BGE-M3) — no separate model server | Configured externally (needs an embedder) |
| **FIPS 140-3** | 👑 Deployment-ready crypto boundary (validation belongs to the wolfSSL CMVP module, not M3 itself) | No |
| **Installation** | 👑 `pip install m3-memory` + one-command auto-configuring wizard (`m3 setup`) | Part of LangChain / LangGraph install |
| **Overhead** | Very light | Medium (tied to LangGraph runtime) |
| **Cost** | Free, Apache 2.0 | Free, MIT |

### When to choose M3-Memory over LangChain Memory

- You use Claude Code, Cursor, Cline, Gemini CLI, Aider, or any non-LangChain MCP agent
- You need a single memory backend that works across multiple agent frameworks
- You want automatic contradiction detection without writing custom procedural memory logic
- GDPR compliance tooling is a requirement

### When to choose LangChain Memory / LangMem

- You want to keep using LangMem's tools and taxonomy directly — in which case **back them with M3** (`store=M3Store()`) to gain local-first storage, contradiction handling, and temporal queries without changing your LangMem code
- You prefer everything in one unified LangChain install and don't need M3's extra capabilities

> **Note:** choosing LangMem and choosing M3 are not mutually exclusive — M3 implements the `BaseStore` LangMem runs on. See [`docs/integrations/LANGCHAIN.md`](integrations/LANGCHAIN.md).

---

## 🔭 Not yet independently evaluated

The agentic-memory space moves fast and this page only compares systems we've
actually examined against primary sources. If you'd like a head-to-head with a
system not listed here, open an [issue](https://github.com/skynetcmd/m3-memory/issues) —
we'll evaluate it and add an honest section rather than publish a table built on
marketing copy. See [Verifying claims](#-verifying-claims-about-m3-or-any-tool-here)
for how we hold every entry (including M3's own) to source-of-truth.

---

## 📋 Summary Decision Matrix

| I need... | Best choice |
|-----------|-------------|
| Memory for Claude Code / Gemini CLI / Aider | **M3-Memory** |
| Zero cloud, fully offline, private | **M3-Memory** |
| Automatic contradiction detection | **M3-Memory** |
| GDPR forget + export as MCP tools | **M3-Memory** |
| Drop-in memory for an existing MCP agent | **M3-Memory** |
| LangChain / LangGraph memory (drop-in Mem0 replacement, or back LangMem) | **M3-Memory** |
| Full 100+ MCP tool set available inside a LangChain agent | **M3-Memory** |
| Managed cloud, multi-tenant, hosted dashboard (SaaS) | **Mem0** |
| CrewAI memory backend (v1.10+ `StorageBackend`), local-first | **M3-Memory** |
| CrewAI memory that's *also* searchable by your other agents (Claude Code, LangChain) | **M3-Memory** |
| PydanticAI agent memory (drop-in tools + auto-recall, or a formal `AbstractToolset`) | **M3-Memory** |
| One memory store backing LangChain, CrewAI, *and* PydanticAI at once | **M3-Memory** |
| PostgreSQL as a first-class primary backend, local-first (not a managed cloud DB) | **M3-Memory** |
| Long-lived autonomous agents that self-edit memory | **Letta** |
| Full stateful agent runtime (not just memory) | **Letta** |
| Git-backed agent state | **Letta** |
| Dedicated graph-database backend (Neo4j / FalkorDB) at massive scale | **Zep** |
| Graph-first entity/relationship reasoning on a graph DB | **Graphiti** |
| Bitemporal memory + temporal queries, local-first, no graph DB to run | **M3-Memory** |
| Short-term + long-term memory unified, separate, or searched together by config | **M3-Memory** |
| Researching self-organizing / emergent memory structures | **A-MEM** |

---

## 🎯 Where M3 Memory fits

M3 Memory targets a specific intersection that other tools don't fully cover:

- **MCP-native** — works with Claude Code, Cursor, Cline, Gemini CLI, Aider, and any MCP agent without integration code
- **Local-first** — all data stays on your machine; fully offline-capable
- **Automatic consistency** — contradiction detection handles conflicting facts without agent-side logic
- **Compliant** — GDPR forget and export are built-in MCP tools
- **Drop-in** — one config line to add memory to an existing agent; no runtime migration

If you need a full agent runtime (Letta), a dedicated graph-database backend at massive scale (Zep/Graphiti), or a managed cloud SaaS (Mem0 Pro), those tools are better suited. For LangChain / LangGraph, M3 is a drop-in Mem0 replacement and backs LangMem — no need to look elsewhere. And for persistent, private, local-first memory for any MCP agent, M3 is built for exactly that.

---

## 🔍 Verifying claims (about M3 or any tool here)

Third-party AI assistants — Copilot, Gemini, ChatGPT, others — sometimes describe memory tools with features they don't have or scores they haven't earned. This isn't malicious; pattern-matching on a project name is what assistants do when they don't have ground truth.

For M3 specifically, the source of truth is this repo. If you've seen a feature attributed to M3 elsewhere that we don't document here, in [`README.md`](../README.md), or in [`docs/MYTHS_AND_FACTS.md`](MYTHS_AND_FACTS.md), assume it's hallucinated until verified against the source.

Same skepticism should apply to claims about Mem0, Letta, Zep, LangChain Memory, or any other tool in this guide. We've tried to cite each tool's own docs and benchmarks where possible. Where we couldn't, we've said so. If you find a comparison row in this document that misrepresents another tool, [open an issue](https://github.com/skynetcmd/m3-memory/issues) — accuracy matters more than position, and we'll fix it.
