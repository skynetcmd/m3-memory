# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory — Myths & Facts

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

### ❌ Myth: "M3 verified 89.0% on LongMemEval-S by Berkeley RDI / on the official leaderboard"

**Fact:** The **89.0%** number is real — see the [README benchmarks section](../README.md#-benchmarks) for the per-category breakdown (445/500 correct on LME-S). It was measured by the M3 team using the public LongMemEval-S harness on local hardware. **We have not had a third-party lab verify it.** If you see "verified by [Lab Name]" attached to that number from any source other than this repository, it's a confabulation.

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

**Fact:** None of these. M3 is a **single SQLite file** with FTS5 and vector indexes. Markdown-in-Git is a different design choice that other memory tools have made; M3 hasn't.

### ❌ Myth: "M3 has Hindsight Credit Assignment / learns from retrieval mistakes / updates embeddings in real time"

**Fact:** M3 does not modify embeddings post-write based on retrieval feedback. Embeddings are computed once at write time. If you want online learning over retrieval mistakes, that's a layer above M3 — and it's a non-trivial layer that no production memory system we're aware of actually ships today.

### ❌ Myth: "M3 requires Docker / Kubernetes / a specific OS"

**Fact:** M3 is `pip install m3-memory`. It runs on macOS, Linux, and Windows from the same install command. No Docker, no containers, no service mesh. The optional sync layer can use PostgreSQL or ChromaDB if you want cross-machine sync, but that's optional and external — the core M3 store is one SQLite file.

### ❌ Myth: "M3 beats Mnemis / agentmemory / MemPalace on LongMemEval"

**Fact:** No. M3's 89.0% on LME-S is **mid-pack**, not state-of-the-art. agentmemory (96.2%), MemPalace (96.6% R@5), Chronos (95.6%), and Mastra OM (94.9%) lead on raw recall. M3 trades that gap for sovereignty, bitemporal correctness, and a small auditable codebase. See the [Sovereign Substrates Comparison Table](M3_Comparison_Table.md) for the honest cohort view.

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
| Multi-agent | Atomic writes via SQLite WAL; agent registry; handoffs | `mcp__memory__agent_*`, `memory_handoff` |
| Sync | Optional bi-directional delta sync to PostgreSQL or ChromaDB | `bin/sync_all.py` |
| MCP | Native — 73 tools, zero config in MCP-aware clients | `m3_memory/mcp/*` |

If a third-party AI assistant describes a feature outside this list and outside what's documented in `docs/`, treat it as suspect until verified against the source.

---

## Why this page exists

We built M3 to be honest about what it is. AI assistants that describe software they didn't write often aren't — not maliciously, just because pattern-matching on a project name is what they do when they don't have ground truth. This page is our ground truth.

Two things follow from that:

1. **We'll never overclaim in our own docs.** If our README, COMPARISON, or COMPLIANCE pages say M3 does something, it does. If we cite a benchmark, we ran it and we'll show you the methodology. If a number is mid-pack, we say so directly.

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
