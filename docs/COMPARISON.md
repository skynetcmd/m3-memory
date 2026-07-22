<a id="top"></a>

# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory — Comparison Guide

> Last updated: July 2026. Corrections welcome via [issue](https://github.com/skynetcmd/m3-memory/issues).

Several tools address agentic memory. This document explains where M3 Memory fits relative to each, and when a different tool is the better choice.

> 📊 **Retrieval accuracy (the metric that isolates the memory layer).** M3's **v3 core engine** reaches **99.2% retrieval session-hit-rate @ k=10 (496/500) and 100% @ k=20** on [LongMemEval-S](https://github.com/xiaowu0162/LongMemEval) — raw turns, hybrid FTS5 + BGE-M3 vector + MMR, no knowledge graph, no oracle metadata. SHR (session hit-rate) **is** retrieval accuracy: it measures whether the correct evidence session is surfaced, with no answer model involved — the like-for-like, retrieval-only metric memory systems publish as their headline. Separately, the same v3 config scores **92.0% end-to-end QA accuracy** (460/500, no oracle metadata) — a different, answer-model-dependent metric. Receipts, per-category breakdown, and full methodology: the [LME-S Benchmarking Report](../benchmarks/longmemeval/LME-S_Benchmarking_Report.md).

### M3 vs Other Memory Systems

<blockquote>
<table>
<tr>
<td><a href="#vs-a-mem">A-MEM</a></td>
<td><a href="#vs-agentmemory">agentmemory</a></td>
<td><a href="#vs-chronos">Chronos</a></td>
</tr>
<tr>
<td><a href="#vs-graphiti">Graphiti</a></td>
<td><a href="#vs-hindsight">Hindsight</a></td>
<td><a href="#vs-langmem">LangChain Memory / LangMem</a></td>
</tr>
<tr>
<td><a href="#vs-letta">Letta</a></td>
<td><a href="#vs-mastra-om">Mastra OM</a></td>
<td><a href="#vs-mem0">Mem0</a></td>
</tr>
<tr>
<td><a href="#vs-memento">Memento</a></td>
<td><a href="#vs-mempalace">MemPalace</a></td>
<td><a href="#vs-zep">Zep</a></td>
</tr>
</table>
</blockquote>

> 💡 For benchmark sourcing and judge-provenance behind the LongMemEval figures, see the [Sovereign Memory Systems benchmark reference](M3_Comparison_Table.md).

---

## 🧭 Where the cognition lives

The cleanest way to compare agentic memory tools is to ask **where in your stack does cognition belong**. Different products answer this differently:

- **Memory layer owns cognition** — the memory tool itself runs LLM-driven extraction, builds belief states, infers temporal relationships, and decides what to update. Mem0 takes this approach. The benefit: less to build. The cost: opinionated, harder to swap parts, every retrieval implicitly involves an LLM call.
- **Memory layer is infrastructure; cognition lives in the agent** — the memory tool gives you durable storage, deterministic retrieval, and graph primitives. Anything cognitive (extraction, conflict resolution, belief updates) is a separable layer you compose, swap, or skip. M3 takes this approach.

M3 is unusual in that **it ships both modes**. Out of the box, M3 includes a local-SLM extraction pipeline (`m3_enrich`), a reflector for conflict resolution (`run_reflector`), bitemporal valid-time / transaction-time, supersedes relationships, and 3-hop graph traversal. You can run M3 with all of that on, or run M3 as raw substrate and bring your own extraction stack. **The choice is yours, not the tool's.**

This composability is the actual differentiator — not "M3 has no cognition" (it does) and not that welded-in designs are wrong (that's a feature for some teams). The split that matters: do you want cognition welded to the memory layer, or factored as an exchangeable component?

Roughly where each system in this guide sits:

| Cognition placement | Systems | What it means for you |
|---|---|---|
| **Welded into the memory layer** | [Mem0](#vs-mem0), [Mastra OM](#vs-mastra-om), [A-MEM](#vs-a-mem) | The tool decides what's worth remembering. Less to build; an LLM is implicitly in the loop, and extraction policy isn't separately swappable. |
| **Owned by the agent runtime** | [Letta](#vs-letta) | The agent edits its own memory blocks via tool calls. Powerful for self-improving agents; requires adopting the runtime. |
| **Composable — cognition is a separable layer** | **M3**, [agentmemory](#vs-agentmemory), [Memento](#vs-memento) | Storage and retrieval are deterministic; extraction is a component you enable, replace, or skip. |
| **Substrate only — bring your own cognition** | [Graphiti](#vs-graphiti), [Zep](#vs-zep), [LangMem](#vs-langmem) | You get graph/store primitives and supply the cognitive layer yourself. |

If "the LLM should decide what's worth remembering" matches your worldview, a tighter-integrated tool is a genuine win. If "extraction policy should be inspectable, swappable, and testable independent of storage" matches yours, M3's split is the right shape.

> The head-to-head detail behind this axis — deterministic mode, bring-your-own extraction, built-in SLM — lives in the [M3 vs Mem0](#vs-mem0) table below, alongside every other Mem0 comparison.

---

> **Legend:** 🏆 = the system has this capability and does it well · 👑 = best-in-class here — either a rare stand-out few offer (e.g. FIPS-ready crypto, bundled in-process embedder) or a shared capability M3 does better (e.g. deterministic contradiction supersession, native MCP, drop-in LangChain) · ⚖️ = has the capability but with a caveat that makes it hard to compare (e.g. a benchmark graded by a self-authored or unpublished judge) · 🛠️ = has it, but with a notable limitation or setup burden (needs infra, undocumented, immature) · ❌ = does not have it. Where a competitor also has a feature it earns 🏆; M3's 👑 marks where it leads. A benchmark score earns 🏆 only when it is a real, comparable number (standard setting, published/strict judge) — a figure from a loosened or unpublished judge gets ⚖️, not 🏆, no matter how high. (Temporal/bitemporal is a genuine tie with graph-native systems like Zep/Graphiti — both earn 🏆; M3's edge there is doing it local-first with no graph DB to run.) Applies to every table below.

> **📎 Benchmark sourcing.** A LongMemEval-S QA score is comparable only when
> graded by the same judge, so each figure below carries its provenance. m3,
> agentmemory, and Mastra OM grade with the **unmodified upstream judge** and are
> mutually comparable; the others are not. Full analysis:
> [Sovereign Memory Systems benchmark reference](M3_Comparison_Table.md).
>
> ᵃ **M3** — 92.0% QA (no oracle; SHR=100% @ k=20), unmodified upstream LongMemEval judge.
> ᵇ **Mem0** — ~94% self-reported (94.4% [research page](https://mem0.ai/research), 94.8% [repo](https://github.com/mem0ai/mem0)); independent/older evaluations put earlier Mem0 at ~67% ([arXiv 2504.19413](https://arxiv.org/abs/2504.19413)). Judge is **modified and more lenient** (single unified prompt: "judge by MEANING, not exact words", explicit pro-yes bias, superset answers accepted), so it is **not** comparable to strict-judge numbers. Answer model undisclosed as of 2026-06-22.
> ᶜ **agentmemory** — 96.2% QA (481/500), Claude Opus 4.6 answerer, GPT-4o judge; judge is upstream Wu exact (5/6 templates byte-identical, temporal template only *adds* a stricter `Reference Date:` line). The 96.2% is driven by answerer-side prompt tuning, not a loosened judge. Both numbers are answer-model-dependent. Source: [github.com/JordanMcCann/agentmemory](https://github.com/JordanMcCann/agentmemory). *Verified 2026-06-22.*
> ᵈ **Chronos** — 95.6% QA (self-reported, arXiv preprint [2603.16862](https://arxiv.org/abs/2603.16862), not peer-reviewed). The paper says it implements "LongMemEval's LLM judge" but shows no prompt text, names no judge model, and releases no code (it even flags "LLM-as-judge variability"). *Figure verified; judge unconfirmed 2026-06-23.*
> ᵉ **Hindsight** — 91.4% QA, Gemini 3 Pro backbone. The public [hindsight-benchmarks](https://github.com/vectorize-io/hindsight-benchmarks) repo ships LongMemEval *results* but no LongMemEval judge code (the only judge it ships is a lenient LoCoMo one). *Figure verified; judge unconfirmed 2026-06-23.*
> ᶠ **Mastra OM** — 94.9% QA (94.87%), gpt-5-mini answerer, GPT-4o judge; eval code carries "copied EXACTLY from the official LongMemEval benchmark … Do not modify these prompts" — the six templates match verbatim. Source: [mastra.ai/research/observational-memory](https://mastra.ai/research/observational-memory). *Verified 2026-06-22.*
> ᵍ **Memento** — 90.8% QA in the **oracle / evidence-only (no-distractor)** setting, *not* standard LongMemEval-S; the harder S-setting is unpublished. Judge is modified/more-lenient (rewritten prompts adding "minor phrasing differences are acceptable", "off-by-one errors are acceptable"). Not apples-to-apples with strict-judge S-setting numbers. Source: [github.com/shane-farkas/memento-memory](https://github.com/shane-farkas/memento-memory). *Partially verified — setting differs, judge loosened, 2026-06-23.*
> ʰ **MemPalace** — 96.6% is **R@5 recall, not QA accuracy** (different metric). An [independent analysis](https://arxiv.org/abs/2604.21284) attributes it to ChromaDB, not the spatial architecture, and shows the AAAK compression mode is lossy (96.6%→84.2%). Scam allegations ([repo issue #618](https://github.com/MemPalace/mempalace/issues/618)) and malware-impostor domains documented. *Listed to flag only.*
>
> ᵛ **FIPS 140-3.** M3 ships a deployment-ready crypto boundary (AES-256-GCM vault, PBKDF2-HMAC-SHA256, TLS 1.3 FIPS ciphersuites); point it at the CMVP-validated wolfSSL FIPS module for a validated deployment. The validation belongs to that module — M3 is not itself a CMVP-validated cryptographic module (no application is). No other system in this guide documents a FIPS posture.

---

<a id="vs-mem0"></a>

## ⚔️ M3-Memory vs Mem0

Mem0 is a popular agentic memory library with broad ecosystem adoption. M3-Memory offers a **superset of Mem0's capabilities** and ships a drop-in Mem0-compatible surface (`from m3_memory.langchain import Memory` — a one-line import swap), so LangChain/LangGraph users get everything Mem0 does plus contradiction supersession, bitemporal history, commanded forgetting, and hybrid+graph retrieval — locally, with no server or API key. M3 also backs **CrewAI** (native `StorageBackend`) and **PydanticAI** (drop-in tools + a formal `M3MemoryToolset`) from the same store — so one local memory serves LangChain, CrewAI, and PydanticAI agents at once. And it serves developers using **desktop coding agents** (Claude Code, Cursor, Cline, Gemini CLI, Aider) who need memory that is private, offline-capable, and speaks MCP natively.

| Feature | M3-Memory | Mem0 |
|---|---|---|
| **Category** | Dedicated memory layer (MCP-native) | Managed agentic-memory service + library |
| **Deployment** | 👑 100% local by default — SQLite + bundled BGE-M3 embedder, fully offline, zero data egress | 🏆 Cloud API, **or** self-hosted server (Docker Compose) / pip-npm library |
| **Infrastructure required** | 👑 None to start — no server, no graph DB, no model server | Managed cloud, or a self-hosted server stack (Docker Compose) |
| **Storage backend** | 🏆 SQLite (default) **or PostgreSQL** as a first-class primary (`M3_DB_BACKEND=postgres`) — same semantics on either | Managed cloud store; self-hosted uses a pluggable vector DB (e.g. Qdrant) |
| **Search / retrieval** | 🏆 3-pillar hybrid: FTS5 (BM25) + vector cosine + MMR diversity re-rank | Vector search + knowledge-graph traversal |
| **Temporal model** | 🏆 Bitemporal (valid + transaction time), item-grain — local-first, no graph DB to run | ⚖️ Time-aware retrieval ranks the right dated instance; no bitemporal as-of query model |
| **Contradiction handling** | 👑 Heuristic auto-detect on write **plus** deterministic explicit `memory_supersede` — soft-delete, `supersedes` edge, history preserved | Basic deduplication; no strong conflict resolution |
| **Published LongMemEval-S (QA)** | **92.0%** — standard S-setting, no oracle, unmodified upstream judgeᵃ (retrieval SHR 99.2%@10 / 100%@20) | ⚖️ ~94% self-reported, but graded with a **self-authored, more lenient judge** — not comparable to strict-judge numbersᵇ |
| **Knowledge graph** | 🏆 Automatic entity extraction (cognitive loop) + 9 relationship types; query-time entity-graph expansion feeds retrieval scoring (BFS to 3 hops), off-switchable | 🏆 Yes — a strong point |
| **Agent integration** | 👑 Native MCP (100+ tools) + LangChain/LangGraph, CrewAI, PydanticAI adapters | 🏆 Native LangChain/CrewAI libraries; no native MCP (needs a custom wrapper) |
| **Compliance tooling** | 👑 `gdpr_forget` (Art. 17) + `gdpr_export` (Art. 20) as MCP tools; FIPS 140-3 deployment-ready crypto boundaryᵛ | Manual; no dedicated GDPR tooling. No FIPS posture |
| **Auto-generated wiki / Obsidian export** | 👑 `m3 wiki generate` projects memories + files into an interlinked Markdown/Obsidian vault | [Structured JSON export](https://docs.mem0.ai/cookbooks/essentials/exporting-memories) for migration/compliance — not a human-readable wiki |
| **Cost & licence** | Free, Apache 2.0 | Apache 2.0 core; free tier + $249/mo Pro cloud |

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

<sub>[↑ Back to top](#top)</sub>

---

<a id="vs-letta"></a>

## ⚔️ M3-Memory vs Letta

Letta (formerly MemGPT) is a **full stateful agent runtime** with memory built in — not just a memory layer. It uses hierarchical memory blocks (core / recall / archival) that the agent itself edits via tool calls. Letta Code adds git-backed agent state. It's a powerful platform for long-lived, self-improving agents.

M3-Memory is a **dedicated, lightweight memory layer** — a drop-in backend for agents you already have. It does one thing: give your agent persistent, private, consistent memory via 100+ MCP tools.

| Feature | M3-Memory | Letta |
|---|---|---|
| **Category** | Dedicated memory layer (MCP-native) | 🏆 Full stateful agent runtime + memory |
| **Deployment** | 👑 100% local by default — SQLite + bundled BGE-M3 embedder, fully offline, zero data egress | 🏆 Self-hosted or Letta Cloud |
| **Infrastructure required** | 👑 None to start — no server, no graph DB, no model server | Letta runtime (must rebuild your agent on it) |
| **Storage backend** | 🏆 SQLite (default) **or PostgreSQL** as a first-class primary (`M3_DB_BACKEND=postgres`) — same semantics on either | Letta's own DB |
| **Search / retrieval** | 🏆 3-pillar hybrid: FTS5 (BM25) + vector cosine + MMR diversity re-rank | Tiered recall with embeddings (core / recall / archival blocks) |
| **Temporal model** | 🏆 Bitemporal (valid + transaction time), item-grain — local-first, no graph DB to run | ❌ No bitemporal / as-of queries |
| **Contradiction handling** | 👑 Heuristic auto-detect on write **plus** deterministic explicit `memory_supersede` — soft-delete, `supersedes` edge, history preserved | 🏆 Agent-driven — the agent decides to update its own memory blocks |
| **Published LongMemEval-S (QA)** | **92.0%** — standard S-setting, no oracle, unmodified upstream judgeᵃ (retrieval SHR 99.2%@10 / 100%@20) | Not published |
| **Knowledge graph** | 🏆 Automatic entity extraction (cognitive loop) + 9 relationship types; query-time entity-graph expansion feeds retrieval scoring (BFS to 3 hops), off-switchable | ❌ Tiered memory blocks rather than an entity graph |
| **Agent integration** | 👑 Native MCP (100+ tools) + LangChain/LangGraph, CrewAI, PydanticAI adapters | Custom SDKs / REST API; can call external MCP tools |
| **Compliance tooling** | 👑 `gdpr_forget` (Art. 17) + `gdpr_export` (Art. 20) as MCP tools; FIPS 140-3 deployment-ready crypto boundaryᵛ | Not built-in. No FIPS posture |
| **Auto-generated wiki / Obsidian export** | 👑 `m3 wiki generate` projects memories + files into an interlinked Markdown/Obsidian vault | Reverse direction — the [Letta Obsidian plugin](https://github.com/letta-ai/letta-obsidian) reads an *existing* vault in; memory stays in Letta's DB |
| **Cost & licence** | Free, Apache 2.0 | OSS + Letta Cloud SaaS |

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

<sub>[↑ Back to top](#top)</sub>

---

<a id="vs-zep"></a>

## ⚔️ M3-Memory vs Zep

Zep focuses on temporal knowledge graphs for enterprise multi-agent systems. It has strong temporal reasoning capabilities, but requires more infrastructure than M3.

| Feature | M3-Memory | Zep |
|---|---|---|
| **Category** | Dedicated memory layer (MCP-native) | Temporal knowledge-graph memory for enterprise fleets |
| **Deployment** | 👑 100% local by default — SQLite + bundled BGE-M3 embedder, fully offline, zero data egress | 🏆 Self-hosted or Zep Cloud |
| **Infrastructure required** | 👑 None to start — no server, no graph DB, no model server | 🛠️ Requires a graph DB (Neo4j / FalkorDB) |
| **Storage backend** | 🏆 SQLite (default) **or PostgreSQL** as a first-class primary (`M3_DB_BACKEND=postgres`) — same semantics on either | Graph database |
| **Search / retrieval** | 🏆 3-pillar hybrid: FTS5 (BM25) + vector cosine + MMR diversity re-rank | 🏆 Vector + temporal knowledge-graph traversal |
| **Temporal model** | 🏆 Bitemporal (valid + transaction time), item-grain — local-first, no graph DB to run | 🏆 Bitemporal at fact/edge grain in a temporal KG — finer grain than m3's item-grain |
| **Contradiction handling** | 👑 Heuristic auto-detect on write **plus** deterministic explicit `memory_supersede` — soft-delete, `supersedes` edge, history preserved | 🏆 Graph-level fact invalidation over time |
| **Published LongMemEval-S (QA)** | **92.0%** — standard S-setting, no oracle, unmodified upstream judgeᵃ (retrieval SHR 99.2%@10 / 100%@20) | Not published |
| **Knowledge graph** | 🏆 Automatic entity extraction (cognitive loop) + 9 relationship types; query-time entity-graph expansion feeds retrieval scoring (BFS to 3 hops), off-switchable | 🏆 Yes — the core abstraction |
| **Agent integration** | 👑 Native MCP (100+ tools) + LangChain/LangGraph, CrewAI, PydanticAI adapters | 🏆 Official Python/TypeScript/Go SDKs **and** a first-party [MCP server](https://github.com/getzep/zep) |
| **Compliance tooling** | 👑 `gdpr_forget` (Art. 17) + `gdpr_export` (Art. 20) as MCP tools; FIPS 140-3 deployment-ready crypto boundaryᵛ | 🏆 Partial GDPR support. No FIPS posture |
| **Auto-generated wiki / Obsidian export** | 👑 `m3 wiki generate` projects memories + files into an interlinked Markdown/Obsidian vault | Export API / DB dump; markdown↔graph only via a 3rd-party plugin (MegaMem) that *ingests* Obsidian |
| **Cost & licence** | Free, Apache 2.0 | OSS + SaaS |

### When to choose M3-Memory over Zep
- You want bitemporal memory without running a graph database — one SQLite file, no Neo4j/FalkorDB to operate.
- You need MCP-native tools and offline operation out of the box.
- GDPR forget/export as first-class tooling matters.

### When to choose Zep over M3-Memory
- You're running enterprise multi-agent systems at a scale where a dedicated graph DB is warranted.
- You want temporal reasoning at fact/edge grain in a knowledge graph, and already have (or want) the infrastructure for it.

<sub>[↑ Back to top](#top)</sub>

---

<a id="vs-graphiti"></a>

## ⚔️ M3-Memory vs Graphiti

Graphiti (by the Zep team) is a framework for building **temporally-aware knowledge graphs** for agents. Its core abstraction is the graph: entities and relationships as first-class nodes/edges, with bi-temporal edge validity, typically backed by Neo4j (or FalkorDB). It's a strong fit when your problem is fundamentally *relational* — reasoning over how entities connect and how those connections change over time.

M3 is memory-first rather than graph-first: the primary store is a bitemporal SQLite knowledge base with hybrid retrieval, and an entity graph is *one* layer on top (`memory_graph`, entity extraction) rather than the central abstraction. Where Graphiti asks "what's the graph?", M3 asks "what should the agent remember, and is it still true?"

| Feature | M3-Memory | Graphiti |
|---|---|---|
| **Category** | Dedicated memory layer (MCP-native) | Framework for temporally-aware knowledge graphs |
| **Deployment** | 👑 100% local by default — SQLite + bundled BGE-M3 embedder, fully offline, zero data egress | Depends on your graph-DB deployment |
| **Infrastructure required** | 👑 None to start — no server, no graph DB, no model server | 🛠️ Requires a graph database (Neo4j, FalkorDB, or Amazon Neptune) |
| **Storage backend** | 🏆 SQLite (default) **or PostgreSQL** as a first-class primary (`M3_DB_BACKEND=postgres`) — same semantics on either | Graph DB — Neo4j, FalkorDB, or Amazon Neptune (Kuzu deprecated) |
| **Search / retrieval** | 🏆 3-pillar hybrid: FTS5 (BM25) + vector cosine + MMR diversity re-rank | 🏆 Graph traversal + semantic + BM25 |
| **Temporal model** | 🏆 Bitemporal (valid + transaction time), item-grain — local-first, no graph DB to run | 🏆 Bi-temporal edge validity (fact/edge grain) |
| **Contradiction handling** | 👑 Heuristic auto-detect on write **plus** deterministic explicit `memory_supersede` — soft-delete, `supersedes` edge, history preserved | 🏆 Edge invalidation as facts change |
| **Published LongMemEval-S (QA)** | **92.0%** — standard S-setting, no oracle, unmodified upstream judgeᵃ (retrieval SHR 99.2%@10 / 100%@20) | Not published |
| **Knowledge graph** | 🏆 Automatic entity extraction (cognitive loop) + 9 relationship types; query-time entity-graph expansion feeds retrieval scoring (BFS to 3 hops), off-switchable | 🏆 Yes — it *is* the product |
| **Agent integration** | 👑 Native MCP (100+ tools) + LangChain/LangGraph, CrewAI, PydanticAI adapters | 🏆 Via a separate MCP server |
| **Compliance tooling** | 👑 `gdpr_forget` (Art. 17) + `gdpr_export` (Art. 20) as MCP tools; FIPS 140-3 deployment-ready crypto boundaryᵛ | Not a documented focus. No FIPS posture |
| **Auto-generated wiki / Obsidian export** | 👑 `m3 wiki generate` projects memories + files into an interlinked Markdown/Obsidian vault | No — API / MCP over a graph DB, not a file export |
| **Cost & licence** | Free, Apache 2.0 | Free, Apache 2.0 |

### When to choose M3-Memory over Graphiti
- You want zero-infrastructure local memory (one SQLite file, no graph DB to run — or PostgreSQL as the primary backend if you need a shared/server store).
- Retrieval — "recall what's relevant and still valid" — matters more than graph reasoning.
- You need MCP-native tools and offline operation out of the box.

### When to choose Graphiti over M3-Memory
- Your problem is genuinely graph-shaped: multi-hop entity relationship reasoning is the point, not a side feature.
- You already run (or want) a graph database and want the graph as the primary substrate.

### Can you use both?
Yes — they operate at different altitudes. You can let M3 own memory/retrieval and offload deep relationship reasoning to a Graphiti graph if a workload needs it.

<sub>[↑ Back to top](#top)</sub>

---

<a id="vs-a-mem"></a>

## ⚔️ M3-Memory vs A-MEM

A-MEM is a **research-oriented agentic memory** design: memories are "notes" that the system links into an evolving network (inspired by Zettelkasten), with the LLM generating structured attributes and dynamically updating links as new memories arrive. It's a compelling model for emergent, self-organizing memory and is primarily a research codebase rather than a production deployment target.

M3 is production-and-operations oriented: typed memories, bitemporal supersession, explicit GDPR/FIPS posture, an operational MCP tool surface, and a benchmarked retrieval stack. The *supersede operation* itself is deterministic and auditable (soft-delete + `supersedes` edge, not an LLM re-linking pass); automatic *detection* of which prior memory to supersede is a cosine+title heuristic (or you target it explicitly with `memory_supersede`).

| Feature | M3-Memory | A-MEM |
|---|---|---|
| **Category** | Dedicated memory layer (MCP-native) | 🛠️ Research prototype — self-organizing note network |
| **Deployment** | 👑 100% local by default — SQLite + bundled BGE-M3 embedder, fully offline, zero data egress | Depends on the LLM / embeddings you wire up |
| **Infrastructure required** | 👑 None to start — no server, no graph DB, no model server | ChromaDB + an LLM endpoint |
| **Storage backend** | 🏆 SQLite (default) **or PostgreSQL** as a first-class primary (`M3_DB_BACKEND=postgres`) — same semantics on either | ChromaDB |
| **Search / retrieval** | 🏆 3-pillar hybrid: FTS5 (BM25) + vector cosine + MMR diversity re-rank | Embedding-based over the note network |
| **Temporal model** | 🏆 Bitemporal (valid + transaction time), item-grain — local-first, no graph DB to run | ❌ No bitemporal / as-of queries |
| **Contradiction handling** | 👑 Heuristic auto-detect on write **plus** deterministic explicit `memory_supersede` — soft-delete, `supersedes` edge, history preserved | 🏆 LLM-driven link and attribute updates (Zettelkasten-style evolution) |
| **Published LongMemEval-S (QA)** | **92.0%** — standard S-setting, no oracle, unmodified upstream judgeᵃ (retrieval SHR 99.2%@10 / 100%@20) | Not published |
| **Knowledge graph** | 🏆 Automatic entity extraction (cognitive loop) + 9 relationship types; query-time entity-graph expansion feeds retrieval scoring (BFS to 3 hops), off-switchable | 🏆 Yes — an evolving link network is the core idea |
| **Agent integration** | 👑 Native MCP (100+ tools) + LangChain/LangGraph, CrewAI, PydanticAI adapters | Library / research code |
| **Compliance tooling** | 👑 `gdpr_forget` (Art. 17) + `gdpr_export` (Art. 20) as MCP tools; FIPS 140-3 deployment-ready crypto boundaryᵛ | Not a focus. No FIPS posture |
| **Auto-generated wiki / Obsidian export** | 👑 `m3 wiki generate` projects memories + files into an interlinked Markdown/Obsidian vault | Linked "memory notes" in ChromaDB — a note network, not a portable Markdown vault |
| **Cost & licence** | Free, Apache 2.0 | Free, MIT |

### When to choose M3-Memory over A-MEM
- You need something to deploy and operate today — with MCP integration, compliance tooling, and predictable behavior.
- You want deterministic, auditable contradiction handling rather than emergent LLM re-linking.

### When to choose A-MEM over M3-Memory
- You're researching self-organizing / emergent memory structures and want the note-network model as the object of study.
- Deterministic operations and compliance posture are not your priority.

<sub>[↑ Back to top](#top)</sub>

---

<a id="vs-agentmemory"></a>

## ⚔️ M3-Memory vs agentmemory

agentmemory (Jordan McCann, [github.com/JordanMcCann/agentmemory](https://github.com/JordanMcCann/agentmemory)) is a local-first, sovereign memory system that currently sits **#1 on the published LongMemEval-S leaderboard** (96.2% QA, graded with the exact upstream judge). Like M3 it's Native Python over local SQLite with a Merkle-tree integrity model and deterministic extraction — a genuine peer on sovereignty, and the strongest published retrieval number in the cohort.

M3's differences are breadth over a single-benchmark peak. Both ship a native MCP server and both scale to PostgreSQL; M3 adds framework adapters (LangChain/CrewAI/PydanticAI), bitemporal *valid-time* as-of queries (agentmemory's temporal signature is integrity-oriented rather than an as-of query model), first-class GDPR tooling, and the auto-generated wiki. On raw retrieval M3 leads on the like-for-like SHR metric (99.2%@10 / 100%@20); on published QA headline agentmemory's 96.2% edges M3's 92.0% — though both are answer-model-dependent and graded by the same strict judge, so it's the closest thing to an apples-to-apples QA number in the table.

| Feature | M3-Memory | agentmemory |
|---|---|---|
| **Category** | Dedicated memory layer (MCP-native) | Sovereign local-first memory system |
| **Deployment** | 👑 100% local by default — SQLite + bundled BGE-M3 embedder, fully offline, zero data egress | 🏆 Full local — SQLite, deterministic extraction, zero telemetry |
| **Infrastructure required** | 👑 None to start — no server, no graph DB, no model server | 🏆 None — native Python + SQLite |
| **Storage backend** | 🏆 SQLite (default) **or PostgreSQL** as a first-class primary (`M3_DB_BACKEND=postgres`) — same semantics on either | 🏆 SQLite (default) or PostgreSQL for multi-process |
| **Search / retrieval** | 🏆 3-pillar hybrid: FTS5 (BM25) + vector cosine + MMR diversity re-rank | 🏆 6-signal hybrid — a broader signal mix than m3's 3 pillars |
| **Temporal model** | 🏆 Bitemporal (valid + transaction time), item-grain — local-first, no graph DB to run | ⚖️ Temporal signature is integrity-oriented, not an as-of query model |
| **Contradiction handling** | 👑 Heuristic auto-detect on write **plus** deterministic explicit `memory_supersede` — soft-delete, `supersedes` edge, history preserved | Merkle-tree integrity + consolidation pipeline; no documented supersession model |
| **Published LongMemEval-S (QA)** | **92.0%** — standard S-setting, no oracle, unmodified upstream judgeᵃ (retrieval SHR 99.2%@10 / 100%@20) | 🏆 **96.2%** — #1 published, same strict upstream judge as m3ᶜ |
| **Knowledge graph** | 🏆 Automatic entity extraction (cognitive loop) + 9 relationship types; query-time entity-graph expansion feeds retrieval scoring (BFS to 3 hops), off-switchable | 🏆 Yes — auto entity extraction + graph spreading activation in retrieval |
| **Agent integration** | 👑 Native MCP (100+ tools) + LangChain/LangGraph, CrewAI, PydanticAI adapters | 🏆 Native MCP server (`agentmemory mcp`); no framework adapters documented |
| **Compliance tooling** | 👑 `gdpr_forget` (Art. 17) + `gdpr_export` (Art. 20) as MCP tools; FIPS 140-3 deployment-ready crypto boundaryᵛ | Local-only (no dedicated GDPR tooling). No FIPS posture |
| **Auto-generated wiki / Obsidian export** | 👑 `m3 wiki generate` projects memories + files into an interlinked Markdown/Obsidian vault | Not a documented feature |
| **Cost & licence** | Free, Apache 2.0 | Free, MIT |


### When to choose M3-Memory over agentmemory
- You want framework adapters (LangChain/CrewAI/PydanticAI) alongside MCP, not MCP alone.
- You need bitemporal as-of queries and GDPR primitives.
- You want to scale to PostgreSQL, or project memory to a portable wiki.

### When to choose agentmemory over M3-Memory
- The single published LongMemEval-S QA peak is your deciding factor.
- You don't need framework adapters, as-of temporal queries, or GDPR tooling — and its 6-signal hybrid retrieval appeals.

<sub>[↑ Back to top](#top)</sub>

---

<a id="vs-chronos"></a>

## ⚔️ M3-Memory vs Chronos

Chronos (PwC, arXiv [2603.16862](https://arxiv.org/abs/2603.16862)) is a research memory system organized around an **event-calendar / ISO-temporal** model, reporting 95.6% QA on LongMemEval-S. It runs on-prem (Linux/Python + services) with a dual-index retrieval design.

M3 differs on deployment simplicity and openness: zero-infrastructure local SQLite vs. Chronos's service stack, a published/reproducible benchmark posture (Chronos's LongMemEval judge is unpublished, so its number isn't independently verifiable), plus MCP, GDPR tooling, and the wiki. Chronos's ISO-temporal extraction is a genuine strength for calendar-grained reasoning.

| Feature | M3-Memory | Chronos |
|---|---|---|
| **Category** | Dedicated memory layer (MCP-native) | Research memory system — event-calendar / ISO-temporal model |
| **Deployment** | 👑 100% local by default — SQLite + bundled BGE-M3 embedder, fully offline, zero data egress | ⚖️ On-prem (Linux/Python + services) |
| **Infrastructure required** | 👑 None to start — no server, no graph DB, no model server | 🛠️ Service stack |
| **Storage backend** | 🏆 SQLite (default) **or PostgreSQL** as a first-class primary (`M3_DB_BACKEND=postgres`) — same semantics on either | Service-managed store |
| **Search / retrieval** | 🏆 3-pillar hybrid: FTS5 (BM25) + vector cosine + MMR diversity re-rank | ⚖️ Dual-index design |
| **Temporal model** | 🏆 Bitemporal (valid + transaction time), item-grain — local-first, no graph DB to run | 🏆 ISO-temporal event log — strong for calendar-grained reasoning |
| **Contradiction handling** | 👑 Heuristic auto-detect on write **plus** deterministic explicit `memory_supersede` — soft-delete, `supersedes` edge, history preserved | Event-log / ISO-temporal audit trail |
| **Published LongMemEval-S (QA)** | **92.0%** — standard S-setting, no oracle, unmodified upstream judgeᵃ (retrieval SHR 99.2%@10 / 100%@20) | ⚖️ **95.6%** self-reported, but the **judge is unpublished** — not independently verifiableᵈ |
| **Knowledge graph** | 🏆 Automatic entity extraction (cognitive loop) + 9 relationship types; query-time entity-graph expansion feeds retrieval scoring (BFS to 3 hops), off-switchable | Not a documented feature |
| **Agent integration** | 👑 Native MCP (100+ tools) + LangChain/LangGraph, CrewAI, PydanticAI adapters | 🛠️ Not documented |
| **Compliance tooling** | 👑 `gdpr_forget` (Art. 17) + `gdpr_export` (Art. 20) as MCP tools; FIPS 140-3 deployment-ready crypto boundaryᵛ | On-prem posture; no dedicated GDPR tooling. No FIPS posture |
| **Auto-generated wiki / Obsidian export** | 👑 `m3 wiki generate` projects memories + files into an interlinked Markdown/Obsidian vault | Not a documented feature |
| **Cost & licence** | Free, Apache 2.0 | Not publicly released |

### When to choose M3-Memory over Chronos
- You want zero-infrastructure local memory rather than an on-prem service stack.
- A reproducible, independently verifiable benchmark posture matters to your evaluation.
- You need MCP, GDPR tooling, and framework adapters.

### When to choose Chronos over M3-Memory
- Calendar-grained / ISO-temporal event reasoning is central to your problem.
- You're comfortable deploying and operating a service stack on-prem.


<sub>[↑ Back to top](#top)</sub>

---

<a id="vs-hindsight"></a>

## ⚔️ M3-Memory vs Hindsight

Hindsight (arXiv [2512.12818](https://arxiv.org/html/2512.12818v1)) is a local-first memory layer with a **4-stream neural retrieval** design (91.4% QA on LongMemEval-S), and is notably **framework-agnostic** — it ships dedicated LangGraph/CrewAI/AutoGen integrations among 40+ framework/tool connectors. On sovereignty and integration breadth it's a real peer.

M3's edges: native MCP with a 100+-tool surface (Hindsight integrates via per-framework adapters, not MCP), bitemporal as-of queries, GDPR primitives, PostgreSQL primary, lighter retrieval overhead (Hindsight's 4-stream rerank is heavier), and the wiki. Hindsight's published QA judge is unpublished, so its 91.4% isn't independently verifiable.

| Feature | M3-Memory | Hindsight |
|---|---|---|
| **Category** | Dedicated memory layer (MCP-native) | Local-first memory layer, framework-agnostic |
| **Deployment** | 👑 100% local by default — SQLite + bundled BGE-M3 embedder, fully offline, zero data egress | 🏆 Docker recommended, or embedded/self-contained via the `hindsight-all` Python package |
| **Infrastructure required** | 👑 None to start — no server, no graph DB, no model server | PostgreSQL (embedded `pg0` option for Python) |
| **Storage backend** | 🏆 SQLite (default) **or PostgreSQL** as a first-class primary (`M3_DB_BACKEND=postgres`) — same semantics on either | 🏆 PostgreSQL by default; Oracle AI Database for enterprise |
| **Search / retrieval** | 🏆 3-pillar hybrid: FTS5 (BM25) + vector cosine + MMR diversity re-rank | 🏆 4-stream neural retrieval — richer, but heavier rerank cost |
| **Temporal model** | 🏆 Bitemporal (valid + transaction time), item-grain — local-first, no graph DB to run | ⚖️ Traceable, but not an as-of query model |
| **Contradiction handling** | 👑 Heuristic auto-detect on write **plus** deterministic explicit `memory_supersede` — soft-delete, `supersedes` edge, history preserved | Not a documented focus |
| **Published LongMemEval-S (QA)** | **92.0%** — standard S-setting, no oracle, unmodified upstream judgeᵃ (retrieval SHR 99.2%@10 / 100%@20) | **91.4%** — below m3's 92.0%, but the **judge is unpublished**, so not strictly comparableᵉ |
| **Knowledge graph** | 🏆 Automatic entity extraction (cognitive loop) + 9 relationship types; query-time entity-graph expansion feeds retrieval scoring (BFS to 3 hops), off-switchable | 🏆 Yes — graph is one of its recall strategies |
| **Agent integration** | 👑 Native MCP (100+ tools) + LangChain/LangGraph, CrewAI, PydanticAI adapters | 🏆 Broad — LangGraph/CrewAI/AutoGen + 40+ connectors (adapter-based, no native MCP) |
| **Compliance tooling** | 👑 `gdpr_forget` (Art. 17) + `gdpr_export` (Art. 20) as MCP tools; FIPS 140-3 deployment-ready crypto boundaryᵛ | Local-only. No FIPS posture |
| **Auto-generated wiki / Obsidian export** | 👑 `m3 wiki generate` projects memories + files into an interlinked Markdown/Obsidian vault | Not a documented feature |
| **Cost & licence** | Free, Apache 2.0 | Free, MIT |

### When to choose M3-Memory over Hindsight
- You want native MCP with a 100+-tool surface rather than per-framework adapters.
- You need bitemporal as-of queries, GDPR primitives, or PostgreSQL as a primary store.
- Retrieval overhead matters — Hindsight's 4-stream rerank is heavier.

### When to choose Hindsight over M3-Memory
- You need its breadth of ready-made connectors (LangGraph/CrewAI/AutoGen + 40 more) and MCP isn't part of your stack.
- Multi-stream neural reranking is worth the extra retrieval cost for your workload.


<sub>[↑ Back to top](#top)</sub>

---

<a id="vs-mastra-om"></a>

## ⚔️ M3-Memory vs Mastra OM

Mastra Observational Memory ([mastra.ai](https://mastra.ai/blog/observational-memory)) is a memory layer for the Mastra agent framework built on **background observer/reflector agents** that compress message history into a dense observation log (94.9% QA, exact upstream judge). Its "reflector" extraction is a strength; it also keeps working memory as structured JSON/markdown *internally*.

M3 differs mainly on reach and temporal depth. Mastra OM is memory *for the Mastra framework* and needs one of its supported storage adapters (`@mastra/pg`, `libsql`, `mongodb`, `convex`) — it runs locally on a LibSQL file DB, and needs no vector or graph database. M3 adds native MCP and cross-framework adapters, bitemporal as-of queries, GDPR tooling, and a portable exported wiki (Mastra's internal markdown is working-memory state, not an exported vault).

| Feature | M3-Memory | Mastra OM |
|---|---|---|
| **Category** | Dedicated memory layer (MCP-native) | Observational memory for the Mastra agent framework |
| **Deployment** | 👑 100% local by default — SQLite + bundled BGE-M3 embedder, fully offline, zero data egress | 🏆 Runs locally (LibSQL file DB) or against hosted Postgres/MongoDB/Convex |
| **Infrastructure required** | 👑 None to start — no server, no graph DB, no model server | A supported storage adapter (`@mastra/pg`, `libsql`, `mongodb`, or `convex`); no vector or graph DB needed |
| **Storage backend** | 🏆 SQLite (default) **or PostgreSQL** as a first-class primary (`M3_DB_BACKEND=postgres`) — same semantics on either | 🏆 LibSQL, PostgreSQL, MongoDB, or Convex |
| **Search / retrieval** | 🏆 3-pillar hybrid: FTS5 (BM25) + vector cosine + MMR diversity re-rank | Observation-log retrieval over reflector-compressed history |
| **Temporal model** | 🏆 Bitemporal (valid + transaction time), item-grain — local-first, no graph DB to run | ⚖️ 3-date anchor, not full bitemporal |
| **Contradiction handling** | 👑 Heuristic auto-detect on write **plus** deterministic explicit `memory_supersede` — soft-delete, `supersedes` edge, history preserved | 🏆 Background reflector agents reconcile the observation log |
| **Published LongMemEval-S (QA)** | **92.0%** — standard S-setting, no oracle, unmodified upstream judgeᵃ (retrieval SHR 99.2%@10 / 100%@20) | 🏆 **94.9%** — exact upstream judge, directly comparable to m3's 92.0%ᶠ |
| **Knowledge graph** | 🏆 Automatic entity extraction (cognitive loop) + 9 relationship types; query-time entity-graph expansion feeds retrieval scoring (BFS to 3 hops), off-switchable | Not a documented feature |
| **Agent integration** | 👑 Native MCP (100+ tools) + LangChain/LangGraph, CrewAI, PydanticAI adapters | 🛠️ Mastra-framework-native only |
| **Compliance tooling** | 👑 `gdpr_forget` (Art. 17) + `gdpr_export` (Art. 20) as MCP tools; FIPS 140-3 deployment-ready crypto boundaryᵛ | ⚖️ Hybrid posture. No FIPS posture |
| **Auto-generated wiki / Obsidian export** | 👑 `m3 wiki generate` projects memories + files into an interlinked Markdown/Obsidian vault | Internal working-memory markdown/JSON — not an exported vault |
| **Cost & licence** | Free, Apache 2.0 | OSS + framework ecosystem |

### When to choose M3-Memory over Mastra OM
- You want memory that is not tied to one agent framework — M3 speaks MCP and backs LangChain, CrewAI, and PydanticAI from the same store.
- You need bitemporal as-of queries or first-class GDPR tooling.
- You need native MCP, bitemporal as-of queries, or GDPR tooling.

### When to choose Mastra OM over M3-Memory
- You're already building on the Mastra framework and want its native memory layer.
- Background observer/reflector compression of message history is the model you want.


<sub>[↑ Back to top](#top)</sub>

---

<a id="vs-memento"></a>

## ⚔️ M3-Memory vs Memento

Memento ([github.com/shane-farkas/memento-memory](https://github.com/shane-farkas/memento-memory)) is a sovereign, Native-Python memory system with a **bitemporal knowledge-graph + Merkle-audit** model and compositional retrieval — architecturally one of the closer peers to M3 on integrity and sovereignty. It reports 90.8% QA, but in the **oracle / evidence-only setting**, not standard LongMemEval-S.

M3's differences: a verifiable standard-setting benchmark (Memento's number is from the easier oracle setting, graded by a self-loosened judge). Both ship a native MCP server; M3 adds framework adapters (LangChain/CrewAI/PydanticAI), first-class GDPR tooling, PostgreSQL as a primary backend, and the wiki.

| Feature | M3-Memory | Memento |
|---|---|---|
| **Category** | Dedicated memory layer (MCP-native) | Sovereign bitemporal knowledge-graph memory |
| **Deployment** | 👑 100% local by default — SQLite + bundled BGE-M3 embedder, fully offline, zero data egress | ⚖️ Config-local (native Python, local SQLite) |
| **Infrastructure required** | 👑 None to start — no server, no graph DB, no model server | 🏆 None — native Python + SQLite |
| **Storage backend** | 🏆 SQLite (default) **or PostgreSQL** as a first-class primary (`M3_DB_BACKEND=postgres`) — same semantics on either | SQLite |
| **Search / retrieval** | 🏆 3-pillar hybrid: FTS5 (BM25) + vector cosine + MMR diversity re-rank | Compositional retrieval |
| **Temporal model** | 🏆 Bitemporal (valid + transaction time), item-grain — local-first, no graph DB to run | 🏆 Bitemporal KG with Merkle-audit — a genuine peer on temporal modelling |
| **Contradiction handling** | 👑 Heuristic auto-detect on write **plus** deterministic explicit `memory_supersede` — soft-delete, `supersedes` edge, history preserved | 🏆 Contradiction detection with entity resolution over a Merkle-audited bitemporal graph |
| **Published LongMemEval-S (QA)** | **92.0%** — standard S-setting, no oracle, unmodified upstream judgeᵃ (retrieval SHR 99.2%@10 / 100%@20) | **90.8%** — but in the easier **oracle / evidence-only setting** and graded by a **loosened judge**; the standard S-setting is unpublishedᵍ |
| **Knowledge graph** | 🏆 Automatic entity extraction (cognitive loop) + 9 relationship types; query-time entity-graph expansion feeds retrieval scoring (BFS to 3 hops), off-switchable | 🏆 Yes — a bitemporal KG is the core abstraction |
| **Agent integration** | 👑 Native MCP (100+ tools) + LangChain/LangGraph, CrewAI, PydanticAI adapters | 🏆 Native MCP server + provider packages for Anthropic, OpenAI, and Gemini |
| **Compliance tooling** | 👑 `gdpr_forget` (Art. 17) + `gdpr_export` (Art. 20) as MCP tools; FIPS 140-3 deployment-ready crypto boundaryᵛ | Local-only. No FIPS posture |
| **Auto-generated wiki / Obsidian export** | 👑 `m3 wiki generate` projects memories + files into an interlinked Markdown/Obsidian vault | Not a documented feature |
| **Cost & licence** | Free, Apache 2.0 | Free, MIT |

### When to choose M3-Memory over Memento
- You want a benchmark number from the standard LongMemEval-S setting, graded by the unmodified upstream judge.
- You need framework adapters (LangChain/CrewAI/PydanticAI) alongside MCP.
- GDPR tooling, PostgreSQL as primary, or the generated wiki matter.

### When to choose Memento over M3-Memory
- Merkle-audited integrity over a bitemporal knowledge graph is the architecture you want.
- Native MCP is sufficient and you don't need framework adapters.


<sub>[↑ Back to top](#top)</sub>

---

<a id="vs-mempalace"></a>

## ⚔️ M3-Memory vs MemPalace ⚠️

> **⚠️ Caution — listed to flag, not endorse.** MemPalace has **disputed benchmark claims** and **scam/malware-impostor concerns**. Only `github.com/MemPalace/mempalace` is the official repo; `.tech`/`.net` domain variants are flagged malware — **do not visit them**. This entry exists so a reader comparing tools has the verified facts, not to recommend it.

MemPalace advertises a spatial "memory-palace" (loci-hierarchy) architecture with 96.6% **R@5 recall** (a different metric than QA accuracy). An independent critical analysis (arXiv [2604.21284](https://arxiv.org/abs/2604.21284)) attributes the number to **ChromaDB's embeddings + verbatim storage, not the palace architecture**, and finds its compression mode lossy (R@5 drops 96.6%→84.2%).

| Feature | M3-Memory | MemPalace |
|---|---|---|
| **Category** | Dedicated memory layer (MCP-native) | ⚠️ Spatial "memory-palace" (loci-hierarchy) store — **disputed claims** |
| **Deployment** | 👑 100% local by default — SQLite + bundled BGE-M3 embedder, fully offline, zero data egress | Local Python + ChromaDB |
| **Infrastructure required** | 👑 None to start — no server, no graph DB, no model server | ChromaDB |
| **Storage backend** | 🏆 SQLite (default) **or PostgreSQL** as a first-class primary (`M3_DB_BACKEND=postgres`) — same semantics on either | 🛠️ ChromaDB + JSON (documented desync risk) |
| **Search / retrieval** | 🏆 3-pillar hybrid: FTS5 (BM25) + vector cosine + MMR diversity re-rank | ChromaDB embeddings + verbatim storage |
| **Temporal model** | 🏆 Bitemporal (valid + transaction time), item-grain — local-first, no graph DB to run | ❌ Verbatim only — no temporal model |
| **Contradiction handling** | 👑 Heuristic auto-detect on write **plus** deterministic explicit `memory_supersede` — soft-delete, `supersedes` edge, history preserved | ❌ Verbatim only; 🛠️ multi-agent writes can fail silently |
| **Published LongMemEval-S (QA)** | **92.0%** — standard S-setting, no oracle, unmodified upstream judgeᵃ (retrieval SHR 99.2%@10 / 100%@20) | ⚠️ No QA figure published — its headline 96.6% is **R@5 recall, a different metric**, and independently attributed to ChromaDB rather than the architectureʰ |
| **Knowledge graph** | 🏆 Automatic entity extraction (cognitive loop) + 9 relationship types; query-time entity-graph expansion feeds retrieval scoring (BFS to 3 hops), off-switchable | Loci hierarchy rather than an entity graph |
| **Agent integration** | 👑 Native MCP (100+ tools) + LangChain/LangGraph, CrewAI, PydanticAI adapters | Not documented |
| **Compliance tooling** | 👑 `gdpr_forget` (Art. 17) + `gdpr_export` (Art. 20) as MCP tools; FIPS 140-3 deployment-ready crypto boundaryᵛ | Not documented. No FIPS posture |
| **Auto-generated wiki / Obsidian export** | 👑 `m3 wiki generate` projects memories + files into an interlinked Markdown/Obsidian vault | Not a documented feature |
| **Cost & licence** | Free, Apache 2.0 | OSS (official repo only — see caution above) |


<sub>[↑ Back to top](#top)</sub>

---

<a id="vs-langmem"></a>

## ⚔️ M3-Memory vs LangChain Memory / LangMem

LangChain Memory (including LangGraph's thread/store memory and the newer LangMem library) is memory that lives inside the LangChain ecosystem. It covers short-term thread memory, long-term JSON stores, and LangMem's episodic/semantic/procedural memory types. It's the natural choice if you're already building LangGraph agents.

M3-Memory is framework-agnostic and MCP-native — it works with any agent via a single config line. It is also **compatible with LangMem**: `M3Store` implements LangGraph's `BaseStore`, so LangMem's tools and background manager run on M3 unchanged (`store=M3Store()`) — persisted locally with contradiction, temporal, and graph features underneath.

**For LangChain users, M3 is a superset.** You keep everything LangChain Memory / LangMem gives you — thread memory, the `BaseStore`, LangMem's episodic/semantic/procedural tools — and gain what they don't: automatic contradiction supersession, bitemporal `as_of` queries, commanded forgetting (GDPR), hybrid FTS5+vector+MMR retrieval, a bundled in-process embedder, and M3's full 100+ MCP tool surface exposed to your agent — all local-first, no external store to provision. Nothing is given up; capabilities are added.

| Feature | M3-Memory | LangChain Memory / LangMem |
|---|---|---|
| **Category** | Dedicated memory layer (MCP-native) | Memory inside the LangChain / LangGraph ecosystem |
| **Deployment** | 👑 100% local by default — SQLite + bundled BGE-M3 embedder, fully offline, zero data egress | 🏆 Good — depends on the backend store you configure |
| **Infrastructure required** | 👑 None to start — no server, no graph DB, no model server | A backend store + an external embedder |
| **Storage backend** | 🏆 SQLite (default) **or PostgreSQL** as a first-class primary (`M3_DB_BACKEND=postgres`) — same semantics on either | 🏆 Pluggable (`BaseStore` implementations) |
| **Search / retrieval** | 🏆 3-pillar hybrid: FTS5 (BM25) + vector cosine + MMR diversity re-rank | Depends on the configured backend store |
| **Temporal model** | 🏆 Bitemporal (valid + transaction time), item-grain — local-first, no graph DB to run | ❌ No bitemporal / as-of queries |
| **Contradiction handling** | 👑 Heuristic auto-detect on write **plus** deterministic explicit `memory_supersede` — soft-delete, `supersedes` edge, history preserved | 🏆 Manual / LLM-driven via procedural memory |
| **Published LongMemEval-S (QA)** | **92.0%** — standard S-setting, no oracle, unmodified upstream judgeᵃ (retrieval SHR 99.2%@10 / 100%@20) | Not published |
| **Knowledge graph** | 🏆 Automatic entity extraction (cognitive loop) + 9 relationship types; query-time entity-graph expansion feeds retrieval scoring (BFS to 3 hops), off-switchable | ❌ A store abstraction, not a graph |
| **Agent integration** | 👑 Native MCP (100+ tools) + LangChain/LangGraph, CrewAI, PydanticAI adapters | 🏆 Native to LangChain/LangGraph; no MCP. m3 implements its `BaseStore`, so LangMem runs on m3 unchanged |
| **Compliance tooling** | 👑 `gdpr_forget` (Art. 17) + `gdpr_export` (Art. 20) as MCP tools; FIPS 140-3 deployment-ready crypto boundaryᵛ | Custom implementation required. No FIPS posture |
| **Auto-generated wiki / Obsidian export** | 👑 `m3 wiki generate` projects memories + files into an interlinked Markdown/Obsidian vault | No — a store abstraction, not a knowledge-base generator |
| **Cost & licence** | Free, Apache 2.0 | Free, MIT |

### When to choose M3-Memory over LangChain Memory

- You use Claude Code, Cursor, Cline, Gemini CLI, Aider, or any non-LangChain MCP agent
- You need a single memory backend that works across multiple agent frameworks
- You want automatic contradiction detection without writing custom procedural memory logic
- GDPR compliance tooling is a requirement

### When to choose LangChain Memory / LangMem

- You want to keep using LangMem's tools and taxonomy directly — in which case **back them with M3** (`store=M3Store()`) to gain local-first storage, contradiction handling, and temporal queries without changing your LangMem code
- You prefer everything in one unified LangChain install and don't need M3's extra capabilities

> **Note:** choosing LangMem and choosing M3 are not mutually exclusive — M3 implements the `BaseStore` LangMem runs on. See [`docs/integrations/LANGCHAIN.md`](integrations/LANGCHAIN.md).

<sub>[↑ Back to top](#top)</sub>

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
| Managed cloud, multi-tenant, hosted dashboard (SaaS) | [**Mem0**](#vs-mem0) |
| CrewAI memory backend (v1.10+ `StorageBackend`), local-first | **M3-Memory** |
| CrewAI memory that's *also* searchable by your other agents (Claude Code, LangChain) | **M3-Memory** |
| PydanticAI agent memory (drop-in tools + auto-recall, or a formal `AbstractToolset`) | **M3-Memory** |
| One memory store backing LangChain, CrewAI, *and* PydanticAI at once | **M3-Memory** |
| PostgreSQL as a first-class primary backend, local-first (not a managed cloud DB) | **M3-Memory** |
| Long-lived autonomous agents that self-edit memory | [**Letta**](#vs-letta) |
| Full stateful agent runtime (not just memory) | [**Letta**](#vs-letta) |
| Git-backed agent state | [**Letta**](#vs-letta) |
| Dedicated graph-database backend (Neo4j / FalkorDB) at massive scale | [**Zep**](#vs-zep) |
| Graph-first entity/relationship reasoning on a graph DB | [**Graphiti**](#vs-graphiti) |
| Bitemporal memory + temporal queries, local-first, no graph DB to run | **M3-Memory** |
| Short-term + long-term memory unified, separate, or searched together by config | **M3-Memory** |
| Researching self-organizing / emergent memory structures | [**A-MEM**](#vs-a-mem) |
| The single highest published LongMemEval-S QA score, sovereign, MCP not required | [**agentmemory**](#vs-agentmemory) |
| Calendar-grained / ISO-temporal event reasoning, on-prem service stack acceptable | [**Chronos**](#vs-chronos) |
| Breadth of per-framework connectors (LangGraph / CrewAI / AutoGen + 40 more) | [**Hindsight**](#vs-hindsight) |
| Memory for agents already built on the Mastra framework | [**Mastra OM**](#vs-mastra-om) |
| Merkle-audited bitemporal knowledge graph, native MCP, no framework adapters needed | [**Memento**](#vs-memento) |
| Retrieval accuracy on the like-for-like metric (99.2% SHR@10, 100% @ k=20) | **M3-Memory** |

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
