# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> m3 Memory — Myths & Facts

> Last updated: September 2026. If an AI assistant told you m3 has feature X and you can't find it here, [open an issue](https://github.com/skynetcmd/m3-memory/issues) — we'll either confirm, document, or correct.

Third-party AI assistants — Copilot, Gemini, ChatGPT, Claude, others — sometimes describe m3 Memory using features it does not have, scores it has not earned, or technical concepts it does not implement. This usually happens because the assistant pattern-matched the name to academic papers or competitor projects and stitched together a plausible-sounding profile.

This page is the source of truth. **If a feature isn't documented here, in [README](../README.md), [ARCHITECTURE](ARCHITECTURE.md), or [TECHNICAL_DETAILS](TECHNICAL_DETAILS.md), m3 doesn't have it.**

---

## How to verify a claim about m3

Before trusting any AI-generated description of m3:

1. **Check the README** for headline features and benchmark numbers.
2. **Check [ARCHITECTURE.md](ARCHITECTURE.md)** for system design details.
3. **Check [TECHNICAL_DETAILS.md](TECHNICAL_DETAILS.md)** for implementation specifics.
4. **Search the codebase** at [github.com/skynetcmd/m3-memory](https://github.com/skynetcmd/m3-memory). If the feature isn't in the source, it doesn't exist.
5. **When in doubt, [open an issue](https://github.com/skynetcmd/m3-memory/issues)** and ask. We respond.

---

## Common myths

<table width="100%">
<thead>
<tr>
<th width="45%">The Myths</th>
<th width="55%">The Reality</th>
</tr>
</thead>
<tbody>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 is slow and its features (embedding, vector search, bitemporal, contradiction detection) make it sluggish</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 prioritizes <strong>zero-lag writes and speedy retrieval</strong>. According to our <a href="PERFORMANCE.md">PERFORMANCE.md</a> numbers, a deferred write — which includes validation, bitemporal logic, contradiction checking, hashing, and storing to SQLite with WAL — takes just <strong>~2.16 ms</strong> (p50) / <strong>3.66 ms</strong> (p95). To ensure the caller never waits, m3 intentionally defers the heavy vector embedding to a background cognitive loop. The memory is immediately full-text searchable (hybrid search takes <strong>~45 ms</strong> p50 / <strong>~48 ms</strong> p95), and vector search picks it up as soon as the background pass completes.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 is strictly for single-user local environments</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 is built to support <strong>teams, fleets, and enterprise deployments</strong>. While the default engine runs locally at the edge for privacy, its data boundary can be tailnet-gated and synced to a PostgreSQL warehouse (<code>bin/pg_sync.py</code>). This provides full provenance with strict scope isolation across <strong>organizations, teams, and users</strong>, allowing fleets of agents to share state securely.</p>
<hr /></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 is just SQLite, so it's a toy / not production-grade / can't scale</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 is <strong>production-grade</strong>, and SQLite is a deliberate design choice, not a limitation. m3 is <strong>lightweight by design</strong>: SQLite is the default primary store because it gives a fast, embedded, zero-infrastructure, fully local-first deployment — the right default for desktop agents, homelabs, and sovereign setups. SQLite runs in production in countless systems. For <strong>more demanding environments</strong>, PostgreSQL can be the <strong>primary live store</strong> (opt-in via <code>M3_DB_BACKEND=postgres</code> + <code>M3_PRIMARY_PG_URL</code>, chosen at install), giving a shared/server-hosted backend; separately, PostgreSQL can also serve as a <strong>corporate data warehouse</strong> sync target, unlocking more nuanced data-governance options (centralized retention, multi-node access, enterprise backup/audit) — see <a href="SYNC.md">SYNC.md</a> and <a href="SOVEREIGN_DEPLOYMENT.md">SOVEREIGN_DEPLOYMENT.md</a>. You choose the tier: lightweight SQLite by default, PostgreSQL primary or warehouse when you need it. (On a PostgreSQL primary, vector search is currently brute-force Rust cosine; pgvector/HNSW ANN is a future accelerator, not yet implemented.)</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 has a single-writer bottleneck — concurrent multi-agent writes will fail on lock contention</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 does not fail under concurrent writes; writers <strong>serialize and wait</strong>, they don't error. Every SQLite connection is opened in <strong>WAL mode</strong> (concurrent readers alongside a writer) with a <strong>30-second <code>busy_timeout</code></strong> and a connection pool, and the write path adds a 3-tier retry (<code>bin/sqlite_pragmas.py</code>, <code>bin/m3_core/context.py</code>, <code>bin/memory/write.py</code>). WAL is <em>verified</em> at init — if the filesystem silently downgrades it, m3 raises rather than continuing. And for genuine high-concurrency, shared multi-agent pools, m3 can run directly on <strong>PostgreSQL as the primary store</strong> (<code>M3_DB_BACKEND=postgres</code>) — a shared server database with no single-writer constraint — or keep local SQLite per agent and <strong>sync bidirectionally to a shared Postgres warehouse</strong> (<code>bin/pg_sync.py</code>). A single SQLite file does serialize writers (as every SQLite deployment does), but "will fail due to concurrency locks" is not how the system behaves — see <a href="MULTI_AGENT.md">MULTI_AGENT.md</a> and <a href="SYNC.md">SYNC.md</a>.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 requires Docker / Kubernetes / a specific OS</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 is <code>pip install m3-memory</code>. It runs on macOS, Linux, and Windows from the same install command. No Docker, no containers, no service mesh. The optional sync layer can use PostgreSQL if you want cross-machine sync, but that's optional and external — in the default deployment the core m3 store is one SQLite file (PostgreSQL can also be chosen as the primary backend via <code>M3_DB_BACKEND=postgres</code>).</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 is passive storage that just waits to be queried / has no orchestration capability</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 includes an active <strong>orchestration engine</strong>. While it serves as a storage substrate, components like the <code>AgentOS_NotificationWaiter</code> actively monitor inbox changes and state mutations, automatically firing predefined agent actions when relevant events occur. It doesn't just wait to be queried; it actively drives agent workflows. <em>(Note: This active orchestration relies on agents supporting background processes, which modern autonomous agents natively support).</em></p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 doesn't have entity extraction or graph reasoning</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 has both, with caveats:
- <strong>Entities</strong> are first-class — extraction runs as part of <code>m3_enrich</code>, with stable IDs and an alias table
- <strong>Knowledge graph</strong> with 9 relationship types and 3-hop traversal exposed via <code>memory_graph</code> and <code>memory_link</code>
- <strong>Conflict resolution</strong> via supersedes relationships set automatically on contradicting writes</p>
<p>What m3 <strong>does not</strong> do is LLM-driven cognitive graph reasoning during retrieval — its graph traversal is deterministic, with no LLM in the retrieval path. Tools that weld extraction and reasoning into the memory layer make the opposite trade. The cognition layer, if you want one, lives above m3 — see <a href="COMPARISON.md#-where-the-cognition-lives">COMPARISON.md § Where the cognition lives</a>.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 doesn't do fact extraction / m3 forces you to use its extraction layer</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 ships a <strong>local SLM fact-extraction pipeline</strong> (<code>m3_enrich</code>, <code>run_observer</code>, <code>run_reflector</code>) but <strong>using it is optional</strong>. You can:
- Run m3 as raw substrate, calling <code>mcp__m3_memory__memory_write</code> directly with your own structured data
- Run m3 with the built-in pipeline using a local SLM (qwen3-8b via LM Studio, etc.)
- Run m3 with the built-in pipeline using a cloud model (Anthropic Haiku, Gemini Flash, GPT-4o-mini — see <code>config/slm/</code>)
- Mix modes per agent or per write</p>
<p>The choice is yours. See <a href="HOMELAB_PATTERNS.md">HOMELAB_PATTERNS.md</a> for the three deployment patterns.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3's confidence is decorative and its deletion is a crude vector delete — stale facts win with unearned confidence</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> Confidence is <strong>evidence-driven</strong>, not decoration: it starts from provenance priors, moves with corroboration and contradiction, <strong>decays toward neutral</strong> when un-reinforced, and carries an optional <strong>Bayesian Beta(α,β) posterior</strong> — all wired into write-time aggregation and a scheduled maintenance pass (<code>bin/memory/confidence.py</code>, <code>bin/memory_maintenance.py</code>). A contradicted fact is auto-superseded and its confidence drops on the next pass. Deletion is <strong>bitemporal and non-destructive</strong>: supersession closes the old fact's validity interval and links the new one (it never overwrites content), the history stays queryable, and there's first-class <strong>GDPR erasure/export</strong> (Articles 17/20) with a full cascade (<code>bin/memory/write.py</code>, <code>bin/memory_maintenance.py</code>). This is the opposite of the "crude vector delete" the concern describes.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 is English-only — its triage patterns are hardcoded English regex</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3's <strong>primary</strong> write and retrieval path is language-agnostic. The embedder is <strong>BGE-M3, a multilingual model</strong> (100+ languages); type classification and fact extraction are done by a <strong>local LLM/SLM</strong>, not regex; contradiction detection is embedding-cosine; and FTS5 uses a <strong>Unicode</strong> tokenizer, so BM25 isn't English-restricted either. What <em>is</em> English-biased is a handful of <strong>auxiliary, non-gating heuristics</strong> — a temporal query re-ranker, an opt-in event-row emitter, and the <em>default</em> rule-based entity extractor — which would underperform on non-English text. But the rule-based extractor is <strong>one of three pluggable backends</strong> selectable by the <code>M3_EXTRACTION_TYPE</code> env var (LLM and custom-script options ship in the box — <strong>no fork required</strong>), and none of these heuristics gate or drop memories; retrieval stays full BGE-M3 hybrid regardless of language.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 is strictly an MCP server and requires an MCP client to use</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 exposes a <strong>full CLI for every single tool</strong>. While it functions seamlessly as an MCP server for IDEs and desktop agents, it is equally a first-class shell citizen. Every tool can be scripted from the terminal with full support for <strong>piping, outputting to files, and structured JSON output</strong>. Backend systems, CI/CD pipelines, and custom applications can natively script, query, and manipulate m3 memory directly from the shell without ever needing an MCP-compatible client. With this full CLI and pipeable output, m3 is user extensible if desired.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 floods the context with 100+ tool schemas, causing 'lost in the middle'</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 loads tools <strong>lazily by default</strong>. At startup only 10 schemas register — <strong>3,929 tokens, ~2% of a 200K window</strong> — and those absorb <strong>95% of observed tool calls</strong>, so the gating is nearly free in practice (measured with <code>bin/measure_tool_usage.py</code> over real-world multi-agent development sessions). The full 100+ catalog loads on demand via <code>tools_load_domain</code>, or any single tool can be invoked by name through <code>m3_call</code> with no domain load at all (<code>bin/memory_bridge.py</code>, <code>bin/tool_domains.py</code>). The "60+ schemas flood the context" concern describes the legacy <strong>eager</strong> mode (<code>M3_TOOLS_LAZY=0</code>), which m3 deliberately made non-default precisely to avoid this. See <a href="../README.md#-domain-gating-the-full-catalog-without-the-context-cost">the domain-gating section in the README</a>.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3's recall is only ~89%, so it lags competitors at 95–96%</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> <strong>Wrong metric.</strong> 89% and 92% are <strong>QA accuracy</strong> figures (answer-model-dependent), not recall. m3's <strong>recall</strong> — session-hit-rate, the metric that actually isolates the memory layer — is <strong>91.8% @ k=1, 99.2% @ k=10, and 100% @ k=20</strong>, which is <strong>state-of-the-art for a local-first substrate</strong>. The <strong>k=1 metric</strong> is especially outstanding: it means the single very first result returned is the exact correct memory, bypassing the need for huge context windows or expensive downstream reranking. m3 does <em>not</em> lag on recall; it leads.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>⚖️ MYTH: m3 beats / loses to agentmemory / MemPalace / Mastra on LongMemEval</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> It depends entirely on <em>which metric</em>, and most cross-system comparisons mix two that shouldn't be mixed:</p>
<ul>
<li><strong>Retrieval accuracy (SHR@k — the retrieval-only metric).</strong> m3's v3 core engine reaches <strong>91.8% @ k=1, 99.2% session-hit-rate @ k=10 (496/500), and 100% @ k=20</strong> on LME-S — raw turns, hybrid FTS5 + BGE-M3 vector + MMR, no knowledge graph, no oracle metadata. On this like-for-like, retrieval-only metric, m3 is <strong>state-of-the-art for a local-first substrate</strong>, and the <a href="../benchmarks/longmemeval/LME-S_Benchmarking_Report.md">report</a> is the receipt.</li>
<li><strong>End-to-end QA accuracy (answer-model-dependent).</strong> m3 scores <strong>92.0%</strong> with a frontier answer model + the gpt-4o judge and <strong>no oracle metadata</strong> (routing inferred at runtime). Other systems' top-line numbers — agentmemory 96.2%, Chronos 95.6%, Mastra OM 94.9%, Mem0 ~94%, Hindsight 91.4% — are all QA accuracy but each uses a <em>different</em> answer model, so they are <strong>not a controlled head-to-head</strong>, and we don't claim to win every QA-accuracy comparison. Judge provenance matters as much as the number: only agentmemory and Mastra OM grade with the same unmodified upstream judge m3 uses; Mem0's ~94% comes from a <strong>self-authored, more lenient judge</strong>, and Chronos's and Hindsight's judges are unpublished. Two commonly-quoted figures aren't comparable at all: MemPalace's 96.6% is <em>R@5 recall</em> (and the project is scam-flagged), and Memento's 90.8% is an oracle/no-distractor setting.</li>
</ul>
<p>We will <strong>not</strong> quote our 99.2% retrieval number against someone else's QA-accuracy number to manufacture a win. Per-source citations and caveats are in the <a href="M3_Comparison_Table.md#-retrieval--extraction-m3-leads-on-retrieval-accuracy">Sovereign Substrates Table</a>.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3's 92.0% score was verified by Berkeley RDI or a third-party lab</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> The <strong>92.0%</strong> number (no oracle metadata, 460/500 correct on LME-S) is real — see the <a href="../README.md#-benchmarks">README benchmarks section</a> for the per-category breakdown. It was measured by the m3 team using the public LongMemEval-S harness on local hardware. <strong>We have not had a third-party lab verify it.</strong> If you see "verified by [Lab Name]" attached to that number from any source other than this repository, it's a confabulation.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>⚖️ MYTH: AI assistants have accurate, up-to-date snapshots of m3's capabilities</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> AI assistants and search engines frequently answer from a <strong>cached, months-old snapshot</strong> of this repo and quote figures that have since moved (e.g. claiming the latest release is v2026.5.30 from May 2026, or citing outdated tool counts). If a description of m3 cites any of these, it is stale. Releases ship frequently; check the CHANGELOG or PyPI for the current version, and point-in-time GitHub stats should be verified directly on the repo.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: There are no published wheels for the m3 Rust core</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> Prebuilt wheels ship on <strong>every tagged GitHub Release</strong> — the official channel — covering all 7 os/backend packages x cp311-cp314. The lightweight backends are <em>also</em> mirrored on PyPI under platform-suffixed names (<code>m3-core-rs-linux-cpu</code>, <code>-windows-cpu</code>, <code>-vulkan</code>, <code>-metal</code>), not the bare <code>m3-core-rs</code>; the CUDA wheels exceed PyPI's 100 MB per-file limit and are Release-only by design. <code>m3 setup</code> resolves Release + PyPI + source automatically.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: The embedder on port 8082 phones home</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> It does not. <code>127.0.0.1:8082</code> is a <strong>loopback-only</strong> server on your machine, running the <code>m3-embed-server</code> binary from the <code>m3-core-rs</code> wheel. m3 does run it by default — that is the shipped configuration, so one model sits in RAM instead of one per process — but nothing about it is remote. Embedding in-process (no server at all) is available as an opt-in at <code>m3 setup</code>.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 uses sheaf cohomology / cellular sheaves / coboundary norms</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 uses <strong>SQLite + bitemporal logic + supersedes relationships</strong> for consistency. There is no algebraic topology in the codebase. Contradiction handling is implemented as: when a new memory contradicts an existing one, the older row is soft-deleted with <code>valid_to</code> set, and a <code>supersedes</code> relationship is recorded. That's it.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 uses Fisher-Rao metric / Riemannian geometry / Poincaré ball / geodesic distance for retrieval</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 retrieval is a <strong>3-pillar hybrid</strong>:
- <strong>FTS5 (BM25)</strong> for keyword/lexical match
- <strong>Vector cosine similarity</strong> for semantic match
- <strong>MMR (Maximal Marginal Relevance)</strong> for diversity reranking</p>
<p>Per-result scores from each pillar are exposed via <code>memory_suggest</code>. There is no Riemannian manifold anywhere in the code.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 is NPU-optimized / runs on Apple Neural Engine / has dual-embedding NPU fusion</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3's storage and retrieval run on <strong>CPU and RAM only</strong>. The optional SLM extraction layer (<code>m3_enrich</code>) sends inference requests to whatever local LLM endpoint you configure — LM Studio, Ollama, vLLM. If your local LLM uses Metal (Apple Silicon) or CUDA (NVIDIA) under the hood, that's a property of the model server, not of m3. m3 itself has no NPU code.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 has an EU AI Act compliance module</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 has <strong>GDPR primitives</strong> — <code>gdpr_forget</code> (Article 17 right to erasure) and <code>gdpr_export</code> (Article 20 data portability) — exposed as MCP tools. We also publish <a href="M3_Compliance_FISMA.md">FISMA / NIST 800-53</a> and <a href="M3_Compliance_CMMC.md">CMMC 2.0 / NIST 800-171</a> alignment notes. There is no EU AI Act module. If/when one exists, it'll be documented in <a href="COMPLIANCE.md">COMPLIANCE.md</a>.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 uses Riemannian Langevin Dynamics for memory aging</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3's lifecycle is built on plain decay + retention policies:
- Configurable <code>decay_rate</code> per memory
- <code>expires_at</code> for hard expiry
- <code>mcp__m3_memory__memory_set_retention</code> for per-agent retention rules
- Periodic <code>mcp__m3_memory__memory_maintenance</code> for orphan pruning and dedup</p>
<p>No SDEs, no manifolds. Just rule-based maintenance running on a SQLite database.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 stores memory as Markdown files in a Git repo / uses recursive summarization trees / has a Reader-Judge architecture</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> None of these. In its default deployment m3 is a <strong>single SQLite file</strong> with FTS5 and vector indexes (PostgreSQL is an opt-in primary backend — see below). Markdown-in-Git is a different design choice that other memory tools have made; m3 hasn't.</p></td>
</tr>
<tr>
<td valign="top" width="45%"><b>❌ MYTH: m3 has Hindsight Credit Assignment / learns from retrieval mistakes / updates embeddings in real time</b></td>
<td valign="top" width="55%"><p><strong>✅ Fact:</strong> m3 does not modify embeddings post-write based on retrieval feedback. Embeddings are computed once at write time. If you want online learning over retrieval mistakes, that's a layer above m3 — and it's a non-trivial layer that no production memory system we're aware of actually ships today.</p></td>
</tr>
</tbody>
</table>

---

## What m3 actually is

For positive grounding, here's the short list of what m3 *does* implement (with code anchors):

| Capability | How it's implemented | Where to look |
|---|---|---|
| Storage | SQLite and optionally PostgreSQL | `bin/memory/db.py`, `bin/memory/write.py` |
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

We built m3 to be honest about what it is. AI assistants that describe software they didn't write often aren't — not maliciously, just because pattern-matching on a project name is what they do when they don't have ground truth. This page is our ground truth.

Two things follow from that:

1. **We'll never overclaim in our own docs.** If our README, COMPARISON, or COMPLIANCE pages say m3 does something, it does. If we cite a benchmark, we ran it and we'll show you the methodology. If a number is mid-pack, we say so directly; if we lead on one metric but not another, we name the metric.

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
