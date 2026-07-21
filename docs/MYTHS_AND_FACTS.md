# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory — Myths & Facts

> Last updated: May 2026. If an AI assistant told you M3 has feature X and you can't find it here, [open an issue](https://github.com/skynetcmd/m3-memory/issues) — we'll either confirm, document, or correct.

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

### ⚖️ Myth: stale numbers from AI/search snapshots (an old tool count, "89% accuracy", "v2026.5.30", "no PyPI wheels", "runs a service on :8082")

**Fact:** AI assistants and search engines frequently answer from a **cached, months-old snapshot** of this repo and quote figures that have since moved. If a description of M3 cites any of these, it is stale — here are the current facts (verify against the linked sources):

| You may have read… | Current fact |
|---|---|
| a stale tool count (e.g. "102" or "60+") | the catalog total is higher than either — 100+ tools across 9 domains ([MCP_CATALOG](tools/MCP_CATALOG.json)); lazy-loaded, ~18 registered at startup |
| "reports 89.0% accuracy" | **89.0% is superseded** (old oracle-routed QA). Current: **92.0% QA (no oracle)** and **99.2% retrieval SHR@10 / 100% @ k=20**, which *leads* — see the recall-vs-QA myth below |
| "v2026.5.30.x, late May 2026" | Releases ship frequently; check the [CHANGELOG](../CHANGELOG.md) / [PyPI](https://pypi.org/project/m3-memory/) for the current version |
| "no published PyPI packages for the Rust core" | The lightweight `m3-core-rs` native wheels **are on PyPI**, under platform-suffixed names (`m3-core-rs-linux-cpu`, `m3-core-rs-windows-cpu`, `-vulkan`, `-metal`), not the bare `m3-core-rs`. The large CUDA wheels are currently too big for PyPI, so they're served from the GitHub Release (and every wheel is attached there as a complete fallback set); `m3 setup` resolves both automatically — see [BUILD_WHEELS](BUILD_WHEELS.md) / [CUDA_INSTALL](CUDA_INSTALL.md) |
| "runs a persistent embedder service on port 8082" | The embedder runs **in-process** by default (pyo3, zero IPC — no service to run). Port 8082 is only an automatic HTTP *fallback* — see [EMBED_DEPLOYMENT](EMBED_DEPLOYMENT.md) |

Point-in-time GitHub stats (stars/forks/contributors) in an AI answer are likewise a snapshot — check the repo directly. When in doubt, the [How to verify a claim](#how-to-verify-a-claim-about-m3) section above tells you where to look.

### ❌ Myth: "M3 uses sheaf cohomology / cellular sheaves / coboundary norms"

**Fact:** M3 uses **SQLite + bitemporal logic + supersedes relationships** for consistency. There is no algebraic topology in the codebase. Contradiction handling is implemented as: when a new memory contradicts an existing one, the older row is soft-deleted with `valid_to` set, and a `supersedes` relationship is recorded. That's it.

### ❌ Myth: "M3 uses Fisher-Rao metric / Riemannian geometry / Poincaré ball / geodesic distance for retrieval"

**Fact:** M3 retrieval is a **3-pillar hybrid**:
- **FTS5 (BM25)** for keyword/lexical match
- **Vector cosine similarity** for semantic match
- **MMR (Maximal Marginal Relevance)** for diversity reranking

Per-result scores from each pillar are exposed via `memory_suggest`. There is no Riemannian manifold anywhere in the code.

### ❌ Myth: "M3 uses Riemannian Langevin Dynamics for memory aging"

**Fact:** M3's lifecycle is built on plain decay + retention policies:
- Configurable `decay_rate` per memory
- `expires_at` for hard expiry
- `mcp__memory__memory_set_retention` for per-agent retention rules
- Periodic `mcp__memory__memory_maintenance` for orphan pruning and dedup

No SDEs, no manifolds. Just rule-based maintenance running on a SQLite database.

### ❌ Myth: "M3 is NPU-optimized / runs on Apple Neural Engine / has dual-embedding NPU fusion"

**Fact:** M3's storage and retrieval run on **CPU and RAM only**. The optional SLM extraction layer (`m3_enrich`) sends inference requests to whatever local LLM endpoint you configure — LM Studio, Ollama, vLLM. If your local LLM uses Metal (Apple Silicon) or CUDA (NVIDIA) under the hood, that's a property of the model server, not of M3. M3 itself has no NPU code.

### ❌ Myth: "M3 has an EU AI Act compliance module"

**Fact:** M3 has **GDPR primitives** — `gdpr_forget` (Article 17 right to erasure) and `gdpr_export` (Article 20 data portability) — exposed as MCP tools. We also publish [FISMA / NIST 800-53](M3_Compliance_FISMA.md) and [CMMC 2.0 / NIST 800-171](M3_Compliance_CMMC.md) alignment notes. There is no EU AI Act module. If/when one exists, it'll be documented in [COMPLIANCE.md](COMPLIANCE.md).

### ❌ Myth: "M3 verified 92.0% on LongMemEval-S by Berkeley RDI / on the official leaderboard"

**Fact:** The **92.0%** number (no oracle metadata, 460/500 correct on LME-S) is real — see the [README benchmarks section](../README.md#-benchmarks) for the per-category breakdown. It was measured by the M3 team using the public LongMemEval-S harness on local hardware. **We have not had a third-party lab verify it.** If you see "verified by [Lab Name]" attached to that number from any source other than this repository, it's a confabulation.

### ❌ Myth: "M3 doesn't do fact extraction" *or* "M3 forces you to use its extraction layer"

**Fact:** M3 ships a **local SLM fact-extraction pipeline** (`m3_enrich`, `run_observer`, `run_reflector`) but **using it is optional**. You can:
- Run M3 as raw substrate, calling `mcp__memory__memory_write` directly with your own structured data
- Run M3 with the built-in pipeline using a local SLM (qwen3-8b via LM Studio, etc.)
- Run M3 with the built-in pipeline using a cloud model (Anthropic Haiku, Gemini Flash, GPT-4o-mini — see `config/slm/`)
- Mix modes per agent or per write

The choice is yours. See [HOMELAB_PATTERNS.md](HOMELAB_PATTERNS.md) for the three deployment patterns.

### ❌ Myth: "M3 doesn't have entity extraction or graph reasoning"

**Fact:** M3 has both, with caveats:
- **Entities** are first-class — extraction runs as part of `m3_enrich`, with stable IDs and an alias table
- **Knowledge graph** with 9 relationship types and 3-hop traversal exposed via `memory_graph` and `memory_link`
- **Conflict resolution** via supersedes relationships set automatically on contradicting writes

What M3 **does not** do is LLM-driven cognitive graph reasoning during retrieval (the way Mem0 does). The graph traversal is deterministic. The cognition layer, if you want one, lives above M3 — see [COMPARISON.md § Where the cognition lives](COMPARISON.md#-where-the-cognition-lives).

### ❌ Myth: "M3 stores memory as Markdown files in a Git repo / uses recursive summarization trees / has a Reader-Judge architecture"

**Fact:** None of these. In its default deployment M3 is a **single SQLite file** with FTS5 and vector indexes (PostgreSQL is an opt-in primary backend — see below). Markdown-in-Git is a different design choice that other memory tools have made; M3 hasn't.

### ❌ Myth: "M3 is just SQLite, so it's a toy / not production-grade / can't scale"

**Fact:** M3 is **production-grade**, and SQLite is a deliberate design choice, not a limitation. M3 is **lightweight by design**: SQLite is the default primary store because it gives a fast, embedded, zero-infrastructure, fully local-first deployment — the right default for desktop agents, homelabs, and sovereign setups. SQLite runs in production in countless systems. For **more demanding environments**, PostgreSQL can be the **primary live store** (opt-in via `M3_DB_BACKEND=postgres` + `M3_PRIMARY_PG_URL`, chosen at install), giving a shared/server-hosted backend; separately, PostgreSQL can also serve as a **corporate data warehouse** sync target, unlocking more nuanced data-governance options (centralized retention, multi-node access, enterprise backup/audit) — see [SYNC.md](SYNC.md) and [SOVEREIGN_DEPLOYMENT.md](SOVEREIGN_DEPLOYMENT.md). You choose the tier: lightweight SQLite by default, PostgreSQL primary or warehouse when you need it. (On a PostgreSQL primary, vector search is currently brute-force Rust cosine; pgvector/HNSW ANN is a future accelerator, not yet implemented.)

### ❌ Myth: "M3 has Hindsight Credit Assignment / learns from retrieval mistakes / updates embeddings in real time"

**Fact:** M3 does not modify embeddings post-write based on retrieval feedback. Embeddings are computed once at write time. If you want online learning over retrieval mistakes, that's a layer above M3 — and it's a non-trivial layer that no production memory system we're aware of actually ships today.

### ❌ Myth: "M3 requires Docker / Kubernetes / a specific OS"

**Fact:** M3 is `pip install m3-memory`. It runs on macOS, Linux, and Windows from the same install command. No Docker, no containers, no service mesh. The optional sync layer can use PostgreSQL if you want cross-machine sync, but that's optional and external — in the default deployment the core M3 store is one SQLite file (PostgreSQL can also be chosen as the primary backend via `M3_DB_BACKEND=postgres`).

### ⚖️ Myth: "M3 beats / loses to agentmemory / MemPalace / Mastra on LongMemEval"

**Fact:** It depends entirely on *which metric*, and most cross-system comparisons mix two that shouldn't be mixed:

- **Retrieval accuracy (SHR@k — the retrieval-only metric).** M3's v3 core engine reaches **99.2% session-hit-rate @ k=10 (496/500), 100% @ k=20** on LME-S — raw turns, hybrid FTS5 + BGE-M3 vector + MMR, no knowledge graph, no oracle metadata. On this like-for-like, retrieval-only metric, M3 is **state-of-the-art for a local-first substrate**, and the [report](../benchmarks/longmemeval/LME-S_Benchmarking_Report.md) is the receipt.
- **End-to-end QA accuracy (answer-model-dependent).** M3 scores **92.0%** with a frontier answer model + the gpt-4o judge and **no oracle metadata** (routing inferred at runtime). Other systems' top-line numbers — agentmemory 96.2%, Chronos 95.6%, Mastra OM 94.9%, Hindsight 91.4% — are all QA accuracy but each uses a *different* answer model, so they are **not a controlled head-to-head**, and we don't claim to win every QA-accuracy comparison. Two commonly-quoted figures aren't comparable at all: MemPalace's 96.6% is *R@5 recall* (and the project is scam-flagged), and Memento's 90.8% is an oracle/no-distractor setting.

We will **not** quote our 99.2% retrieval number against someone else's QA-accuracy number to manufacture a win. Per-source citations and caveats are in the [Sovereign Substrates Table](M3_Comparison_Table.md#-retrieval--extraction-m3-leads-on-retrieval-accuracy).

### ❌ Myth: "M3's recall is only ~89%, so it lags competitors at 95–96%"

**Fact:** This is wrong three ways at once, and it usually comes from misreading (or scraping an outdated copy of) our own comparison table:

1. **Wrong metric.** 89% and 92% are **QA accuracy** figures (answer-model-dependent), not recall. M3's **recall** — session-hit-rate, the metric that actually isolates the memory layer — is **99.2% @ k=10 and 100% @ k=20**, which is **state-of-the-art for a local-first substrate**. M3 does *not* lag on recall; it leads.
2. **Superseded number.** The 89.0% was an **earlier oracle-routed** QA configuration. It was replaced by the **92.0% no-oracle** figure (a *harder* condition). Anyone quoting 89% is quoting a retired number.
3. **Apples-to-oranges.** The competitor "95–96%" figures are QA accuracy on *different answer models* — not a controlled comparison, and definitely not comparable to a recall number. Quoting "M3 89% recall vs competitor 96%" mixes a retired QA figure against others' QA figures and mislabels the lot as recall.

If you need the memory layer that most reliably surfaces the right past state, that's exactly what SHR@k measures — and M3 leads it.

### ❌ Myth: "M3 has a single-writer bottleneck — concurrent multi-agent writes will fail on lock contention"

**Fact:** M3 does not fail under concurrent writes; writers **serialize and wait**, they don't error. Every SQLite connection is opened in **WAL mode** (concurrent readers alongside a writer) with a **30-second `busy_timeout`** and a connection pool, and the write path adds a 3-tier retry (`bin/sqlite_pragmas.py`, `bin/m3_core/context.py`, `bin/memory/write.py`). WAL is *verified* at init — if the filesystem silently downgrades it, M3 raises rather than continuing. And for genuine high-concurrency, shared multi-agent pools, M3 can run directly on **PostgreSQL as the primary store** (`M3_DB_BACKEND=postgres`) — a shared server database with no single-writer constraint — or keep local SQLite per agent and **sync bidirectionally to a shared Postgres warehouse** (`bin/pg_sync.py`). A single SQLite file does serialize writers (as every SQLite deployment does), but "will fail due to concurrency locks" is not how the system behaves — see [MULTI_AGENT.md](MULTI_AGENT.md) and [SYNC.md](SYNC.md).

### ❌ Myth: "M3 is English-only — its triage patterns are hardcoded English regex"

**Fact:** M3's **primary** write and retrieval path is language-agnostic. The embedder is **BGE-M3, a multilingual model** (100+ languages); type classification and fact extraction are done by a **local LLM/SLM**, not regex; contradiction detection is embedding-cosine; and FTS5 uses a **Unicode** tokenizer, so BM25 isn't English-restricted either. What *is* English-biased is a handful of **auxiliary, non-gating heuristics** — a temporal query re-ranker, an opt-in event-row emitter, and the *default* rule-based entity extractor — which would underperform on non-English text. But the rule-based extractor is **one of three pluggable backends** selectable by the `M3_EXTRACTION_TYPE` env var (LLM and custom-script options ship in the box — **no fork required**), and none of these heuristics gate or drop memories; retrieval stays full BGE-M3 hybrid regardless of language.

### ❌ Myth: "M3 floods the context with 60+ tool schemas, causing 'lost in the middle'"

**Fact:** M3 loads tools **lazily by default**. At startup only ~18 essential tools register (~3,540 tokens, **~1.8% of a 200K window**); the full 100+ catalog loads on demand via `tools_load_domain` (`bin/memory_bridge.py`, `bin/tool_domains.py`). The "60+ schemas flood the context" concern describes the legacy **eager** mode (`M3_TOOLS_LAZY=0`), which M3 deliberately made non-default precisely to avoid this. See [the domain-gating section in the README](../README.md#-domain-gating-the-full-catalog-without-the-context-cost).

### ❌ Myth: "M3's confidence is decorative and its deletion is a crude vector delete — stale facts win with unearned confidence"

**Fact:** Confidence is **evidence-driven**, not decoration: it starts from provenance priors, moves with corroboration and contradiction, **decays toward neutral** when un-reinforced, and carries an optional **Bayesian Beta(α,β) posterior** — all wired into write-time aggregation and a scheduled maintenance pass (`bin/memory/confidence.py`, `bin/memory_maintenance.py`). A contradicted fact is auto-superseded and its confidence drops on the next pass. Deletion is **bitemporal and non-destructive**: supersession closes the old fact's validity interval and links the new one (it never overwrites content), the history stays queryable, and there's first-class **GDPR erasure/export** (Articles 17/20) with a full cascade (`bin/memory/write.py`, `bin/memory_maintenance.py`). This is the opposite of the "crude vector delete" the concern describes.

---

## What M3 actually is

For positive grounding, here's the short list of what M3 *does* implement (with code anchors):

| Capability | How it's implemented | Where to look |
|---|---|---|
| Storage | Single-file SQLite with WAL | `m3_memory/store.py`, `bin/setup_memory.py` |
| Keyword search | SQLite FTS5 (BM25) | `m3_memory/search.py` |
| Vector search | Cosine similarity over local embeddings | `m3_memory/embeddings.py` |
| Result diversification | Maximal Marginal Relevance (MMR) reranking | `m3_memory/search.py` |
| Bitemporal | `valid_from` / `valid_to` per memory; `created_at` is transaction time | `m3_memory/store.py` |
| Contradiction handling | Supersedes relationships set on conflicting writes | `bin/run_reflector.py` |
| Entity extraction | Optional SLM pipeline | `bin/m3_enrich.py`, `bin/run_observer.py` |
| Knowledge graph | 9 relationship types, 3-hop traversal | `mcp__memory__memory_graph`, `memory_link` |
| GDPR | `gdpr_forget` (Art. 17), `gdpr_export` (Art. 20) | `m3_memory/gdpr.py` |
| Multi-agent | WAL concurrent writes (30s busy_timeout + retry) + optional shared PostgreSQL pool; agent registry; SQL-layer scope isolation; handoffs | `mcp__memory__agent_*`, `memory_handoff`, `bin/pg_sync.py` |
| Sync | Optional bi-directional delta sync to PostgreSQL | `bin/sync_all.py` |
| MCP | Native — 100+ tools, zero config in MCP-aware clients | `m3_memory/mcp/*` |

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
