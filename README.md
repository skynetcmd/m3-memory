![M3 Memory]
<p align="center">
  <a href="https://github.com/skynetcmd/m3-memory">
    <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/M3-banner.jpg" alt="M3 Memory" width="100%">
  </a>
</p>

# M3 Memory

Local-first Agentic Memory Layer Framework for MCP Agents • 74 tools • Hybrid search (FTS5 + vector + MMR) • GDPR • FIPS 140-3 ready • 100% local

> **"Wait, you remember that?"** — Stop re-explaining your project to your AI. Give it a long-term brain that stays 100% on your machine.
>
> 🚀 **[New to M3? Start here with our 5-minute "Human-First" guide.](docs/GETTING_STARTED.md)**

<p align="center">
  <a href="https://pypi.org/project/m3-memory/"><img alt="PyPI" src="https://img.shields.io/pypi/v/m3-memory?style=flat-square"></a>
  <a href="https://pypi.org/project/m3-memory/"><img alt="Downloads" src="https://img.shields.io/pypi/dm/m3-memory?style=flat-square"></a>
  <a href="https://www.python.org"><img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square"></a>
  <a href="https://github.com/skynetcmd/m3-memory/blob/main/LICENSE"><img alt="Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-green?style=flat-square"></a>
  <a href="https://modelcontextprotocol.io"><img alt="MCP" src="https://img.shields.io/badge/MCP-74_tools-orange?style=flat-square"></a>
  <img alt="macOS" src="https://img.shields.io/badge/macOS-000000?style=flat-square&logo=apple&logoColor=white">
  <img alt="Windows" src="https://img.shields.io/badge/Windows-0078D4?style=flat-square&logo=windows&logoColor=white">
  <img alt="Linux" src="https://img.shields.io/badge/Linux-FCC624?style=flat-square&logo=linux&logoColor=black">
</p>

Works with Claude Code, Gemini CLI, Aider, OpenCode, and any MCP-compatible agent. Quick one-line command to have your agent install chat log sub-system which saves verbatim chat log info, before compaction, with zero lag/latency and 100% retrieval recall. Just tell your AI agent "install m3-memory chat log sub-system" and your agent will automatically install it with all the proper hooks with some minimal customization questions from you (you can accept the default answers).

---

## 📦 Install

```bash
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
```

Installs on macOS or Linux with the single command above. Use this to [install on Windows](https://github.com/skynetcmd/m3-memory/blob/main/docs/install_windows.md). Use this link to [install manually](https://github.com/skynetcmd/m3-memory/blob/main/INSTALL.md#tldr--manual-path-per-os) and this to [examine the script](https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh) and what it does.

**Claude Code users** can also install as a plugin instead — gets you 15 `/m3:*` slash commands, two curator subagents (`m3:curate-memory`, `m3:curate-chatlog`), and auto-wired hooks:

```
/plugin marketplace add skynetcmd/m3-memory
/plugin install m3@skynetcmd
```

[Plugin reference](https://github.com/skynetcmd/m3-memory/blob/main/docs/claude_code_plugin.md) · [Claude.ai (web/desktop) connector](https://github.com/skynetcmd/m3-memory/blob/main/docs/claude_ai_connector.md)

---

Add to your MCP config:

```json
{
  "mcpServers": {
    "memory": { "command": "mcp-memory" }
  }
}
```

An embedder is **optional but highly recommended**. M3 functions as a pure keyword-search (FTS5/BM25) memory without one, but adding an embedder enables semantic retrieval and high-performance **hybrid search**.

### 🚀 Recommended: Integrated Sovereign Setup
For the best experience (Windows, Linux, or Apple Silicon), use our integrated, self-contained installer. It sets up a private instance of **LM Studio** and our preferred model, **BGE-M3**, directly in your project folder.

```bash
mcp-memory install-embedder
```

### 🍎 Older Intel Macs
If you are on an older Intel-based Mac, **LM Studio is not supported**. We recommend using **Ollama** instead:

```bash
ollama pull qwen3-embedding:0.6b && ollama serve
```

### Other Options
You can also use a standalone [Ollama](https://ollama.com) or [LM Studio](https://lmstudio.ai) instance. Qwen3-Embedding-0.6B (1024-dim) and BGE-M3 are the models M3 Memory is tuned for. If you use a different model, set `EMBED_MODEL` in your environment. If no embedder is detected at startup, M3 will automatically fall back to keyword-only mode.

Want auto-classification, summarization, and consolidation? Load a small chat model alongside the embedder (e.g. `qwen2.5:0.5b` via Ollama, or any 0.5–1B instruct GGUF in LM Studio / llama.cpp). M3 auto-selects it; embedding-only features work without it. See [docs/QUICKSTART.md → Optional: load a small chat model](docs/QUICKSTART.md#optional-load-a-small-chat-model-for-enrichment).

> **Optional — Rust core (`m3-memory[oxidation]`).** A Rust compute core ([`m3-core-rs`](https://github.com/skynetcmd/m3-core-rs)) can take over hot-path work — hashing, cosine/MMR ranking, redaction, and in-process llama.cpp embeddings. Install with `pip install m3-memory[oxidation]` (needs a Rust toolchain + maturin). Every path falls back to pure Python when the extra is absent, and `M3_CORE_RS_DISABLE=1` forces the Python path at runtime. See [docs/ENVIRONMENT_VARIABLES.md → Project Oxidation](docs/ENVIRONMENT_VARIABLES.md).

Restart your agent. Done!

---

## 🛡️ Sovereign Embedder (Air-gapped / Offline)

M3-Memory can be installed as a completely self-contained "memory appliance" for secure or air-gapped environments. This mode includes the embedding engine (LM Studio) and the BGE-M3 model directly in the project folder—no internet connection required after the initial clone.

**See the [🛡️ Sovereign & Air-Gapped Deployment Guide](docs/SOVEREIGN_DEPLOYMENT.md) for full instructions.**

### 1. Unified Setup
If you are installing from a USB drive or in an offline room, run:

```bash
mcp-memory install-embedder
```

**Need a clean slate?** You can wipe and reinstall the system payload at any time with:

```bash
mcp-memory reinstall
```

### 2. Configuration
By default, M3-Memory stores its configuration, repository payload, and backups in `~/.m3-memory`. You can override this by setting the `M3_MEMORY_ROOT` environment variable.

### 3. What it does:
*   **Zero-Dependency:** Operates entirely via file-system migration. No `curl`, `pip`, or external calls.
*   **Hardware Optimized:** Automatically detects your OS and architecture (Apple Silicon, Windows x64, Linux x64, or Linux ARM64) and moves the matching binaries.
*   **Surgical Purge:** Once you choose your mode (CPU vs. GPU), it permanently deletes all unused OS binaries and model variants. It will report exactly how many **MB of unneeded setup files were deleted**.
*   **Stealth Portability:** Installs into a hidden `.m3-lmstudio` directory. If you move the project folder, M3 **self-heals** its absolute paths in Windows Startup, macOS LaunchAgents, or Linux Systemd units.
*   **Clean Integration:** Locks the local server to `127.0.0.1:8081` and auto-wires your `.env` file.

### 3. Existing LM Studio instances
If a local instance of LM Studio is already detected, the installer will:
1.  Offer to link to your existing server instead of installing a separate one.
2.  Warn if a different embedder is loaded (e.g., `nomic-embed-text`) and explain that **re-embedding** (`mcp-memory re-embed`) only applies to M3-owned data.
3.  Instruct you on how to manually load **bge-m3** for optimal retrieval.

---

## 🔮 What happens next (benefits of use)

You're at a coffee shop on your MacBook, asking Claude to debug a deployment issue. It remembers the architecture decisions you made last week, the server configs you stored yesterday, and the troubleshooting steps that worked last time — all from local SQLite, no internet required.

Later, you're at your Windows desktop at home with Gemini CLI, and it picks up exactly where you left off. Same memories, same context, same knowledge graph. You didn't copy files, didn't export anything, didn't push to someone else's cloud. Your PostgreSQL sync handled everything in the background the moment your laptop hit the local network.

---

## 💡 Why this exists

Most AI agents don't persist state between sessions. You re-paste context, re-explain architecture, re-correct mistakes. When facts change, the agent has no mechanism to update what it "knows."

M3 Memory gives agents a structured, persistent memory layer that handles this.

---

## ⚡ What it does

**Autonomous cognitive loop** — optional background worker (`m3_cognitive_loop.py`) that extracts facts, resolves contradictions, and links entities while you sleep. Turns raw chat logs into a refined knowledge graph without human intervention.

**Persistent memory** — facts, decisions, preferences survive across sessions. Stored in local SQLite.

**Hybrid retrieval** — FTS5 keyword matching + semantic vector similarity + MMR diversity re-ranking. Automatic, no tuning required.

**Contradiction handling** — conflicting facts are automatically superseded. Bitemporal versioning preserves the full history.

**Knowledge graph** — related memories linked automatically on write. Nine relationship types, 3-hop traversal. Entity extraction (`entity_search`, `entity_get`) supplements the graph with first-class people / places / things resolution.

**Zero-config local install** — `pip install m3-memory` plus one line in your MCP config, or `mcp-memory install-m3` for a one-command setup that wires settings.json, hooks, and the chatlog subsystem in one shot. SQLite stores everything locally — no external databases, no cloud calls, no API costs. Works offline.

**Cross-device sync** — optional, easy-to-add bi-directional delta sync via PostgreSQL or ChromaDB, with manifest-driven multi-DB support for fleet deployments. Set one environment variable and your memories follow you across machines.

---

## 📚 Learn more

| | |
|---|---|
| 🚀 **[Getting started](docs/GETTING_STARTED.md)** | 👥 **[Multi-agent orchestration](docs/MULTI_AGENT.md)** |
| ✨ **[Core features](docs/CORE_FEATURES.md)** | 🧩 **[Multi-agent example](examples/multi-agent-team/README.md)** |
| 🏗️ **[System design](docs/ARCHITECTURE.md)** | ⚖️ **[Compare M3 to alternatives](docs/COMPARISON.md)** ([sovereign substrates table](docs/M3_Comparison_Table.md)) |
| 🔧 **[Implementation details](docs/TECHNICAL_DETAILS.md)** | ⚙️ **[Configuration](docs/ENVIRONMENT_VARIABLES.md)** |
| 🤖 **[Agent rules + all 74 tools](docs/AGENT_INSTRUCTIONS.md)** | 🛡️ **[Compliance & assurance](docs/COMPLIANCE.md)** (FISMA, CMMC, GDPR) |
| 🏠 **[Homelab patterns](docs/HOMELAB_PATTERNS.md)** | 🔍 **[Myths & facts](docs/MYTHS_AND_FACTS.md)** (verify claims about M3) |
| 🗺️ **[Roadmap](docs/ROADMAP.md)** | |

---

## 🎯 Who this is for

### M3 is a good fit if…

| | |
|---|---|
| 🤖 **You use coding agents** | Claude Code, Gemini CLI, Aider, OpenCode, or any MCP-compatible agent. Non-MCP clients work too via the built-in HTTP proxy. |
| 👥 **You run multiple agents** | Coordinating Claude + Gemini + a background worker on a shared local store, with handoffs and per-agent scoping. |
| 🛡️ **You need compliance primitives** | `gdpr_forget` / `gdpr_export` as MCP tools, bitemporal valid-time / transaction-time, audit trail, no telemetry. |
| 💾 **You want pure local-first** | Single-file SQLite. Works offline. No external database, no cloud calls, no API costs by default. |
| 🌐 **You want memory across devices** | Optional bi-directional delta sync via PostgreSQL or ChromaDB — your data, your hardware. |

### M3 is **not** the right tool if…

| | Try instead |
|---|---|
| You're building LangChain / LangGraph / CrewAI pipelines and want framework-native memory | [Mem0](https://mem0.ai), [LangChain Memory / LangMem](https://python.langchain.com/docs/modules/memory/) |
| You want a hosted agent runtime with managed scaling, dashboards, and SLAs | [Letta](https://letta.ai), [Mem0 Pro](https://mem0.ai) |
| Pure retrieval-accuracy is your only criterion (M3 is mid-pack at 89.0% LME-S) | [agentmemory](https://github.com/agentmemory) (96.2%), [Hindsight](https://github.com/vectorize-io/hindsight) |
| You only need in-session chat context that's discarded after the conversation | Your agent's built-in conversation buffer; M3 is overkill |

---

## 🛡️ Why trust this

| | |
|---|---|
| **74 MCP tools** | Memory, search, GDPR, refresh lifecycle — plus agent registry, handoffs, notifications, tasks, entity graph, fact enrichment, and chat-log capture for multi-agent orchestration |
| **193 end-to-end tests** | Covering write, search, contradiction, sync, GDPR, maintenance, and orchestration paths |
| **Explainable retrieval** | `memory_suggest` returns vector, BM25, and MMR scores per result |
| **SQLite core** | No external database required. Single-file, portable, inspectable |
| **GDPR compliance** | `gdpr_forget` (Article 17) and `gdpr_export` (Article 20) as built-in tools — see [compliance & assurance](docs/COMPLIANCE.md) for FISMA / CMMC alignment too |
| **Self-maintaining** | Automatic decay, dedup, orphan pruning, retention enforcement |
| **Audited security posture** | Periodic Bandit + pip-audit + secrets-scan reports published under [`docs/audits/`](docs/audits/); CI gates on core-dep CVEs |
| **Apache 2.0 licensed** | Free. No SaaS tier, no usage limits, no lock-in |

> 🧭 **Maturity, honestly.** The core (storage, retrieval, GDPR, MCP tools, sync) is stable and covered by the test suite. The newer enrichment + reflector pipeline matured rapidly through 2026-Q2 and has live-fire experience behind it but is still iterating. **Production-ready for personal, homelab, and multi-agent developer workflows today.** For regulated workloads, do your own evaluation against your specific use case — and we recommend that against any memory tool, not just M3. See [docs/MYTHS_AND_FACTS.md](docs/MYTHS_AND_FACTS.md) for what we *don't* claim.

---

## 📊 Benchmarks

**89.0%** on [LongMemEval-S](https://github.com/xiaowu0162/LongMemEval) (445/500 correct) — a 500-question evaluation of long-horizon conversational memory. Without oracle metadata: **74.8%** (smart retrieval) to **68.0%** (fixed-k baseline).

| Question type | n | Accuracy |
|---|---|---|
| single-session-user | 70 | 91.4% |
| single-session-assistant | 56 | 94.6% |
| single-session-preference | 30 | 93.3% |
| multi-session | 133 | 85.0% |
| temporal-reasoning | 133 | 86.5% |
| knowledge-update | 78 | 92.3% |
| **Overall** | **500** | **89.0%** |

Full methodology, ablations, and honest caveats: [`benchmarks/longmemeval/LME-S_Benchmarking_Report.md`](benchmarks/longmemeval/LME-S_Benchmarking_Report.md). 
LoCoMo audit pending — see [`benchmarks/locomo/README.md`](benchmarks/locomo/README.md).

> 🔍 **Verifying claims about M3.** If a third-party AI assistant has described M3 with features or scores that don't match what's documented here, it's almost certainly hallucinating. See [`docs/MYTHS_AND_FACTS.md`](docs/MYTHS_AND_FACTS.md) for the source-of-truth list of what M3 actually implements (and what it doesn't).

---

## 🧰 Core tools

Most sessions use three tools. The rest is there when you need it.

| Tool | Purpose |
|------|---------|
| `memory_write` | Store a fact, decision, preference, config, or observation |
| `memory_search` | Retrieve relevant memories (hybrid search) |
| `memory_update` | Refine existing knowledge |
| `memory_suggest` | Search with full score breakdown |
| `memory_get` | Fetch a specific memory by ID |

All 74 tools are documented in [docs/AGENT_INSTRUCTIONS.md](docs/AGENT_INSTRUCTIONS.md) and the full inventory lives in [docs/MCP_TOOLS.md](docs/MCP_TOOLS.md).

---

## 🤖 For AI agents

M3 Memory exposes 74 MCP tools for storing, searching, updating, and linking knowledge — including conversation grouping, a refresh lifecycle for aging memories, agent registry, handoffs, notifications, tasks, entity-graph extraction, fact enrichment, and chat-log capture for multi-agent orchestration. Any MCP-compatible agent can use them automatically.

To teach your agent best practices (search before answering, write aggressively, update instead of duplicating), drop the compact rules file into your project:

```
examples/AGENT_RULES.md
```

Full tool reference with all parameters and behaviors: [docs/AGENT_INSTRUCTIONS.md](docs/AGENT_INSTRUCTIONS.md)

---

## 🪄 Let your agent install it

Already inside Claude Code or Gemini CLI? Paste one of these prompts:

**Claude Code:**
```
Install m3-memory for persistent memory. Run: pip install m3-memory
Then add {"mcpServers":{"memory":{"command":"mcp-memory"}}} to my
~/.claude/settings.json under "mcpServers". For best retrieval, ensure 
Ollama is running with qwen3-embedding:0.6b (optional, falls back 
to keyword search without it). Then use /mcp to verify the memory server loaded.
```

**Gemini CLI:**
```
Install m3-memory for persistent memory. Run: pip install m3-memory
Then add {"mcpServers":{"memory":{"command":"mcp-memory"}}} to my
~/.gemini/settings.json under "mcpServers". For best retrieval, ensure 
Ollama is running with qwen3-embedding:0.6b (optional, falls back 
to keyword search without it).
```

After install, test it:
```
Write a memory: "M3 Memory installed successfully on [today's date]"
Then search for: "M3 install"
```

### Add the chat log subsystem

Want auto-capture of every Claude Code / Gemini CLI / OpenCode / Aider conversation into a searchable, promotable chat log store? Once m3-memory is wired up, just say:

```
Install the m3-memory chat log subsystem.
```

The agent runs `bin/chatlog_init.py`, wires the host-agent hook, and installs the embed sweeper schedule. See [docs/CHATLOG.md](docs/CHATLOG.md) for the architecture and ops guide.

---

## 🎬 See it in action

### Contradiction detection
<p align="center">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/demo_contradiction.svg" alt="Demo: contradiction detection and automatic resolution" width="100%">
</p>

### Hybrid search with scores
<p align="center">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/demo_search.svg" alt="Demo: hybrid search with score breakdown" width="100%">
</p>

### Cross-device, cross-platform sync
<p align="center">
  <img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/demo_sync.svg" alt="Demo: cross-device, cross-platform memory sync" width="100%">
</p>

---

## 💬 Community

[![Discord](https://img.shields.io/badge/Discord-M3_Memory-5865F2?logo=discord&logoColor=white&style=flat-square)](https://discord.gg/ZcJ3EGC99B)
&nbsp;
[![GitHub Issues](https://img.shields.io/badge/GitHub-Issues-181717?logo=github&style=flat-square)](https://github.com/skynetcmd/m3-memory/issues)
&nbsp;
[Contributing](docs/CONTRIBUTING.md) · [Good first issues](docs/GOOD_FIRST_ISSUES.md)

---

[![Star History](https://api.star-history.com/svg?repos=skynetcmd/m3-memory&type=Date)](https://star-history.com/#skynetcmd/m3-memory&Date)

<!-- mcp-name: io.github.skynetcmd/m3-memory -->
