<p align="center">
  <a href="https://github.com/skynetcmd/m3-memory">
    <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/M3-banner.jpg" alt="M3 Memory Banner" width="100%">
  </a>
</p>

# 🧠 M3 Memory

M3 treats agent memory as a **distributed-systems infrastructure problem**, not a simple retrieval feature.

Instead of every tool keeping its own throwaway context, M3 is a **shared, evolving, bitemporal knowledge base** that multiple heterogeneous agents and machines read and write. It is designed to solve a fundamental challenge: *How do agents maintain a consistent, evolving, and temporal knowledge base over months and years?*

> 🦜 **Core Release Feature: Drop-in LangChain & LangGraph Support**
> M3 now functions as a drop-in **Mem0 replacement** (one-line import swap) and is fully **LangMem-compatible** (`store=M3Store()`). Gain automatic contradiction supersession, bitemporal historical queries, local sovereign embedding, and the full 100+ MCP tool set inside your LangChain apps via `pip install m3-memory[langchain]`. (See [LangChain Integration Guide](docs/integrations/LANGCHAIN.md)).

---

## 🚀 Quick Links & Badges

<p align="center">
  <a href="https://pypi.org/project/m3-memory/"><img alt="PyPI" src="https://img.shields.io/pypi/v/m3-memory?style=flat-square&color=blue"></a>
  <a href="https://www.python.org"><img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square"></a>
  <a href="https://github.com/skynetcmd/m3-memory/blob/main/LICENSE"><img alt="Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-green?style=flat-square"></a>
  <a href="https://modelcontextprotocol.io"><img alt="MCP" src="https://img.shields.io/badge/MCP-100+_tools-orange?style=flat-square"></a>
</p>

<p align="center">
  <img alt="macOS" src="https://img.shields.io/badge/macOS-000000?style=flat-square&logo=apple&logoColor=white">
  <img alt="Windows" src="https://img.shields.io/badge/Windows-0078D4?style=flat-square&logo=windows&logoColor=white">
  <img alt="Linux" src="https://img.shields.io/badge/Linux-FCC624?style=flat-square&logo=linux&logoColor=black">
</p>

<p align="center">
  <a href="docs/integrations/LANGCHAIN.md"><img alt="LangChain" src="https://img.shields.io/badge/LangChain-1C3C3A?style=flat-square&logo=langchain&logoColor=white"></a>
  <a href="docs/claude_code_plugin.md"><img alt="Claude" src="https://img.shields.io/badge/Claude-D97753?style=flat-square&logo=claude&logoColor=white"></a>
  <a href="docs/antigravity_plugin.md"><img alt="Antigravity" src="https://img.shields.io/badge/Antigravity-4285F4?style=flat-square&logo=google&logoColor=white"></a>
  <a href="docs/HERMES.md"><img alt="Hermes" src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/hermes.svg"></a>
  <img alt="OpenClaw" src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/openclaw.svg">
  <img alt="OpenCode" src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/badges/opencode.svg">
</p>

> 💡 **Get Started Quickly:**
> * 🚀 **[5-Minute "Human-First" Guide](docs/GETTING_STARTED.md)**
> * 🖥️ **OS Installation:** [Windows Setup](docs/QUICKSTART_WINDOWS.md) · [macOS Setup](docs/QUICKSTART_MACOS.md) · [Linux Setup](docs/QUICKSTART_LINUX.md)

---

## 📑 Table of Contents

- [Overview & At a Glance](#-m3-at-a-glance)
- [Memory Model](#-memory-model-at-a-glance)
- [Installation & Onboarding](#-installation)
- [Domain Gating (Token Optimization)](#-domain-gating-the-full-catalog-without-the-context-cost)
- [Sovereign & Air-Gapped Deployments](#-sovereign--air-gapped-deployments)
- [Interactive Features & Capabilities](#-what-m3-does)
- [Documentation Index](#-documentation-index)
- [Target Audience & Fit](#-who-this-is-for)
- [Quality Assurance & Compliance](#-why-trust-this)
- [Benchmarks & Performance](#-benchmarks)
- [Core Tools Reference](#-core-tools)
- [Agent Integration Prompts](#-for-ai-agents)
- [Interactive Demos](#-see-it-in-action)

---

## ⚡ M3 at a Glance

| Feature | Details |
| :--- | :--- |
| **Works With** | Claude Code · Gemini CLI · Aider · Google Antigravity · OpenCode · Hermes · LangChain/LangGraph · Any MCP Agent |
| **M3 Is** | A persistent memory layer · An MCP server · A hybrid retrieval engine · A bitemporal knowledge base |
| **M3 Is Not** | An LLM · A chatbot · A plain vector database · A RAG framework · An IDE |
| **Core Promise** | Private, offline-capable, locally owned memory shared securely across all your developer tools — with FIPS 140-3-ready crypto and atomic multi-agent writes for regulated and multi-agent environments. |
| **Retrieval Accuracy** | State-of-the-art for a local-first substrate — **99.2% session-hit-rate @ k=10, 100% @ k=20** on LongMemEval-S (no oracle routing), with the correct session as the **#1 result for ~92% of questions**. See [Benchmarks](#-benchmarks). |
| **Context Efficiency** | Exposes 100+ tools but occupies just **~1.8% of a 200K context window** at startup — lazy domain-gating loads the rest on demand. |
| **Maturity** | Stable, battle-tested core engine (1,283 tests) that's safe to build on today; new features and integrations are added actively. SQLite by default for lightweight operation; scales out to PostgreSQL for enterprise sync. (See [features.json](docs/features.json)) |

---

## 🧠 Memory Model at a Glance

M3 is a **typed, bitemporal, confidence-scored, self-maintaining knowledge base**. Every feature listed below is implemented natively (see [Memory Model Details](docs/MEMORY_MODEL.md)):

*   **Structured Metadata:** Every memory contains a `type`, `source`, `confidence`, `scope`, provenance (`change_agent`), and salience (`importance`, `decay_rate`).
*   **Verbatim, Non-Destructive Storage:** Memory content is stored exactly as written and **never altered in place** — the raw text is always retrievable byte-for-byte. Corrections don't overwrite: a superseded fact is *closed* (its validity interval ends) and the new fact is linked to it, so both the original wording and its full edit history stay queryable. You get true verbatim recall *and* an audit trail, not one or the other.
*   **Bitemporal History:** Distinguishes valid-time from transaction-time. Because superseded facts are closed rather than deleted, you can query what the agent believed at any specific point in time.
*   **Contradiction Management:** Conflicting facts are resolved automatically on write. The stale fact is marked as superseded, and confidence values are updated dynamically via Bayesian confidence posteriors.
*   **Self-Maintaining Lifecycle:** Implements memory decay, deduplication, automatic consolidation into higher-order beliefs, TTL expiry, and GDPR erasure.
*   **Write-Gating & Content Safety:** Filters out low-signal noise via an enrichment queue and content safety guardrails before storage.
*   **Explainable Retrieval:** Hybrid engine combining vector similarity, BM25 (FTS5), MMR diversity, and reranking. `memory_suggest` returns the exact score breakdown per result. (See [Confidence and Trust Guide](docs/CONFIDENCE_AND_TRUST.md)).
*   **Proven Accuracy:** On LongMemEval-S, M3 delivers **state-of-the-art retrieval for a local-first substrate — 99.2% session-hit-rate @ k=10 and 100% @ k=20** (no oracle routing), with the correct session as the **#1 result for ~92% of questions**. End-to-end QA accuracy is **92.0%** with no oracle metadata (see [Benchmarking Report](benchmarks/longmemeval/LME-S_Benchmarking_Report.md)).

---

## 📦 Installation

### The One-Liner (macOS & Linux)
```bash
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
```
*   *For Windows, please follow the [Windows Manual Installation Guide](docs/install_windows.md).*
*   *To install manually on any platform, refer to the [OS-Specific Install Instructions](INSTALL.md#tldr--manual-path-per-os) or examine the [installer script](https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh).*

### Developer Setup Wizard
If you are developing inside python environments:
```bash
pip install m3-memory
m3 setup
```
The `m3 setup` wizard automatically scans your `PATH` for active agents (Claude Code, Gemini CLI, OpenCode, OpenClaw), installs settings files/hooks, provisions the sovereign CPU embedder, and performs a system diagnostic.

### Integrating with AI Coding Tools

#### 🤖 Claude Code
Install as a plugin to unlock `/m3:*` slash commands, curation subagents, and automatic hooks:
```
/plugin marketplace add skynetcmd/m3-memory
/plugin install m3@skynetcmd
```
*See [Claude Code Plugin Reference](docs/claude_code_plugin.md) and [Claude.ai Connector Guide](docs/claude_ai_connector.md).*

#### 🪐 Google Antigravity
Install the plugin directly:
```bash
agy plugin install https://github.com/skynetcmd/m3-memory
```
*See [Antigravity Plugin Reference](docs/antigravity_plugin.md).*

#### 🦊 Hermes Agent
Run the wizard to automatically wire up optimal memory providers:
```bash
m3 setup
```
*See [Hermes Plugin Integration Guide](docs/HERMES.md).*

#### 🐍 Python / LangChain & LangGraph
Use M3 as a drop-in Mem0 replacement or LangMem backend:
```bash
pip install m3-memory[langchain]
```
*See [LangChain Integration Guide](docs/integrations/LANGCHAIN.md).*

---

### Manual MCP Server Configuration
To expose M3 to any Model Context Protocol host, add it to your configuration file:

```json
{
  "mcpServers": {
    "memory": {
      "command": "m3"
    }
  }
}
```

---

## 🎚️ Domain Gating: the Full Catalog Without the Context Cost

M3 gives you the full 100+ tool surface while occupying just **1.8% of a 200K context window** at startup — most MCP servers make you pay for every tool in every prompt. Tools are grouped into **9 domains** (`memory`, `chatlog`, `files`, `entity`, `agent`, `tasks`, `conversations`, `diagnostics`, `admin`) and loaded lazily.

Only the essential core set (~18, ~3,540 tokens) registers at startup. When your agent needs advanced functionality, it calls `tools_load_domain(domain="...")` to fetch the rest on demand — so a large catalog costs near-zero context until you actually use a domain.

| Gating Mode | Registered Tools | Tokens in Schema | % of 200K Window |
| :--- | :---: | :---: | :---: |
| **Lazy (Default)** | **~18** | **~3,540** | **1.8%** |
| Typical Active Session | 64 | ~17,975 | 9.0% |
| Eager Mode (`M3_TOOLS_LAZY=0`) | 109 | ~24,918 | 12.5% |

> 🛠️ *Note: If your client does not support dynamic tool registration, set the environment variable `M3_TOOLS_LAZY=0` to register all tools eagerly.*

---

## 🛡️ Sovereign & Air-Gapped Deployments

M3 operates completely offline by default.

### Sovereign Local Embedder
A high-performance BGE-M3 embedder runs locally after installation.
*   **Default:** **in-process** via the `m3-core-rs` native module (llama.cpp linked in-process, zero IPC — *not* a separate service you have to run or monitor). CPU execution using GGUF format (`_assets/models/bge-m3-Q4_K_M.gguf`). A local HTTP embed server on `127.0.0.1:8082` exists only as an automatic fallback if the in-process path can't load.
*   **Hardware Acceleration (GPU):** Execute `m3 embedder install-gpu` to compile with CUDA, Vulkan, or Metal.
*   **External Provider Fallback:** Set `EMBED_BASE_URL` to route requests to Ollama, LM Studio, or vLLM.

### Rust-Oxidized Performance Core
M3 includes an optional Rust performance module (`m3_core_rs`) that speeds up MMR re-ranking, batch cosine distance calculations, and FTS compilations by **90× to 800×**. If absent, M3 falls back to pure Python execution automatically. Disable with `M3_CORE_RS_DISABLE=1`. (See [Oxidation Benchmarks](docs/OXIDATION_BENCHMARKS.md)).

### Enterprise Security & Compliance
*   **FIPS 140-3 Ready:** Standardized encryption pathways allow routing through validated cryptographic modules (e.g., wolfSSL via `M3_FIPS_MODE=1`).
*   **Air-Gapped Install:** Supports installation without internet access via pre-compiled python wheels. (See [Sovereign Deployment Guide](docs/SOVEREIGN_DEPLOYMENT.md) & [FIPS Boundary Reference](docs/FIPS_MODULE_BOUNDARY.md)).
*   **Storage Location:** All config and data files reside under `~/.m3-memory` (configurable via `M3_MEMORY_ROOT`).

---

## 🔮 What M3 Does

*   **Memory Persistence:** Saves system architecture, project decisions, and preferences across tool boundaries using a local SQLite database.
*   **Autonomous Cognitive Loop:** Background worker (`m3_cognitive_loop.py`) that periodically sweeps chat logs to extract facts, reconcile contradictions, and construct an entity relationship graph.
*   **Hybrid Vector & Keyword Search:** Seamlessly merges vector space, Full-Text Search (FTS5 BM25), and MMR diversity.
*   **Hierarchical File Ingestion:** A dedicated 26-tool files domain reads directories, chunks files, extracts facts, and reviews staleness — with ~4× faster incremental re-ingest (unchanged sections reuse cached embeddings).
*   **Verbatim Chatlog Capture:** A dedicated 10-tool chatlog domain records conversation turns *before compaction*, so prior Claude/Gemini sessions stay searchable and nothing is lost to context-window truncation.
*   **Cross-Device Sync:** Optional PostgreSQL synchronization backend. Access the same memories on your laptop, desktop, or cloud environments.

---

## 📚 Documentation Index

| Quick & Core | Advanced & Architecture | Integrations & Compliance |
| :--- | :--- | :--- |
| 🚀 **[Getting Started Guide](docs/GETTING_STARTED.md)** | 🏗️ **[System Architecture](docs/ARCHITECTURE.md)** | 🧩 **[LangChain/LangGraph](docs/integrations/LANGCHAIN.md)** |
| ✨ **[Core Features](docs/CORE_FEATURES.md)** | 🔧 **[Technical Implementation](docs/TECHNICAL_DETAILS.md)** | 🧩 **[Hermes Agent](docs/HERMES.md)** |
| ⚙️ **[Environment Variables](docs/ENVIRONMENT_VARIABLES.md)** | 🧠 **[Memory Model Guide](docs/MEMORY_MODEL.md)** | 🛡️ **[Compliance Guide](docs/COMPLIANCE.md)** (GDPR, FISMA) |
| 🛠️ **[Operations Playbook](docs/OPERATIONS.md)** | ⚡ **[Rust Oxidation benchmarks](docs/OXIDATION_BENCHMARKS.md)** | 🛡️ **[FIPS Cryptographic Boundary](docs/FIPS_MODULE_BOUNDARY.md)** |
| 🤖 **[Agent Instructions & Rules](docs/AGENT_INSTRUCTIONS.md)** | 🔍 **[Myths & Facts Guide](docs/MYTHS_AND_FACTS.md)** | 🏠 **[Homelab Patterns](docs/HOMELAB_PATTERNS.md)** |
| 🧩 **[Tool Capability Matrix](docs/CAPABILITY_MATRIX.md)** | 🤖 **[AI Context Injection Profile](docs/llm-context.md)** | 🔢 **[Machine-Readable Features](docs/features.json)** |

### More Documentation

| Guide | Guide | Guide |
| :--- | :--- | :--- |
| 🗺️ [Roadmap](docs/ROADMAP.md) | 🔄 [Cross-Device Sync](docs/SYNC.md) | 👥 [Multi-Agent Orchestration](docs/MULTI_AGENT.md) |
| ⚖️ [Comparison vs Alternatives](docs/COMPARISON.md) | ❓ [FAQ](docs/FAQ.md) | 🔐 [Security Policy](docs/SECURITY.md) |
| 🩹 [Troubleshooting](docs/TROUBLESHOOTING.md) | ⌨️ [CLI Reference](docs/CLI_REFERENCE.md) | 📖 [API Reference](docs/API_REFERENCE.md) |
| 📁 [Files Memory](docs/FILES_MEMORY.md) | 💬 [Chat Log Subsystem](docs/CHATLOG.md) | ✨ [Enrichment Guide](docs/M3_ENRICH_GUIDE.md) |
| ⬆️ [Upgrade Guide](docs/HOW-TO-UPGRADE.md) | 🩺 [Health FAQ](docs/M3_HEALTH_FAQ.md) | 🧬 [Dual Embedding](docs/DUAL_EMBED.md) |
| 📜 [Changelog](CHANGELOG.md) | 🤝 [Code of Conduct](docs/CODE_OF_CONDUCT.md) | 🏗️ [Build Wheels](docs/BUILD_WHEELS.md) |

---

## 🎯 Who This Is For

### M3 is a great fit if...
*   **You use multiple desktop coding agents:** Interoperate Claude Code, Gemini, and Aider on a shared local history.
*   **You build with LangChain/LangGraph:** An advanced replacement for standard memory models, adding bitemporal queries, contradiction management, and local embeddings.
*   **You need security and compliance:** Built-in `gdpr_forget` and `gdpr_export` tools, air-gapped support, and audit logs.
*   **You value privacy:** Zero external cloud requests or subscriptions required.

### M3 is NOT a fit if...
*   You use **CrewAI** and want standard, framework-native memory models (use [Mem0](https://mem0.ai)).
*   You need a hosted SaaS dashboard with managed infrastructure (use [Letta](https://letta.ai)).
*   You only want transient in-session chat context that resets when you exit the terminal (rely on your agent's defaults).
*   **Your need is only contextual retrieval + a little user state:** if plain conversation history, RAG over a knowledge base, and a small structured user profile cover you, that's simpler to build and operate — persistent evolving memory earns its keep when users interact repeatedly *over time* and benefit from accumulated context.
*   **You require a server-based store as the system of record:** M3 is local-first — SQLite is always the source of truth. PostgreSQL is an optional sync/federation tier, not a drop-in replacement backend.

---

## 🛡️ Why Trust This

*   **Benchmarked Retrieval:** State-of-the-art for a local-first substrate — 99.2% session-hit-rate @ k=10, 100% @ k=20 on LongMemEval-S — with a published, reproducible methodology and no oracle routing. See [Benchmarks](#-benchmarks).
*   **Robust Coverage:** Verified with **1,283 tests across 154 test files** (~2,070 cases with parametrization) spanning search, sync, GDPR lifecycle, and files ingestion.
*   **Audit Reports:** Regular vulnerability reports (Bandit, secrets scans, pip-audit) published directly under [`docs/audits/`](docs/audits/).
*   **Explainable Retrieval:** No black-box queries; retrieval math is open, readable, and scoring parameters are outputted directly.
*   **Open Source:** Apache 2.0 licensed, free, with no SaaS walls or usage limits.

---

## 📊 Benchmarks

### Retrieval Recall (Session Hit-Rate @ k)
Evaluated on the 500-question [LongMemEval-S](https://github.com/xiaowu0162/LongMemEval) dataset under default server configurations:

| Retrieve Depth (k) | Session Hit-Rate (SHR) | Success Count | vs. Prior Version |
| :---: | :---: | :---: | :---: |
| 5 | **98.2%** | 491 / 500 | +2.0pp |
| 10 (Default) | **99.2%** | 496 / 500 | +2.4pp |
| 20 | **100.0%** | 500 / 500 | First Report |

### End-to-End QA Accuracy
**92.0% accuracy** (460/500 correct responses) with zero oracle metadata routing:

| Question Domain | Count (n) | Accuracy |
| :--- | :---: | :---: |
| single-session-user | 70 | 94.3% |
| single-session-assistant | 56 | 96.4% |
| single-session-preference | 30 | 80.0% |
| multi-session | 133 | 87.2% |
| temporal-reasoning | 133 | 95.5% |
| knowledge-update | 78 | 93.6% |
| **Overall Summary** | **500** | **92.0%** |

*Methodology and reproducibility details are located in the [LongMemEval-S Benchmarking Report](benchmarks/longmemeval/LME-S_Benchmarking_Report.md).*

---

## 🧰 Core Tools

While M3 features 100+ tools, these five serve as your primary interface:

| Tool Name | Operation Description |
| :--- | :--- |
| `memory_write` | Save a specific fact, project preference, or technical configuration. |
| `memory_search` | Run hybrid keyword (BM25) and semantic vector search. |
| `memory_update` | Edit existing facts to keep memory accurate. |
| `memory_suggest` | Query memories alongside a mathematically explicit score breakdown. |
| `memory_get` | Fetch details of a single memory using its unique ID. |

*Refer to the [Agent Instructions Guide](docs/AGENT_INSTRUCTIONS.md) and [Full MCP Tool Catalog](docs/MCP_TOOLS.md) for complete parameter definitions.*

---

## 🤖 For AI Agents

You can drop the agent ruleset file [`examples/AGENT_RULES.md`](examples/AGENT_RULES.md) into your workspace to teach your agent best practices (e.g., query before writing, update existing records instead of duplicating).

### Command Installation Prompts
Copy and paste these prompts into your terminal client to let your agent set up M3 for you:

#### Claude Code Prompt
```text
Install m3-memory for persistent memory. Run: pip install m3-memory
Then add {"mcpServers":{"memory":{"command":"m3"}}} to my
~/.claude/settings.json under "mcpServers". For best retrieval, ensure
Ollama is running with qwen3-embedding:0.6b (optional, falls back
to keyword search without it). Then use /mcp to verify the memory server loaded.
```

#### Gemini CLI Prompt
```text
Install m3-memory for persistent memory. Run: pip install m3-memory
Then add {"mcpServers":{"memory":{"command":"m3"}}} to my
~/.gemini/settings.json under "mcpServers". For best retrieval, ensure
Ollama is running with qwen3-embedding:0.6b (optional, falls back
to keyword search without it).
```

#### Active Chatlog Capture Plugin
To configure instant conversation logging and backup, tell your active coding agent:
```text
Install the m3-memory chat log subsystem.
```
The agent executes `bin/chatlog_init.py` and configures execution triggers (see [Chat Log Architecture Guide](docs/CHATLOG.md)).

---

## 🎬 See it in action

### Contradiction Detection & Automatic Resolution
<p align="center">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/demo_contradiction.svg" alt="Contradiction Demo" width="100%">
</p>

### Hybrid Search Scoring Details
<p align="center">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/demo_search.svg" alt="Hybrid Search Demo" width="100%">
</p>

### Multi-Device Database Sync
<p align="center">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/demo_sync.svg" alt="Sync Demo" width="100%">
</p>

---

## 💬 Community

[![Discord Badge](https://img.shields.io/badge/Discord-M3_Memory-5865F2?logo=discord&logoColor=white&style=flat-square)](https://discord.gg/ZcJ3EGC99B)
&nbsp;
[![GitHub Issues Badge](https://img.shields.io/badge/GitHub-Issues-181717?logo=github&style=flat-square)](https://github.com/skynetcmd/m3-memory/issues)

[How to Contribute](docs/CONTRIBUTING.md) · [Good First Issues](docs/GOOD_FIRST_ISSUES.md)

---

## 📜 License & Attributions

This project is licensed under the Apache License 2.0. See [LICENSE](LICENSE) for details.

### Asset & Icon Credits
The provider badges under [`docs/badges/`](docs/badges/) embed small logo glyphs:
* **OpenClaw & OpenCode icons** are from the MIT-licensed [LobeHub icon set](https://github.com/lobehub/lobe-icons) (`lobe-icons`).
* **The Hermes badge** uses a generic caduceus glyph.

See [NOTICE](NOTICE) for the full third-party attribution list.

---

### ⭐ Star History

<a href="https://star-history.com/#skynetcmd/m3-memory&Date">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/star-history.svg" alt="Star history for skynetcmd/m3-memory" width="100%">
</a>

<sup>Chart regenerated on a schedule by [`.github/workflows/star-history.yml`](.github/workflows/star-history.yml) using the repo's own token — no third-party embed. Click through for the live interactive version.</sup>

<!-- mcp-name: io.github.skynetcmd/m3-memory -->

