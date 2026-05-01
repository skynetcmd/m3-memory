# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Sovereign Memory Systems Comparison

> Last updated: May 2026. Honest dimensional comparison of local-first / sovereign memory substrates.

> 🔗 **Interactive version:** [M3_Comparison_Table.html](https://html-preview.github.io/?url=https://github.com/skynetcmd/m3-memory/blob/main/docs/M3_Comparison_Table.html) — same data, but with sticky columns, sticky section labels, hover tooltips on acronyms, and clickable jump-links into the dimension glossary. Recommended if you want to scroll across the cohort comparison.

---

## How to read this page

This is a head-to-head against other **sovereign / local-first memory substrates** — projects competing on data residency, integrity, and offline operation. If you're choosing between M3 and a developer-tool memory layer like Mem0, Letta, Zep, or LangChain Memory, see the [developer-facing decision guide](COMPARISON.md) instead.

| Pillar | What M3 ships |
|---|---|
| **Sovereignty** | Local SQLite, local-SLM extraction, zero telemetry by default. |
| **Integrity** | Bitemporal logic with native undo across *valid time* and *transaction time*. |
| **Orchestration** | Native MCP handoffs, agent registry, atomic WAL writes. |
| **Compliance** | Built-in GDPR primitives; aligns with [FISMA](M3_Compliance_FISMA.md) and [CMMC](M3_Compliance_CMMC.md). |

> ⚠️ **Where M3 doesn't lead:** on raw LongMemEval-S recall accuracy, M3 sits at **89.0%** while several competitors reach 95–96%. M3 trades that gap for sovereignty, bitemporal correctness, and a small auditable codebase. If raw recall is the only thing that matters, the higher scorers in the table below may fit better — and we'll say so directly.

---

## ▸ Sovereignty & Integrity (M3 strengths)

| Dimension | m3-memory<br>(2026.4.24.12) | agentmemory<br>(V4.0.2) | Chronos<br>(High/Res) | Hindsight<br>(v0.5.4) | Mastra OM<br>(v1.26.0) | Mem0<br>(v3.0.0) | Memento<br>(v1.0.0) | MemPalace<br>(v3.3.0) |
|---|---|---|---|---|---|---|---|---|
| **[Sovereignty (Main)](#sovereignty-main)** | 🛡️ **Full Sovereign** | 🛡️ **Full Sovereign** | ⚖️ **On-Prem** | ⚖️ **High Local** | ⚖️ **Hybrid** | 🔻 **Cloud-Tied** | ⚖️ **Config-Local** | 🛡️ **Full Verbatim** |
| ↳ *Data Residency* | 🏆 Local SQLite | ✅ Local SQLite | ✅ Local Files | ✅ Local Files | ⚖️ Postgres / Container | 🔻 Cloud DB | ✅ Local SQLite | ✅ Local SQLite |
| ↳ *Extraction Compute* | 🏆 Local SLM | ✅ Deterministic | ✅ ISO-Temporal | ✅ Neural / Local | 🔻 Cloud Reflector | 🔻 Cloud LLM | ⚖️ User-Defined | ❌ Verbatim only |
| ↳ *Telemetry / Audit* | 🏆 Zero / Bitemporal | ✅ Zero / Merkle | ✅ Event Logs | ✅ Internal | ⚖️ Usage Logs | 🔻 SaaS Metrics | ✅ Zero / Merkle | 🛡️ Total Dark |
| ↳ *Infrastructure* | 🏆 Native Python + SQLite | ✅ Native Python | ⚖️ Linux / Python | ⚖️ Py / Services | 🔻 Docker stack | ✅ SDK / API | ✅ Native Python | ✅ Native Python |
| **[Data Integrity](#data-integrity)** | 🏆 **Bitemporal Logic + Undo** | 🏆 **Merkle Tree** | ✅ **Event Logs** | ✅ **Traceable** | ⚖️ **DB-Level only** | 🔻 **Managed only** | 🏆 **Merkle-Audit** | 🔻 **JSON Desync Risk** |
| **[Bitemporal & Undo](#bitemporal--undo)** | 🏆 **Full bitemp + Undo** | ⚖️ **Temporal sig.** | ✅ **Audit log** | ✅ **Traceable** | ✅ **3-Date Anchor** | 🔻 **No Undo** | 🏆 **Merkle-Audit** | ❌ **Verbatim only** |
| **[Privacy / GDPR](#privacy--gdpr)** | 🏆 **Native GDPR tools** | ✅ **Local-only** | ✅ **On-Prem** | ✅ **Local-only** | ⚖️ **Hybrid** | 🔻 **No native** | ✅ **Local-only** | 🛡️ **Total sovereignty** |

---

## ▸ Multi-Agent & Concurrency

| Dimension | m3-memory | agentmemory | Chronos | Hindsight | Mastra OM | Mem0 | Memento | MemPalace |
|---|---|---|---|---|---|---|---|---|
| **[Multi-agent Writes](#multi-agent-writes)** | 🏆 **Atomic (WAL)** | 🏆 **Durable Objects** | ✅ **Turn-based** | 🏆 **Shared Banks** | ⚖️ **Adapter-based** | ✅ **ID-Scoped** | ✅ **Transactional** | 🔻 **Silent failures** |
| **[Multi-agent Orchestration](#multi-agent-orchestration)** | 🏆 **Native MCP handoffs** | 🏆 **Orchestrated** | ✅ **Sequential** | 🏆 **Bank-Scoped** | 🏆 **Supervisor** | ✅ **ID-Scoped** | 🏆 **Native (MCP)** | ✅ **Agent Diaries** |
| **[Native OS Support](#native-os-support)** | 🍎 🐧 🪟 | 🍎 🐧 | 🐧 🍎 | 🐧 🍎 | 🔻 (Docker only) | 🍎 🐧 🪟 | 🍎 🐧 🪟 | 🍎 🐧 🪟 |
| **[Multi-Computer Sync](#multi-computer-sync)** | 🏆 **Bi-dir Delta Sync** | ✅ **Managed API** | ✅ **Web Server** | 🏆 **Local Server** | 🏆 **Cloud / EKS** | 🏆 **Cloud Native** | ⚖️ **Local Sync** | 🔻 **Manual Sync** |

---

## ▸ Retrieval & Extraction (M3 is solid; not the recall leader)

| Dimension | m3-memory | agentmemory | Chronos | Hindsight | Mastra OM | Mem0 | Memento | MemPalace |
|---|---|---|---|---|---|---|---|---|
| **[LME-S Score](#lme-s-score)** | **89.0%** | **96.2%** (🏆 #1) | **95.6%** | **91.4%** | **94.9%** | **89.1%** | **90.8%** | **96.6%** (R@5) |
| **[Search Strategy](#search-strategy)** | ✅ **3-Pillar Hybrid** | 🏆 **6-Signal Hybrid** | ⚖️ **Dual-Index** | 🏆 **4-Stream Neural** | ✅ **Reflective** | ✅ **Vector-only** | ✅ **Compositional** | ⚖️ **Spatial / AAAK** |
| **[Local Fact Extraction](#local-fact-extraction)** | 🏆 **Local SLM** | ✅ **Deterministic** | ✅ **ISO-Temporal** | ✅ **Entity-centric** | 🏆 **Reflector** | ✅ **LLM-Powered** | ✅ **Entity-Res.** | ❌ **Verbatim only** |
| **[Token Efficiency](#token-efficiency)** | 🏆 **Working Memory** | ✅ **Signal Filter** | ✅ **Event-Pruned** | 🔻 **Heavy Rerank** | ✅ **Cache-Stable** | 🏆 **~90% Savings** | ✅ **Verbatim Fall.** | ⚖️ **AAAK Dialect** |

---

## ▸ Architecture (for context)

| Dimension | m3-memory | agentmemory | Chronos | Hindsight | Mastra OM | Mem0 | Memento | MemPalace |
|---|---|---|---|---|---|---|---|---|
| **[Architecture](#architecture)** | **3-Tier** (Short / Working / Long-Term) | **6-Signal Hybrid** | **Event Calendar** | **4-Stream** | **3-Tier** (Obs / Ref) | **Dual-Store** | **Bitemporal KG** | **Loci Hierarchy** |

---

## Icon Legend

- **🛡️ Sovereign** — 100% offline, local-SLM extraction, zero telemetry by default.
- **🏆 Best-in-class** — leads the cohort on this dimension.
- **✅ Has feature** — standard implementation; functional and competitive.
- **⚖️ Parity / partial** — feature exists with a meaningful caveat.
- **🔻 Weakness** — significant cloud dependency or stability risk.
- **❌ Missing** — capability not implemented.

---

## When another tool fits better

M3 is not the right answer for every workload. Pick from the table based on what matters most to *you*:

- **Pure recall accuracy is paramount** — agentmemory (96.2%) or MemPalace (96.6% R@5) lead. M3's 89.0% is solid but not state-of-the-art.
- **You need extreme token compression** — Mem0 reports ~90% context savings. M3's working-memory model is good but not as aggressive.
- **You only need verbatim recall, no extraction** — MemPalace's verbatim-only mode is purpose-built for this.
- **Heavy neural reranking is acceptable** — Hindsight's 4-stream architecture wins on rich retrieval if you can absorb the latency cost.
- **You're committed to a Docker-first ops model** — Mastra OM fits cleanly into containerized stacks.

If **sovereignty, bitemporal correctness, and a small auditable codebase** matter more than raw recall, M3 is built for that combination — and the table above is the receipt.

For the developer-tool decision (Mem0, Letta, Zep, LangChain Memory), see the [developer-facing comparison guide](COMPARISON.md).

---

## Dimension glossary & analysis

> **SOTA** = state-of-the-art.

### Sovereignty (Main)

**What it means:** Independence from cloud services, telemetry, and external dependencies for normal operation.

**Why it matters:** If your data *can't* leave the machine — for legal, contractual, or personal reasons — every external dependency is a compliance risk and an attack surface.

**M3 standing:** Full Sovereign. Local SQLite, local SLM extraction, zero telemetry, native Python — runs on a laptop or in an air-gapped enclave with the same code path.

**Sub-dimensions:**
- **Data Residency:** Local SQLite — single file, portable, inspectable.
- **Extraction Compute:** Local SLM via LM Studio / Ollama / vLLM — no data egress.
- **Telemetry / Audit:** Zero by default; bitemporal log gives auditability without phoning home.
- **Infrastructure:** `pip install` — no Docker, no services, no daemons.

**Cohort context:** M3 is tied with agentmemory and MemPalace at the top. Cloud-tied systems (Mem0) can't reach this tier without significant rework. Mastra OM's Docker stack is more dependent than M3's plain-Python install.

---

### Data Integrity

**What it means:** Mechanisms that keep data accurate, consistent, and tamper-evident across time and across agents.

**Why it matters:** Silent corruption destroys trust slowly. By the time you notice the memory is wrong, the bad fact has already propagated through dozens of decisions.

**M3 standing:** Bitemporal logic with native undo. Every write is durable (WAL), every fact is bounded by valid-time and transaction-time, and supersedes relationships record exactly which old fact was replaced and when.

**Cohort context:** Merkle-tree systems (agentmemory, Memento) provide cryptographic audit but no native undo; bitemporal gives undo but isn't cryptographic. JSON-store systems (MemPalace) carry silent-desync risk.

---

### Bitemporal & Undo

**What it means:** Tracking facts along two independent time axes — *valid time* (when the fact was true in the world) and *transaction time* (when M3 learned it) — with the ability to undo writes.

**Why it matters:** Agents make mistakes. Without bitemporal logic and undo, every error becomes permanent or requires destructive overwrites that lose context.

**M3 standing:** SOTA — full bitemporal model + native undo via supersedes relationships.

**Cohort context:** Memento offers Merkle-style audit (different shape, also strong). Mem0 has no undo; mistakes there are sticky.

---

### Privacy / GDPR

**What it means:** Built-in primitives for the right to erasure (Article 17) and data portability (Article 20).

**Why it matters:** Many regulated workloads require these capabilities to be operational, not theoretical. Implementing them retroactively on top of a memory layer is expensive and error-prone.

**M3 standing:** SOTA — `gdpr_forget` (hard delete) and `gdpr_export` (portable JSON) ship as MCP tools. See also the [FISMA](M3_Compliance_FISMA.md) and [CMMC](M3_Compliance_CMMC.md) alignment notes.

**Cohort context:** Local-only systems (agentmemory, Memento) inherit privacy by deployment but require custom GDPR tooling. Mem0 has no native GDPR primitives.

---

### Multi-agent Writes

**What it means:** Safe handling of concurrent writes when multiple agents update memory simultaneously.

**Why it matters:** In real agent swarms, race conditions silently destroy knowledge — and the corruption usually surfaces hours or days later.

**M3 standing:** Atomic via SQLite WAL. Writes are durable, ordered, and crash-safe.

**Cohort context:** agentmemory (durable objects) and Hindsight (shared banks) are at parity. MemPalace's "silent failures" mode is the worst category here — writes can drop without surfacing an error.

---

### Multi-agent Orchestration

**What it means:** Built-in primitives for task handoff, context sharing, and coordinated agent lifecycles.

**Why it matters:** A single agent is useful. Multiple agents that can hand off work mid-task are the actual value of "agentic" systems.

**M3 standing:** Native MCP handoffs, agent registry, notifications, tasks — all via the same MCP tool surface, no extra runtime required.

**Cohort context:** Memento also goes native MCP. Mastra OM uses a supervisor pattern. Mem0 scopes by ID but doesn't ship handoff primitives.

---

### Native OS Support

**What it means:** Runs natively on macOS, Linux, and Windows without Docker or platform-specific dependencies.

**Why it matters:** Most developers' machines are macOS or Windows; most production servers are Linux. A memory layer that works in all three avoids forcing operational compromises.

**M3 standing:** Full native support — same install command everywhere.

**Cohort context:** Mastra OM's Docker-only deployment is the outlier; the rest of the cohort cover at least two OSes.

---

### Multi-Computer Sync

**What it means:** Synchronizing memory across multiple physical machines without a central cloud.

**Why it matters:** A laptop, a desktop, and a server should be able to share memory without giving the data to a SaaS provider.

**M3 standing:** Bi-directional delta sync via PostgreSQL or ChromaDB — set one env var, your memories follow you across devices.

**Cohort context:** Cloud-native systems (Mem0) deliver sync trivially but at the cost of sovereignty. MemPalace requires manual sync.

---

### LME-S Score

**What it means:** LongMemEval-S, a 500-question benchmark for long-horizon conversational memory retrieval. The single number captures recall accuracy averaged across question types.

**Why it matters:** A retrieval layer that can't find what's there is a liability. But — and this is the part vendor pages skip — the benchmark doesn't measure sovereignty, integrity, undo, or compliance. A 96% score sourced from a cloud LLM tells you nothing about whether your data left the machine.

**M3 standing:** 89.0% — solid mid-pack. We chose to optimize correctness, sovereignty, and a small codebase over chasing the last 7 percentage points of recall.

**Cohort context:** agentmemory (96.2%), MemPalace R@5 (96.6%), Chronos (95.6%), and Mastra OM (94.9%) lead on raw recall. If recall is the only dimension that matters, those are the better picks. M3 (89.0%) is closer to Mem0 (89.1%), Memento (90.8%), and Hindsight (91.4%).

---

### Search Strategy

**What it means:** The retrieval architecture under the hood — how the system blends keyword, vector, and structural signals.

**Why it matters:** Vector-only search hallucinates synonyms. Keyword-only misses paraphrases. The blend is what matters.

**M3 standing:** 3-Pillar Hybrid — FTS5 (BM25) + vector cosine + MMR diversity reranking. Explainable per-result scores via `memory_suggest`.

**Cohort context:** agentmemory's 6-signal hybrid and Hindsight's 4-stream neural model push further. Vector-only systems (Mem0) are simpler but lose precision on terminology-heavy queries.

---

### Local Fact Extraction

**What it means:** Distilling structured facts from raw conversational text — entirely on-device.

**Why it matters:** Raw text is noisy. Extracted facts are dense, queryable, and easier to refresh. Doing this locally preserves sovereignty.

**M3 standing:** SOTA — dedicated local SLM pipeline (LM Studio / Ollama / vLLM compatible).

**Cohort context:** Mastra OM's reflector matches M3 in capability but runs in the cloud. Mem0's LLM-powered extraction is strong on quality but breaks sovereignty. MemPalace's verbatim-only design skips extraction entirely.

---

### Architecture

**What it means:** The high-level system design and internal memory organization.

**Why it matters:** Architecture determines how the system scales, what kinds of queries it can answer, and how easy it is to extend.

**M3 standing:** 3-Tier (short / working / long-term) optimized for real agent lifecycles, with bitemporal logic threaded through every tier.

**Cohort context:** agentmemory's 6-signal hybrid is richer; MemPalace's spatial loci hierarchy is novel. Trade-off: more complex architectures cost more to maintain.

---

### Token Efficiency

**What it means:** How effectively the system reduces context-window usage and downstream LLM costs.

**Why it matters:** Every token saved is dollars saved and latency reduced. At scale, the difference is order-of-magnitude.

**M3 standing:** Strong — working-memory optimization plus 3-pillar retrieval keeps context tight without sacrificing recall coverage.

**Cohort context:** Mem0's reported ~90% savings is the leader; M3 is mid-pack but balances token efficiency against bitemporal richness. Heavy reranking (Hindsight) wastes tokens.

---

Last updated May 2026 — corrections welcome via [GitHub issue](https://github.com/skynetcmd/m3-memory/issues).
