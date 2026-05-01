# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory — Comparison Guide

> Last updated: April 2026. Corrections welcome via [issue](https://github.com/skynetcmd/m3-memory/issues).

Several tools address agentic memory. This document explains where M3 Memory fits relative to each, and when a different tool is the better choice.

> 💡 Looking for a head-to-head against other **sovereign / local-first memory substrates** (agentmemory, Chronos, Hindsight, Mastra OM, Memento, MemPalace)? See the [Sovereign Memory Systems comparison table](M3_Comparison_Table.html) — different cohort, different decision.

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

## ⚔️ M3-Memory vs Mem0

Mem0 is a popular agentic memory library with broad ecosystem adoption. It's excellent for cloud-hosted, multi-session personalization in LangChain/LangGraph/CrewAI apps. M3-Memory targets a different audience: developers using **desktop coding agents** (Claude Code, Gemini CLI, Aider) who need memory that is private, offline-capable, and speaks MCP natively.

| Feature | M3-Memory | Mem0 |
|---------|-----------|------|
| **Primary deployment** | Local SQLite — works fully offline | Cloud API (self-host is possible but not the happy path) |
| **MCP support** | Native — 72 tools, zero config in Claude Code / Gemini CLI | No native MCP; requires a custom wrapper |
| **Search algorithm** | FTS5 (BM25) + vector cosine + MMR diversity re-ranking | Vector search + knowledge graph traversal |
| **Contradiction detection** | Automatic on write — old memory soft-deleted, `supersedes` relationship recorded | Basic deduplication; no strong conflict resolution |
| **Bitemporal history** | `valid_from` / `valid_to` on every memory — query state as of any past date | No |
| **GDPR tooling** | `gdpr_forget` (Art. 17 hard delete) + `gdpr_export` (Art. 20 portable JSON) as MCP tools | Manual; no dedicated GDPR tooling |
| **Embeddings** | Local LLM only (Ollama, LM Studio, vLLM) — zero data egress | Cloud embedding APIs by default |
| **API keys required** | None | Yes (cloud version) |
| **Offline operation** | Full — SQLite + local embeddings | No (cloud version) |
| **Cross-device sync** | SQLite ↔ PostgreSQL ↔ ChromaDB, bi-directional delta sync | Managed by Mem0 cloud |
| **Knowledge graph** | Yes — 9 relationship types, 3-hop traversal | Yes — strong point |
| **Multi-agent concurrent writes** | Atomic via SQLite WAL — multiple agents writing simultaneously without races | Cloud version handles via API queueing; multi-writer correctness in self-host is not emphasized |
| **Cognition placement** | Composable — disable, replace, or use built-in SLM extraction | LLM-driven extraction is welded into the memory layer |
| **Multi-tenant** | Per-agent scoping (`agent_id`, `user_id`, `scope`) | Yes — production-grade |
| **LangChain integration** | Not the focus | Excellent |
| **Cost** | Free, Apache 2.0 licensed | Free tier + $249/mo Pro |
| **Stars (Apr 2026)** | Early — just launched | 20k+ |

### When to choose M3-Memory over Mem0

- You use Claude Code, Gemini CLI, Aider, or any MCP-compatible agent
- Your data cannot leave your machine (enterprise, regulated industries, personal privacy)
- You need agents that stay factually consistent (contradiction detection matters)
- You want compliance tooling (GDPR forget/export) without building it yourself
- You're on a budget — no API costs, no subscriptions, runs on consumer hardware
- You need offline operation (no internet at the coffee shop, air-gapped environments)

### When to choose Mem0 over M3-Memory

- You're building LangChain / LangGraph / CrewAI applications
- You need managed multi-tenant cloud memory with a hosted dashboard
- You want a well-established solution with broad ecosystem adoption
- Multi-session personalization at scale is your primary use case

---

## ⚔️ M3-Memory vs Letta

Letta (formerly MemGPT) is a **full stateful agent runtime** with memory built in — not just a memory layer. It uses hierarchical memory blocks (core / recall / archival) that the agent itself edits via tool calls. Letta Code adds git-backed agent state. It's a powerful platform for long-lived, self-improving agents.

M3-Memory is a **dedicated, lightweight memory layer** — a drop-in backend for agents you already have. It does one thing: give your agent persistent, private, consistent memory via 72 MCP tools.

| Feature | M3-Memory | Letta |
|---------|-----------|-------|
| **Type** | Dedicated memory layer | Full agent runtime + memory |
| **Adoption** | Drop-in — one line in mcp.json | Full runtime adoption required |
| **MCP support** | Native — 72 tools, zero config | Custom SDKs / REST API |
| **Memory model** | Semantic store + knowledge graph | Tiered blocks (core / recall / archival) |
| **Search** | FTS5 + vector + MMR | Tiered recall with embeddings |
| **Contradiction handling** | Automatic on write — bitemporal supersede | Agent-driven — the agent must decide to update its own memory |
| **GDPR tooling** | Built-in `gdpr_forget` + `gdpr_export` | Not built-in |
| **Bitemporal history** | Yes — query state as of any past date | No |
| **Deployment** | 100% local (SQLite) by default | Self-hosted or Letta Cloud |
| **Works with existing agents** | Yes — any MCP agent unchanged | No — must rebuild on Letta runtime |
| **Long-lived self-improving agents** | Supported | Core strength |
| **Git-backed agent state** | No | Yes (Letta Code) |
| **Cost** | Free, Apache 2.0 | OSS + Letta Cloud SaaS |

### When to choose M3-Memory over Letta

- You use Claude Code, Gemini CLI, Aider, or any existing MCP agent and want to add memory **without rewriting your stack**
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
| **Temporal model** | Bitemporal (valid time + transaction time) | Strong temporal KG |
| **GDPR tooling** | Built-in MCP tools | Partial |
| **MCP support** | Native | No |
| **Deployment** | Local SQLite to start | Self-hosted or Zep Cloud |
| **Cost** | Free, OSS | OSS + SaaS |

---

## ⚔️ M3-Memory vs LangChain Memory / LangMem

LangChain Memory (including LangGraph's thread/store memory and the newer LangMem library) is memory that lives inside the LangChain ecosystem. It covers short-term thread memory, long-term JSON stores, and LangMem's episodic/semantic/procedural memory types. It's the natural choice if you're already building LangGraph agents.

M3-Memory is framework-agnostic and MCP-native — it works with any agent via a single config line.

| Feature | M3-Memory | LangChain Memory / LangMem |
|---------|-----------|---------------------------|
| **Ecosystem** | Any MCP agent | LangChain / LangGraph only |
| **MCP support** | Native — 72 tools | No |
| **Memory types** | 21 types + auto-classification | Thread, store, episodic, semantic, procedural |
| **Contradiction handling** | Automatic — bitemporal superseding | Manual / LLM-driven via procedural memory |
| **GDPR tooling** | Built-in `gdpr_forget` + `gdpr_export` | Custom implementation required |
| **Search** | FTS5 + vector + MMR | Depends on configured backend store |
| **Local-first** | 100% — SQLite, fully offline | Good — depends on backend store choice |
| **Installation** | `pip install m3-memory` + 1-line config | Part of LangChain / LangGraph install |
| **Overhead** | Very light | Medium (tied to LangGraph runtime) |
| **Cost** | Free, Apache 2.0 | Free, MIT |

### When to choose M3-Memory over LangChain Memory

- You use Claude Code, Gemini CLI, Aider, or any non-LangChain MCP agent
- You need a single memory backend that works across multiple agent frameworks
- You want automatic contradiction detection without writing custom procedural memory logic
- GDPR compliance tooling is a requirement

### When to choose LangChain Memory

- You're building exclusively on LangGraph and want framework-native memory with no extra dependencies
- LangMem's episodic/semantic/procedural taxonomy fits your use case well
- You prefer everything in one unified LangChain install

---

## 📋 Summary Decision Matrix

| I need... | Best choice |
|-----------|-------------|
| Memory for Claude Code / Gemini CLI / Aider | **M3-Memory** |
| Zero cloud, fully offline, private | **M3-Memory** |
| Automatic contradiction detection | **M3-Memory** |
| GDPR forget + export as MCP tools | **M3-Memory** |
| Drop-in memory for an existing MCP agent | **M3-Memory** |
| LangChain / CrewAI integration | **Mem0** |
| Managed cloud, multi-tenant, hosted dashboard | **Mem0** |
| Already building on LangGraph, want framework-native memory | **LangChain Memory / LangMem** |
| Long-lived autonomous agents that self-edit memory | **Letta** |
| Full stateful agent runtime (not just memory) | **Letta** |
| Git-backed agent state | **Letta** |
| Enterprise temporal knowledge graph at scale | **Zep** |

---

## 🎯 Where M3 Memory fits

M3 Memory targets a specific intersection that other tools don't fully cover:

- **MCP-native** — works with Claude Code, Gemini CLI, Aider, and any MCP agent without integration code
- **Local-first** — all data stays on your machine; fully offline-capable
- **Automatic consistency** — contradiction detection handles conflicting facts without agent-side logic
- **Compliant** — GDPR forget and export are built-in MCP tools
- **Drop-in** — one config line to add memory to an existing agent; no runtime migration

If you need LangChain pipeline memory, a full agent runtime, or enterprise-scale temporal graphs, the tools above are better suited. If you need persistent, private memory for MCP agents, M3 is built for that.

---

## 🔍 Verifying claims (about M3 or any tool here)

Third-party AI assistants — Copilot, Gemini, ChatGPT, others — sometimes describe memory tools with features they don't have or scores they haven't earned. This isn't malicious; pattern-matching on a project name is what assistants do when they don't have ground truth.

For M3 specifically, the source of truth is this repo. If you've seen a feature attributed to M3 elsewhere that we don't document here, in [`README.md`](../README.md), or in [`docs/MYTHS_AND_FACTS.md`](MYTHS_AND_FACTS.md), assume it's hallucinated until verified against the source.

Same skepticism should apply to claims about Mem0, Letta, Zep, LangChain Memory, or any other tool in this guide. We've tried to cite each tool's own docs and benchmarks where possible. Where we couldn't, we've said so. If you find a comparison row in this document that misrepresents another tool, [open an issue](https://github.com/skynetcmd/m3-memory/issues) — accuracy matters more than position, and we'll fix it.
