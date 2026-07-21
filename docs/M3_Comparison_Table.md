# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Sovereign Memory Systems Comparison

> Last updated: 2026-07-21 (m3 row refreshed to 2026.7.21.0). Honest dimensional comparison of local-first / sovereign memory substrates. Competitor benchmark figures are vendor/author self-reported and verified against primary sources through 2026-06-23 (see the sourcing note under Retrieval & Extraction); they are not independently audited and may be stale — corrections welcome via [GitHub issue](https://github.com/skynetcmd/m3-memory/issues).

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

> ✅ **Retrieval accuracy — M3 leads:** the v3 core engine reaches **99.2% session-hit-rate (SHR) @ k=10** (496/500) on LongMemEval-S, **100% @ k=20** — the retrieval-accuracy metric most systems publish as their headline. That is state-of-the-art for a fully local-first, sovereign substrate, and it's a **conservative floor**: of the 4 scored misses, one is a [documented upstream gold-label error](https://github.com/xiaowu0162/LongMemEval/issues/37) and one is an abstention question where SHR is ill-defined — correcting for those puts true retrieval SHR@10 at **≥99.4%** ([details](#lme-s-score)). Cross-system scores are not perfectly controlled (different ingest, embedders, labeling), so we report ours transparently rather than claim a byte-identical head-to-head.
>
> ℹ️ **End-to-end QA accuracy (a different metric):** M3 scores **92.0%** judged-answer accuracy on LME-S with **no oracle metadata** (frontier answer model + the upstream gpt-4o judge, routing inferred at runtime) *(SHR=100% at k=20; QA is very model-dependent)*. Compare it only against other systems' *QA-accuracy* figures, not their retrieval/recall numbers.

---

## ▸ Sovereignty & Integrity (M3 strengths)

| Dimension | m3-memory<br>(2026.7.21.0) | agentmemory<br>(V4) | Chronos<br>(High/Res) | Hindsight | Mastra OM | Mem0 | Memento | MemPalace ⚠️ |
|---|---|---|---|---|---|---|---|---|
| **[Sovereignty (Main)](#sovereignty-main)** | 🛡️ **Full Sovereign** | 🛡️ **Full Sovereign** | ⚖️ **On-Prem** | ⚖️ **High Local** | ⚖️ **Hybrid** | 🔻 **Cloud-Tied** | ⚖️ **Config-Local** | 🛡️ **Full Verbatim** |
| ↳ *Data Residency* | 🏆 Local SQLite (or PostgreSQL primary) | ✅ Local SQLite | ✅ Local Files | ✅ Local Files | ⚖️ Postgres / Container | 🔻 Cloud DB | ✅ Local SQLite | ✅ Local SQLite |
| ↳ *Extraction Compute* | 🏆 Local SLM | ✅ Deterministic | ✅ ISO-Temporal | ✅ Neural / Local | 🔻 Cloud Reflector | 🔻 Cloud LLM | ⚖️ User-Defined | ❌ Verbatim only |
| ↳ *Telemetry / Audit* | 🏆 Zero / Bitemporal | ✅ Zero / Merkle | ✅ Event Logs | ✅ Internal | ⚖️ Usage Logs | 🔻 SaaS Metrics | ✅ Zero / Merkle | 🛡️ Total Dark |
| ↳ *Infrastructure* | 🏆 Native Python + pluggable storage (SQLite default / PostgreSQL primary; optional in-process Rust core) | ✅ Native Python | ⚖️ Linux / Python | ⚖️ Py / Services | 🔻 Docker stack | ✅ SDK / API | ✅ Native Python | ✅ Native Python |
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

## ▸ Retrieval & Extraction (M3 leads on retrieval accuracy)

| Dimension | m3-memory | agentmemory | Chronos | Hindsight | Mastra OM | Mem0 | Memento | MemPalace |
|---|---|---|---|---|---|---|---|---|
| **[Retrieval SHR@10](#lme-s-score)** | **99.2%** (🏆 #1)<br>100% @ k=20 | — | — | — | — | — | — | 96.6% R@5 ⚠️ᵍ |
| **[Published LME-S headline](#lme-s-score)**<br>*(metric varies by vendor — see sourcing note)* | **92.0%** QA<br>(no oracle; SHR=100% @ k=20) | 96.2%ᵃ | 95.6%ᵇ | 91.4%ᶜ | 94.9%ᵈ | ~94% / ~67%ᵉ | 90.8%ᶠ | 96.6% R@5 ⚠️ᵍ |
| **[Search Strategy](#search-strategy)** | ✅ **3-Pillar Hybrid** | 🏆 **6-Signal Hybrid** | ⚖️ **Dual-Index** | 🏆 **4-Stream Neural** | ✅ **Reflective** | ✅ **Vector-only** | ✅ **Compositional** | ⚖️ **Spatial-palace** |
| **[Local Fact Extraction](#local-fact-extraction)** | 🏆 **Local SLM** | ✅ **Deterministic** | ✅ **ISO-Temporal** | ✅ **Entity-centric** | 🏆 **Reflector** | ✅ **LLM-Powered** | ✅ **Entity-Res.** | ❌ **Verbatim only** |
| **[Token Efficiency](#token-efficiency)** | 🏆 **Lazy tools + low-K** (1.8% window at startup) | ✅ **Signal Filter** | ✅ **Event-Pruned** | 🔻 **Heavy Rerank** | ✅ **Cache-Stable** | 🏆 **~90% Savings** | ✅ **Stores raw text** | ⚖️ **Stores raw text** |

> **On the two retrieval rows.** *Retrieval SHR@10* is a like-for-like, retrieval-only metric (session-hit-rate: did a gold-session turn land in the top-k?). M3's **99.2% @ k=10 / 100% @ k=20** comes from the v3 core engine on raw turns — hybrid FTS5 + BGE-M3 vector + MMR, no knowledge graph, no oracle metadata ([report](../benchmarks/longmemeval/LME-S_Benchmarking_Report.md)). The *Published LME-S headline* row collects each vendor's top-line number **as they report it** — but those mix metrics (QA accuracy vs. recall@k) and use different answer models, judges, and ingest pipelines, so they are **not** a controlled head-to-head. M3's own headline there is **QA accuracy (92.0%, no oracle)**, which is answer-model-dependent and should only be compared against other systems' QA-accuracy figures.

> **Competitor figure sourcing** (all vendor/author self-reported, none independently audited; verified by us **2026-06-22**). Each system uses a **different answer model**, so even the like-metric (QA-accuracy) numbers are *not* a controlled ranking — they partly reflect the reader LLM, not memory quality.
>
> **⚖️ Judge-prompt provenance (verified by us 2026-06-23).** A LongMemEval QA score is only comparable across systems if they grade with the **same judge**. We pulled each system's judge prompt from primary sources and diffed it against the upstream Wu et al. judge ([`evaluate_qa.py`](https://github.com/xiaowu0162/LongMemEval/blob/main/src/evaluation/evaluate_qa.py) — six per-question-type templates, each ending "Answer yes or no only", no chain-of-thought, no leniency bias). **m3 uses the unmodified upstream judge.** Summary: m3, **agentmemory**, and **Mastra OM** grade with the **exact upstream judge** (mutually comparable); **Mem0** and **Memento** use self-authored, **more-lenient** judges (scores biased upward — not apples-to-apples); **Chronos** and **Hindsight** don't publish their LongMemEval judge (unverifiable). A more lenient judge inflates *every* system it grades, so comparing a strict-judge number against a lenient-judge one is invalid. Per-system detail in each footnote.
>
> - **ᵃ agentmemory — 96.2% QA** (481/500), Claude Opus 4.6 answerer, GPT-4o judge. **Judge: upstream Wu, exact** (5/6 templates byte-identical; the temporal template only *adds* a `Reference Date:` line — a stricter date-grounded check, not leniency). The 96.2% is driven by heavy answerer-side prompt tuning, not a loosened judge. Source: [github.com/JordanMcCann/agentmemory](https://github.com/JordanMcCann/agentmemory). *Verified.*
> - **ᵇ Chronos (High) — 95.6% QA**, Claude Opus 4.6, LongMemEval judge. **Judge: unverifiable** — the paper says it "implement[s] LongMemEval's LLM judge … routing to a specific prompt based on the question's category" but shows no prompt text, names no judge model, and releases no code (it even flags "LLM-as-judge variability"). Source: arXiv preprint [2603.16862](https://arxiv.org/abs/2603.16862) (self-reported, not peer-reviewed). *Figure verified; judge unconfirmed.*
> - **ᶜ Hindsight — 91.4% QA**, Gemini 3 Pro backbone. **Judge: unverifiable** — the public [hindsight-benchmarks](https://github.com/vectorize-io/hindsight-benchmarks) repo ships LongMemEval *results* but **no LongMemEval judge code** (the only judge it ships is for LoCoMo, and that one is heavily lenient — single prompt, explicit "be generous", CoT JSON). The grader behind 91.4% is not published. Source: [github.com/vectorize-io/hindsight-benchmarks](https://github.com/vectorize-io/hindsight-benchmarks). *Figure verified; judge unconfirmed.*
> - **ᵈ Mastra OM — 94.9% QA** (94.87%), gpt-5-mini answerer, GPT-4o judge. **Judge: upstream Wu, exact** — their eval code (`explorations/longmemeval/src/evaluation/longmemeval-metric.ts`) carries the comment "copied EXACTLY from the official LongMemEval benchmark … Do not modify these prompts"; the six templates match verbatim. Source: [mastra.ai/research/observational-memory](https://mastra.ai/research/observational-memory). *Verified.*
> - **ᵉ Mem0 — figure disputed.** Mem0's *current* token-efficient algorithm self-reports **~94%** (94.4% on its [research page](https://mem0.ai/research), 94.8% on its [repo](https://github.com/mem0ai/mem0)); independent and older evaluations put earlier Mem0 at **~67%** ([arXiv 2504.19413](https://arxiv.org/abs/2504.19413)). As of 2026-06-22, Mem0's own pages no longer state the answer model behind the ~94% figure (previously attributed to gpt-4o) — treat the reader model as undisclosed. **Judge: MODIFIED, more lenient** — Mem0's [memory-benchmarks](https://github.com/mem0ai/memory-benchmarks) uses a *single unified* judge ("Judge by MEANING, not exact words"; explicit pro-yes bias "you have a tendency to say 'no' too quickly"; superset answers + number/date hedging accepted; chain-of-thought in `<judge_thinking>` tags) — materially looser than the paper judge, so its ~94% is **not** comparable to strict-judge numbers like m3's 92.0%. *(Our prior "89.1%" matched no source and has been corrected.)*
> - **ᶠ Memento — 90.8% QA** but in the **oracle / evidence-only (no-distractor) setting**, *not* standard LongMemEval-S; the harder S-setting is unpublished. Not apples-to-apples with the columns above. **Judge: MODIFIED, more lenient** — rewritten prompts (not the upstream strings) adding explicit leniency clauses ("Minor phrasing differences are acceptable", "off-by-one errors are acceptable", "need not cover every rubric point"); the same lenient judge grades the oracle number. Source: [github.com/shane-farkas/memento-memory](https://github.com/shane-farkas/memento-memory). *Partially verified — setting differs, judge loosened.*
> - **ᵍ ⚠️ MemPalace — do not treat as comparable.** (1) 96.6% is **R@5 recall, not QA accuracy** — a different metric; as of 2026-06-22 the repo also co-advertises higher hybrid-v4 R@5 figures (98.4%, ≥99% with LLM reranking) on a smaller held-out set, with a benchmark release **dated 2026-06-23 — in the future relative to this verification**, which only deepens the comparability concern. (2) The figure is disputed: an independent critical analysis ([arXiv 2604.21284](https://arxiv.org/abs/2604.21284)) attributes the 96.6% to **ChromaDB's embeddings and verbatim storage, not MemPalace's spatial "memory-palace" architecture**, and finds its AAAK compression mode is **lossy** — dropping the project's own R@5 from 96.6% (verbatim) to 84.2% (AAAK), contradicting the original "zero information loss" marketing. The project also carries **scam allegations** ([repo issue #618 "POSSIBLE SCAM REPO"](https://github.com/MemPalace/mempalace/issues/618) — now **closed as "not planned"** but still alleging fraud; [HN discussion](https://news.ycombinator.com/item?id=47684922)) plus **malware-impostor domains** (only `github.com/MemPalace/mempalace` is official; `.tech`/`.net` variants are flagged malware — **do not visit**). Listed here only to flag, not endorse.

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

- **Pure retrieval accuracy is paramount** — M3 leads: **99.2% SHR@10 / 100% @ k=20** on LME-S, state-of-the-art for a local-first substrate. (M3's *end-to-end QA accuracy* of 92.0% — no oracle metadata — is a separate, answer-model-dependent metric — don't confuse the two.)
- **You need extreme token compression** — Mem0 reports ~90% context savings. M3's working-memory model is good but not as aggressive.
- **You only need verbatim recall, no extraction** — M3 already does verbatim recall: content is stored exactly as written, never altered in place, and always retrievable byte-for-byte (a plain vector store like ChromaDB, or M3 with enrichment disabled, also covers the pure case). Unlike a verbatim-only store, M3 *also* keeps the verbatim text of superseded facts — corrections close-and-link rather than overwrite — so "what did we record, exactly?" survives across edits.
- **Heavy neural reranking is acceptable** — Hindsight's 4-stream architecture wins on rich retrieval if you can absorb the latency cost.
- **You're committed to a Docker-first ops model** — Mastra OM fits cleanly into containerized stacks.

M3 leads on retrieval accuracy **and** ships **sovereignty, bitemporal correctness, and a small auditable codebase** — that combination is what M3 is built for, and the table above is the receipt.

For the developer-tool decision (Mem0, Letta, Zep, LangChain Memory), see the [developer-facing comparison guide](COMPARISON.md).

---

## Dimension glossary & analysis

> **SOTA** = state-of-the-art.

### Sovereignty (Main)

**What it means:** Independence from cloud services, telemetry, and external dependencies for normal operation.

**Why it matters:** If your data *can't* leave the machine — for legal, contractual, or personal reasons — every external dependency is a compliance risk and an attack surface.

**M3 standing:** Full Sovereign. Local SQLite, local SLM extraction, zero telemetry, native Python with an optional in-process Rust acceleration core (`m3_core_rs`) that ships as a local wheel — no service, no daemon, graceful pure-Python fallback — runs on a laptop or in an air-gapped enclave with the same code path. The Rust core gives large per-operation wins where it matters (up to ~846× on packed MMR rerank, ~97–178× on packed batch-cosine, 11–15× redaction, 1.4–10× on FTS/token-Jaccard; [benchmarks](OXIDATION_BENCHMARKS.md)) without adding any external dependency.

**Sub-dimensions:**
- **Data Residency:** Local SQLite — single file, portable, inspectable (or PostgreSQL as the primary backend for a shared/server store).
- **Extraction Compute:** Local SLM via LM Studio / Ollama / vLLM — no data egress.
- **Telemetry / Audit:** Zero by default; bitemporal log gives auditability without phoning home.
- **Infrastructure:** `pip install` — no Docker, no services, no daemons.

**Cohort context:** M3 and agentmemory lead on sovereignty (fully local, zero-telemetry). Cloud-tied systems (Mem0) can't reach this tier without significant rework. Mastra OM's Docker stack is more dependent than M3's plain-Python install. (MemPalace's local-storage claims aren't independently verifiable — see the ⚠️ scam caveat in the Retrieval section.)

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

**M3 standing:** Bi-directional delta sync via PostgreSQL — set one env var, your memories follow you across devices.

**Cohort context:** Cloud-native systems (Mem0) deliver sync trivially but at the cost of sovereignty. MemPalace requires manual sync.

---

### LME-S Score

**What it means:** [LongMemEval-S](https://github.com/xiaowu0162/LongMemEval), a 500-question benchmark for long-horizon conversational memory. Two different things get measured on it, and vendor pages routinely blur them:
- **Retrieval accuracy (SHR / recall@k)** — did the system surface a turn from the correct evidence session within the top-k results? Purely a property of the memory layer; no answer model involved.
- **End-to-end QA accuracy** — given the retrieved context, did a *separate answer model* produce a judged-correct answer? This number rises and falls with the answer model (Opus, gpt-5-mini, etc.) and the judge, so it measures the whole pipeline, not the memory layer alone.

**Why it matters:** A retrieval layer that can't find what's there is a liability — so retrieval accuracy is the metric that actually isolates the memory system. The benchmark also doesn't measure sovereignty, integrity, undo, or compliance: a 96% QA score sourced from a cloud LLM tells you nothing about whether your data left the machine.

**M3 standing — retrieval:** **99.2% session-hit-rate @ k=10 (496/500), 100% @ k=20** with the v3 core engine (raw turns, hybrid FTS5 + BGE-M3 vector + MMR, no knowledge graph, no oracle metadata). That is state-of-the-art for a fully local-first substrate — the right session turn is the #1 result for ~92% of questions and in the top-10 for >99%. Source: [LME-S Benchmarking Report](../benchmarks/longmemeval/LME-S_Benchmarking_Report.md).

> **99.2% is a conservative floor.** We measure against `longmemeval_s_cleaned`, which has documented upstream annotation errors. Of our 4 scored misses at k=10: one (`eac54add`) is a [confirmed gold-session mislabel](https://github.com/xiaowu0162/LongMemEval/issues/37) (the labeled evidence session is ~18 days off from the real one), and one (`60bf93ed_abs`) is an [abstention question](https://github.com/xiaowu0162/LongMemEval/issues/20) — the gold "evidence" is a deliberate distractor session, so SHR rewards retrieving the lure and penalizes correctly declining it; the metric is ill-defined there. Excluding the 30 abstention questions, SHR@10 is **99.4% (467/470)**; correcting the confirmed mislabel as well, **99.6%**. Only 2 of 500 are arguably genuine retrieval misses, both temporal-distractor cases. We report the strict 99.2% as the headline and note the floor rather than quoting the higher corrected figures.

> ⚠️ **Don't confuse these two numbers, and don't quote the old one.** M3's **recall** (the metric that isolates the memory layer) is **99.2% SHR@10 / 100% @ k=20 — this leads.** The **92.0%** below is a *different* metric: end-to-end **QA accuracy**, which depends heavily on the answer model. And **89.0% is superseded** — it was an earlier oracle-routed QA run, replaced by the 92.0% no-oracle figure. If you've read "~89% recall" anywhere, it is wrong on both counts: it's a retired *QA* number, not recall, and M3's actual recall is 99.2%.

**M3 standing — QA accuracy:** **92.0%** with **no oracle metadata** *(SHR=100% at k=20; QA is very model-dependent)* — the end-to-end figure, scored by the upstream gpt-4o judge, with all routing inferred from the question text at runtime (the earlier oracle-routed configuration scored 89.0%, now superseded). Because it rises and falls with whatever answer model reads the retrieved context, compare it only against other systems' QA-accuracy numbers, never against their recall figures. *(Methodology: the specific answer model used for the 92.0% run is recorded in the [LME-S Benchmarking Report](../benchmarks/longmemeval/LME-S_Benchmarking_Report.md).)*

**Cohort context (read the metric labels carefully):** the published top-line numbers — agentmemory (96.2% QA), Chronos (95.6% QA), Mastra OM (94.9% QA, gpt-5-mini), Hindsight (91.4% QA) — are all **QA accuracy** but use **different answer models** (Opus 4.6 / gpt-5-mini / Gemini 3 Pro), so they are *not* a controlled ranking. Two cells are not comparable at all: **MemPalace's 96.6% is R@5 recall** (and the project is scam-flagged — see ⚠️ note above), and **Memento's 90.8%** is an oracle/no-distractor setting, not standard LME-S. Per-figure sources and caveats are in the [sourcing note](#-retrieval--extraction-m3-leads-on-retrieval-accuracy) under Retrieval & Extraction. On the one like-for-like, retrieval-only metric (SHR@k), M3's v3 engine leads.

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

**M3 standing:** SOTA — dedicated local SLM pipeline (LM Studio / Ollama / vLLM compatible) feeding a typed knowledge graph. The entity-graph layer ships a stock entity-type and predicate vocabulary and is **user-configurable**: point `M3_ENTITY_VOCAB_YAML` at your own profile to swap or extend the vocabulary for your domain, with no code changes.

**Cohort context:** Mastra OM's reflector matches M3 in capability but runs in the cloud. Mem0's LLM-powered extraction is strong on quality but breaks sovereignty. MemPalace's verbatim-only design skips extraction entirely — but note that "verbatim recall" is not a reason to prefer it over M3: M3 stores content verbatim and never alters it in place, so it delivers byte-for-byte recall *plus* extraction, contradiction handling, and preserved verbatim history across corrections. A verbatim-only store gives you the raw text but loses the earlier wording the moment a fact is updated.

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

**M3 standing:** Strong — lazy tool-loading keeps the context window small, and SOTA retrieval means a low top-K (versus the typical K=20–50) is enough: less noise, and the correct answer tends to surface on the first search rather than in follow-up queries.

**Cohort context:** Mem0 reports ~90% context savings. M3 attacks token cost from two directions instead — lazy-loading the tool surface, and retrieving accurately at low K so fewer results carry the answer — while preserving bitemporal richness. Heavy reranking (Hindsight) wastes tokens.

---

Last updated 2026-07-21 (m3 row) — competitor figures verified through 2026-06-23; corrections welcome via [GitHub issue](https://github.com/skynetcmd/m3-memory/issues).
