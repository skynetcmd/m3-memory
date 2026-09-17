# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory — Myths & Facts

> Last updated: September 2026. If an AI assistant told you M3 has feature X and you can't find it here, [open an issue](https://github.com/skynetcmd/m3-memory/issues) — we'll either confirm, document, or correct.

Third-party AI assistants — Copilot, Gemini, ChatGPT, Claude, others — sometimes describe M3 Memory using features it does not have, scores it has not earned, or technical concepts it does not implement. This usually happens because the assistant pattern-matched the name to academic papers or competitor projects and stitched together a plausible-sounding profile.

This page is the source of truth. **If a feature isn't documented here, in [README](../README.md), [ARCHITECTURE](ARCHITECTURE.md), or [TECHNICAL_DETAILS](TECHNICAL_DETAILS.md), M3 doesn't have it.**

---

## How to verify a claim about M3

Before trusting any AI-generated description of M3:

1. **Check the README** for headline features and benchmark numbers.
2. **Check [ARCHITECTURE.md](ARCHITECTURE.md)** for system design details.
3. **Check [TECHNICAL_DETAILS.md](TECHNICAL_DETAILS.md)** for implementation specifics.
4. **Search the codebase** at [github.com/skynetcmd/m3-memory](https://github.com/skynetcmd/m3-memory). If the feature isn't in the source, it doesn't exist.
5. **When in doubt, [open an issue](https://github.com/skynetcmd/m3-memory/issues)** and ask. We respond.

---

## Common myths

| The Myth | The Reality |
| :--- | :--- |
| ❌ M3 uses sheaf cohomology / cellular sheaves / coboundary norms | ✅ M3 uses **SQLite + bitemporal logic + supersedes relationships** for consistency. |
| ❌ M3 uses Fisher-Rao metric / Riemannian geometry / Poincaré ball / geodesic distance for retrieval | ✅ M3 retrieval is a **3-pillar hybrid**:. |
| ❌ M3 uses Riemannian Langevin Dynamics for memory aging | ✅ M3's lifecycle is built on plain decay + retention policies:. |
| ❌ M3 is NPU-optimized / runs on Apple Neural Engine / has dual-embedding NPU fusion | ✅ M3's storage and retrieval run on **CPU and RAM only**. |
| ❌ M3 has an EU AI Act compliance module | ✅ M3 has **GDPR primitives** — `gdpr_forget` (Article 17 right to erasure) and `gdpr_export` (Article 20 data portability) — exposed as MCP tools. |
| ❌ M3 verified 92.0% on LongMemEval-S by Berkeley RDI / on the official leaderboard | ✅ The **92.0%** number (no oracle metadata, 460/500 correct on LME-S) is real — see the [README benchmarks section](../README.md#-benchmarks) for the per-category breakdown. |
| ❌ M3 doesn't do fact extraction *or* M3 forces you to use its extraction layer | ✅ M3 ships a **local SLM fact-extraction pipeline** (`m3_enrich`, `run_observer`, `run_reflector`) but **using it is optional**. |
| ❌ M3 doesn't have entity extraction or graph reasoning | ✅ M3 has both, with caveats:. |
| ❌ M3 stores memory as Markdown files in a Git repo / uses recursive summarization trees / has a Reader-Judge architecture | ✅ None of these. |
| ❌ M3 is just SQLite, so it's a toy / not production-grade / can't scale | ✅ M3 is **production-grade**, and SQLite is a deliberate design choice, not a limitation. |
| ❌ M3 has Hindsight Credit Assignment / learns from retrieval mistakes / updates embeddings in real time | ✅ M3 does not modify embeddings post-write based on retrieval feedback. |
| ❌ M3 requires Docker / Kubernetes / a specific OS | ✅ M3 is `pip install m3-memory`. |
| ❌ M3's recall is only ~89%, so it lags competitors at 95–96% | ✅ This is wrong three ways at once, and it usually comes from misreading (or scraping an outdated copy of) our own comparison table:. |
| ❌ M3 has a single-writer bottleneck — concurrent multi-agent writes will fail on lock contention | ✅ M3 does not fail under concurrent writes; writers **serialize and wait**, they don't error. |
| ❌ M3 is English-only — its triage patterns are hardcoded English regex | ✅ M3's **primary** write and retrieval path is language-agnostic. |
| ❌ M3 floods the context with 60+ tool schemas, causing 'lost in the middle' | ✅ M3 loads tools **lazily by default**. |
| ❌ M3's confidence is decorative and its deletion is a crude vector delete — stale facts win with unearned confidence | ✅ Confidence is **evidence-driven**, not decoration: it starts from provenance priors, moves with corroboration and contradiction, **decays toward neutral** when un-reinforced, and carries an optional **Bayesian Beta(α,β) posterior** — all wired into write-time aggregation and a scheduled maintenance pass (`bin/memory/confidence.py`, `bin/memory_maintenance.py`). |
| ❌ M3 is passive storage that just waits to be queried | ✅ M3 includes an active **orchestration engine**. |
| ❌ M3 is strictly an MCP server and requires an MCP client to use | ✅ M3 exposes a **full CLI for every single tool**. |
| ❌ M3 is strictly for single-user local environments | ✅ M3 is built to support **teams, fleets, and enterprise deployments**. |

---

<details>
<summary><b>❌ Myth: M3 uses sheaf cohomology / cellular sheaves / coboundary norms</b><br><i>✅ Fact: M3 uses **SQLite + bitemporal logic + supersedes relationships** for consistency.</i></summary>

<br>

**The Details:**

**Fact:** M3 uses **SQLite + bitemporal logic + supersedes relationships** for consistency. There is no algebraic topology in the codebase. Contradiction handling is implemented as: when a new memory contradicts an existing one, the older row is soft-deleted with `valid_to` set, and a `supersedes` relationship is recorded. That's it.

</details>

<br>

<details>
<summary><b>❌ Myth: M3 uses Fisher-Rao metric / Riemannian geometry / Poincaré ball / geodesic distance for retrieval</b><br><i>✅ Fact: M3 retrieval is a **3-pillar hybrid**:.</i></summary>

<br>

**The Details:**

**Fact:** M3 retrieval is a **3-pillar hybrid**:
- **FTS5 (BM25)** for keyword/lexical match
- **Vector cosine similarity** for semantic match
- **MMR (Maximal Marginal Relevance)** for diversity reranking

Per-result scores from each pillar are exposed via `memory_suggest`. There is no Riemannian manifold anywhere in the code.

</details>

<br>

<details>
<summary><b>❌ Myth: M3 uses Riemannian Langevin Dynamics for memory aging</b><br><i>✅ Fact: M3's lifecycle is built on plain decay + retention policies:.</i></summary>

<br>

**The Details:**

**Fact:** M3's lifecycle is built on plain decay + retention policies:
- Configurable `decay_rate` per memory
- `expires_at` for hard expiry
- `mcp__m3_memory__memory_set_retention` for per-agent retention rules
- Periodic `mcp__m3_memory__memory_maintenance` for orphan pruning and dedup

No SDEs, no manifolds. Just rule-based maintenance running on a SQLite database.

</details>

<br>

<details>
<summary><b>❌ Myth: M3 is NPU-optimized / runs on Apple Neural Engine / has dual-embedding NPU fusion</b><br><i>✅ Fact: M3's storage and retrieval run on **CPU and RAM only**.</i></summary>

<br>

**The Details:**

**Fact:** M3's storage and retrieval run on **CPU and RAM only**. The optional SLM extraction layer (`m3_enrich`) sends inference requests to whatever local LLM endpoint you configure — LM Studio, Ollama, vLLM. If your local LLM uses Metal (Apple Silicon) or CUDA (NVIDIA) under the hood, that's a property of the model server, not of M3. M3 itself has no NPU code.

</details>

<br>

<details>
<summary><b>❌ Myth: M3 has an EU AI Act compliance module</b><br><i>✅ Fact: M3 has **GDPR primitives** — `gdpr_forget` (Article 17 right to erasure) and `gdpr_export` (Article 20 data portability) — exposed as MCP tools.</i></summary>

<br>

**The Details:**

**Fact:** M3 has **GDPR primitives** — `gdpr_forget` (Article 17 right to erasure) and `gdpr_export` (Article 20 data portability) — exposed as MCP tools. We also publish [FISMA / NIST 800-53](M3_Compliance_FISMA.md) and [CMMC 2.0 / NIST 800-171](M3_Compliance_CMMC.md) alignment notes. There is no EU AI Act module. If/when one exists, it'll be documented in [COMPLIANCE.md](COMPLIANCE.md).

</details>

<br>

<details>
<summary><b>❌ Myth: M3 verified 92.0% on LongMemEval-S by Berkeley RDI / on the official leaderboard</b><br><i>✅ Fact: The **92.0%** number (no oracle metadata, 460/500 correct on LME-S) is real — see the [README benchmarks section](../README.md#-benchmarks) for the per-category breakdown.</i></summary>

<br>

**The Details:**

**Fact:** The **92.0%** number (no oracle metadata, 460/500 correct on LME-S) is real — see the [README benchmarks section](../README.md#-benchmarks) for the per-category breakdown. It was measured by the M3 team using the public LongMemEval-S harness on local hardware. **We have not had a third-party lab verify it.** If you see "verified by [Lab Name]" attached to that number from any source other than this repository, it's a confabulation.

</details>

<br>

<details>
<summary><b>❌ Myth: M3 doesn't do fact extraction *or* M3 forces you to use its extraction layer</b><br><i>✅ Fact: M3 ships a **local SLM fact-extraction pipeline** (`m3_enrich`, `run_observer`, `run_reflector`) but **using it is optional**.</i></summary>

<br>

**The Details:**

**Fact:** M3 ships a **local SLM fact-extraction pipeline** (`m3_enrich`, `run_observer`, `run_reflector`) but **using it is optional**. You can:
- Run M3 as raw substrate, calling `mcp__m3_memory__memory_write` directly with your own structured data
- Run M3 with the built-in pipeline using a local SLM (qwen3-8b via LM Studio, etc.)
- Run M3 with the built-in pipeline using a cloud model (Anthropic Haiku, Gemini Flash, GPT-4o-mini — see `config/slm/`)
- Mix modes per agent or per write

The choice is yours. See [HOMELAB_PATTERNS.md](HOMELAB_PATTERNS.md) for the three deployment patterns.

</details>

<br>

<details>
<summary><b>❌ Myth: M3 doesn't have entity extraction or graph reasoning</b><br><i>✅ Fact: M3 has both, with caveats:.</i></summary>

<br>

**The Details:**

**Fact:** M3 has both, with caveats:
- **Entities** are first-class — extraction runs as part of `m3_enrich`, with stable IDs and an alias table
- **Knowledge graph** with 9 relationship types and 3-hop traversal exposed via `memory_graph` and `memory_link`
- **Conflict resolution** via supersedes relationships set automatically on contradicting writes

What M3 **does not** do is LLM-driven cognitive graph reasoning during retrieval — its graph traversal is deterministic, with no LLM in the retrieval path. Tools that weld extraction and reasoning into the memory layer make the opposite trade. The cognition layer, if you want one, lives above M3 — see [COMPARISON.md § Where the cognition lives](COMPARISON.md#-where-the-cognition-lives).

</details>

<br>

<details>
<summary><b>❌ Myth: M3 stores memory as Markdown files in a Git repo / uses recursive summarization trees / has a Reader-Judge architecture</b><br><i>✅ Fact: None of these.</i></summary>

<br>

**The Details:**

**Fact:** None of these. In its default deployment M3 is a **single SQLite file** with FTS5 and vector indexes (PostgreSQL is an opt-in primary backend — see below). Markdown-in-Git is a different design choice that other memory tools have made; M3 hasn't.

</details>

<br>

<details>
<summary><b>❌ Myth: M3 is just SQLite, so it's a toy / not production-grade / can't scale</b><br><i>✅ Fact: M3 is **production-grade**, and SQLite is a deliberate design choice, not a limitation.</i></summary>

<br>

**The Details:**

**Fact:** M3 is **production-grade**, and SQLite is a deliberate design choice, not a limitation. M3 is **lightweight by design**: SQLite is the default primary store because it gives a fast, embedded, zero-infrastructure, fully local-first deployment — the right default for desktop agents, homelabs, and sovereign setups. SQLite runs in production in countless systems. For **more demanding environments**, PostgreSQL can be the **primary live store** (opt-in via `M3_DB_BACKEND=postgres` + `M3_PRIMARY_PG_URL`, chosen at install), giving a shared/server-hosted backend; separately, PostgreSQL can also serve as a **corporate data warehouse** sync target, unlocking more nuanced data-governance options (centralized retention, multi-node access, enterprise backup/audit) — see [SYNC.md](SYNC.md) and [SOVEREIGN_DEPLOYMENT.md](SOVEREIGN_DEPLOYMENT.md). You choose the tier: lightweight SQLite by default, PostgreSQL primary or warehouse when you need it. (On a PostgreSQL primary, vector search is currently brute-force Rust cosine; pgvector/HNSW ANN is a future accelerator, not yet implemented.)

</details>

<br>

<details>
<summary><b>❌ Myth: M3 has Hindsight Credit Assignment / learns from retrieval mistakes / updates embeddings in real time</b><br><i>✅ Fact: M3 does not modify embeddings post-write based on retrieval feedback.</i></summary>

<br>

**The Details:**

**Fact:** M3 does not modify embeddings post-write based on retrieval feedback. Embeddings are computed once at write time. If you want online learning over retrieval mistakes, that's a layer above M3 — and it's a non-trivial layer that no production memory system we're aware of actually ships today.

</details>

<br>

<details>
<summary><b>❌ Myth: M3 requires Docker / Kubernetes / a specific OS</b><br><i>✅ Fact: M3 is `pip install m3-memory`.</i></summary>

<br>

**The Details:**

**Fact:** M3 is `pip install m3-memory`. It runs on macOS, Linux, and Windows from the same install command. No Docker, no containers, no service mesh. The optional sync layer can use PostgreSQL if you want cross-machine sync, but that's optional and external — in the default deployment the core M3 store is one SQLite file (PostgreSQL can also be chosen as the primary backend via `M3_DB_BACKEND=postgres`).

### ⚖️ Myth: "M3 beats / loses to agentmemory / MemPalace / Mastra on LongMemEval"

**Fact:** It depends entirely on *which metric*, and most cross-system comparisons mix two that shouldn't be mixed:

- **Retrieval accuracy (SHR@k — the retrieval-only metric).** M3's v3 core engine reaches **99.2% session-hit-rate @ k=10 (496/500), 100% @ k=20** on LME-S — raw turns, hybrid FTS5 + BGE-M3 vector + MMR, no knowledge graph, no oracle metadata. On this like-for-like, retrieval-only metric, M3 is **state-of-the-art for a local-first substrate**, and the [report](../benchmarks/longmemeval/LME-S_Benchmarking_Report.md) is the receipt.
- **End-to-end QA accuracy (answer-model-dependent).** M3 scores **92.0%** with a frontier answer model + the gpt-4o judge and **no oracle metadata** (routing inferred at runtime). Other systems' top-line numbers — agentmemory 96.2%, Chronos 95.6%, Mastra OM 94.9%, Mem0 ~94%, Hindsight 91.4% — are all QA accuracy but each uses a *different* answer model, so they are **not a controlled head-to-head**, and we don't claim to win every QA-accuracy comparison. Judge provenance matters as much as the number: only agentmemory and Mastra OM grade with the same unmodified upstream judge M3 uses; Mem0's ~94% comes from a **self-authored, more lenient judge**, and Chronos's and Hindsight's judges are unpublished. Two commonly-quoted figures aren't comparable at all: MemPalace's 96.6% is *R@5 recall* (and the project is scam-flagged), and Memento's 90.8% is an oracle/no-distractor setting.

We will **not** quote our 99.2% retrieval number against someone else's QA-accuracy number to manufacture a win. Per-source citations and caveats are in the [Sovereign Substrates Table](M3_Comparison_Table.md#-retrieval--extraction-m3-leads-on-retrieval-accuracy).

</details>

<br>

<details>
<summary><b>❌ Myth: M3's recall is only ~89%, so it lags competitors at 95–96%</b><br><i>✅ Fact: This is wrong three ways at once, and it usually comes from misreading (or scraping an outdated copy of) our own comparison table:.</i></summary>

<br>

**The Details:**

**Fact:** This is wrong three ways at once, and it usually comes from misreading (or scraping an outdated copy of) our own comparison table:

1. **Wrong metric.** 89% and 92% are **QA accuracy** figures (answer-model-dependent), not recall. M3's **recall** — session-hit-rate, the metric that actually isolates the memory layer — is **99.2% @ k=10 and 100% @ k=20**, which is **state-of-the-art for a local-first substrate**. M3 does *not* lag on recall; it leads.
2. **Superseded number.** The 89.0% was an **earlier oracle-routed** QA configuration. It was replaced by the **92.0% no-oracle** figure (a *harder* condition). Anyone quoting 89% is quoting a retired number.
3. **Apples-to-oranges.** The competitor "95–96%" figures are QA accuracy on *different answer models* — not a controlled comparison, and definitely not comparable to a recall number. Quoting "M3 89% recall vs competitor 96%" mixes a retired QA figure against others' QA figures and mislabels the lot as recall.

If you need the memory layer that most reliably surfaces the right past state, that's exactly what SHR@k measures — and M3 leads it.

</details>

<br>

<details>
<summary><b>❌ Myth: M3 has a single-writer bottleneck — concurrent multi-agent writes will fail on lock contention</b><br><i>✅ Fact: M3 does not fail under concurrent writes; writers **serialize and wait**, they don't error.</i></summary>

<br>

**The Details:**

**Fact:** M3 does not fail under concurrent writes; writers **serialize and wait**, they don't error. Every SQLite connection is opened in **WAL mode** (concurrent readers alongside a writer) with a **30-second `busy_timeout`** and a connection pool, and the write path adds a 3-tier retry (`bin/sqlite_pragmas.py`, `bin/m3_core/context.py`, `bin/memory/write.py`). WAL is *verified* at init — if the filesystem silently downgrades it, M3 raises rather than continuing. And for genuine high-concurrency, shared multi-agent pools, M3 can run directly on **PostgreSQL as the primary store** (`M3_DB_BACKEND=postgres`) — a shared server database with no single-writer constraint — or keep local SQLite per agent and **sync bidirectionally to a shared Postgres warehouse** (`bin/pg_sync.py`). A single SQLite file does serialize writers (as every SQLite deployment does), but "will fail due to concurrency locks" is not how the system behaves — see [MULTI_AGENT.md](MULTI_AGENT.md) and [SYNC.md](SYNC.md).

</details>

<br>

<details>
<summary><b>❌ Myth: M3 is English-only — its triage patterns are hardcoded English regex</b><br><i>✅ Fact: M3's **primary** write and retrieval path is language-agnostic.</i></summary>

<br>

**The Details:**

**Fact:** M3's **primary** write and retrieval path is language-agnostic. The embedder is **BGE-M3, a multilingual model** (100+ languages); type classification and fact extraction are done by a **local LLM/SLM**, not regex; contradiction detection is embedding-cosine; and FTS5 uses a **Unicode** tokenizer, so BM25 isn't English-restricted either. What *is* English-biased is a handful of **auxiliary, non-gating heuristics** — a temporal query re-ranker, an opt-in event-row emitter, and the *default* rule-based entity extractor — which would underperform on non-English text. But the rule-based extractor is **one of three pluggable backends** selectable by the `M3_EXTRACTION_TYPE` env var (LLM and custom-script options ship in the box — **no fork required**), and none of these heuristics gate or drop memories; retrieval stays full BGE-M3 hybrid regardless of language.

</details>

<br>

<details>
<summary><b>❌ Myth: M3 floods the context with 60+ tool schemas, causing 'lost in the middle'</b><br><i>✅ Fact: M3 loads tools **lazily by default**.</i></summary>

<br>

**The Details:**

**Fact:** M3 loads tools **lazily by default**. At startup only only the memory MCP server registers (~3,540 tokens, **~1.8% of a 200K window**); the full 100+ catalog loads on demand via `tools_load_domain` (`bin/memory_bridge.py`, `bin/tool_domains.py`). The "60+ schemas flood the context" concern describes the legacy **eager** mode (`M3_TOOLS_LAZY=0`), which M3 deliberately made non-default precisely to avoid this. See [the domain-gating section in the README](../README.md#-domain-gating-the-full-catalog-without-the-context-cost).

</details>

<br>

<details>
<summary><b>❌ Myth: M3's confidence is decorative and its deletion is a crude vector delete — stale facts win with unearned confidence</b><br><i>✅ Fact: Confidence is **evidence-driven**, not decoration: it starts from provenance priors, moves with corroboration and contradiction, **decays toward neutral** when un-reinforced, and carries an optional **Bayesian Beta(α,β) posterior** — all wired into write-time aggregation and a scheduled maintenance pass (`bin/memory/confidence.py`, `bin/memory_maintenance.py`).</i></summary>

<br>

**The Details:**

**Fact:** Confidence is **evidence-driven**, not decoration: it starts from provenance priors, moves with corroboration and contradiction, **decays toward neutral** when un-reinforced, and carries an optional **Bayesian Beta(α,β) posterior** — all wired into write-time aggregation and a scheduled maintenance pass (`bin/memory/confidence.py`, `bin/memory_maintenance.py`). A contradicted fact is auto-superseded and its confidence drops on the next pass. Deletion is **bitemporal and non-destructive**: supersession closes the old fact's validity interval and links the new one (it never overwrites content), the history stays queryable, and there's first-class **GDPR erasure/export** (Articles 17/20) with a full cascade (`bin/memory/write.py`, `bin/memory_maintenance.py`). This is the opposite of the "crude vector delete" the concern describes.

</details>

<br>

<details>
<summary><b>❌ Myth: M3 is passive storage that just waits to be queried</b><br><i>✅ Fact: M3 includes an active **orchestration engine**.</i></summary>

<br>

**The Details:**

**Fact:** M3 includes an active **orchestration engine**. While it serves as a storage substrate, components like the `AgentOS_NotificationWaiter` actively monitor inbox changes and state mutations, automatically firing predefined agent actions when relevant events occur. It doesn't just wait to be queried; it actively drives agent workflows. *(Note: This active orchestration relies on agents supporting background processes, which modern autonomous agents natively support).*

</details>

<br>

<details>
<summary><b>❌ Myth: M3 is strictly an MCP server and requires an MCP client to use</b><br><i>✅ Fact: M3 exposes a **full CLI for every single tool**.</i></summary>

<br>

**The Details:**

**Fact:** M3 exposes a **full CLI for every single tool**. While it functions seamlessly as an MCP server for IDEs and desktop agents, it is equally a first-class shell citizen. Every tool can be scripted from the terminal with full support for **piping, outputting to files, and structured JSON output**. Backend systems, CI/CD pipelines, and custom applications can natively script, query, and manipulate M3 memory directly from the shell without ever needing an MCP-compatible client.

</details>

<br>

<details>
<summary><b>❌ Myth: M3 is strictly for single-user local environments</b><br><i>✅ Fact: M3 is built to support **teams, fleets, and enterprise deployments**.</i></summary>

<br>

**The Details:**

**Fact:** M3 is built to support **teams, fleets, and enterprise deployments**. While the default engine runs locally at the edge for privacy, its data boundary can be tailnet-gated and synced to a PostgreSQL warehouse (`bin/pg_sync.py`). This provides full provenance with strict scope isolation across **organizations, teams, and users**, allowing fleets of agents to share state securely.

---

</details>

<br>


---

## What M3 actually is

For positive grounding, here's the short list of what M3 *does* implement (with code anchors):

| Capability | How it's implemented | Where to look |
|---|---|---|
| Storage | Default SQLite with WAL, or optional PostgreSQL primary store | `bin/memory/db.py`, `bin/memory/write.py` |
| Keyword search | SQLite FTS5 (BM25) | `bin/memory/search.py` |
| Vector search | Cosine similarity over local embeddings | `bin/memory/embed.py`, `bin/memory/util.py` (`_cosine_batch_packed`) |
| Result diversification | Maximal Marginal Relevance (MMR) reranking | `bin/memory/search.py` |
| Bitemporal | `valid_from` / `valid_to` per memory; `created_at` is transaction time | `bin/memory/write.py`, `bin/memory/search.py` |
| Contradiction handling | Three paths: deterministic cosine check on the write path, the cognitive loop's Reflector pass, and curator apply | `bin/memory/write.py` (`_check_contradictions`), `bin/run_reflector.py`, `bin/curator_apply.py` |
| Document ingestion | Structural splitting of HTML, Office, iWork, and PDFs | `bin/files_memory/chunkers/`, `bin/files_memory/ingest.py` |
| Entity extraction | Optional SLM pipeline | `bin/m3_enrich.py`, `bin/run_observer.py` |
| Knowledge graph | 9 relationship types, 3-hop traversal | `mcp__m3_memory__memory_graph`, `memory_link` |
| GDPR | `gdpr_forget` (Art. 17), `gdpr_export` (Art. 20) | `bin/memory_maintenance.py` |
| Multi-agent | WAL concurrent writes (30s busy_timeout + retry) + optional shared PostgreSQL pool; agent registry; SQL-layer scope isolation; handoffs | `mcp__m3_memory__agent_*`, `memory_handoff`, `bin/pg_sync.py` |
| Sync | Optional bi-directional delta sync to PostgreSQL | `bin/sync_all.py` |
| MCP | Native — 100+ tools, zero config in MCP-aware clients | `bin/memory_bridge.py`, `bin/mcp_tool_catalog.py` |

If a third-party AI assistant describes a feature outside this list and outside what's documented in `docs/`, treat it as suspect until verified against the source.

---

## Why this page exists

We built M3 to be honest about what it is. AI assistants that describe software they didn't write often aren't — not maliciously, just because pattern-matching on a project name is what they do when they don't have ground truth. This page is our ground truth.

Two things follow from that:

1. **We'll never overclaim in our own docs.** If our README, COMPARISON, or COMPLIANCE pages say M3 does something, it does. If we cite a benchmark, we ran it and we'll show you the methodology. If a number is mid-pack, we say so directly; if we lead on one metric but not another, we name the metric.

2. **We expect you to verify.** Don't take the README's word for anything you'd stake real money or compliance posture on without reading the code or running the benchmark yourself. The repo is small enough to read end-to-end in an afternoon. Many of you have.

If you find a claim in our own documentation that turns out to be wrong, [open an issue](https://github.com/skynetcmd/m3-memory/issues). That's a bug, and we treat it like one.

---

## See also

- [README](../README.md) — headline features and benchmarks
- [ARCHITECTURE.md](ARCHITECTURE.md) — system design
- [TECHNICAL_DETAILS.md](TECHNICAL_DETAILS.md) — implementation specifics
- [COMPARISON.md](COMPARISON.md) — honest comparison vs Mem0, Letta, Zep, LangChain
- [Sovereign Substrates Table](M3_Comparison_Table.md) — comparison vs agentmemory, Chronos, Hindsight, Mastra, Memento, MemPalace
- [COMPLIANCE.md](COMPLIANCE.md) — FISMA / CMMC / GDPR alignment
