# 🧠 M3 Memory — Give Your AI Agents Real Memory (in 60 seconds)

<p align="center">
  <img src="docs/logo.svg" alt="M3 Memory Logo" width="600">
</p>

<p align="center">
  <a href="https://github.com/skynetcmd/m3-memory/stargazers"><img alt="GitHub Stars" src="https://img.shields.io/github/stars/skynetcmd/m3-memory?style=social"></a>
  <a href="https://github.com/skynetcmd/m3-memory/network/members"><img alt="GitHub Forks" src="https://img.shields.io/github/forks/skynetcmd/m3-memory?style=social"></a>
  <a href="https://discord.gg/ZcJ3EGC99B"><img alt="Discord" src="https://img.shields.io/badge/Discord-M3--Memory%20Community-5865F2?logo=discord&logoColor=white"></a>
</p>

<p align="center">
  <a href="https://pypi.org/project/m3-memory/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/m3-memory.svg"></a>
  <a href="https://pypi.org/project/m3-memory/"><img alt="PyPI downloads" src="https://img.shields.io/pypi/dm/m3-memory.svg"></a>
  <a href="https://www.python.org"><img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11+-blue.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green.svg"></a>
  <a href="https://modelcontextprotocol.io"><img alt="MCP 25 tools" src="https://img.shields.io/badge/MCP-25_tools-orange.svg"></a>
  <a href=".github/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/badge/CI-lint%20%7C%20typecheck%20%7C%20test-brightgreen.svg"></a>
  <img alt="Platform" src="https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey.svg">
</p>

**M3 Memory lets Claude Code, Gemini CLI, and Aider remember users, context, and past work — fully local, no cloud, no APIs.**

- 🔒 **100% private** — runs entirely on your machine
- ⚡ **Works in one config line** — no setup beyond `pip install`
- 🧠 **Persistent memory** across sessions and devices

---

## ⚡ Quick Start (1 minute)

```bash
pip install m3-memory
```

Add to your MCP config:

```json
{
  "mcpServers": {
    "memory": { "command": "mcp-memory" }
  }
}
```

Restart your agent → it now has memory. ✅ Claude Code &nbsp;✅ Gemini CLI &nbsp;✅ Aider

**Done.**

---

## 🧠 What This Feels Like

You tell your agent:

> "My server runs on port 8080"

Later:

> "Actually it's 9000"

M3 Memory automatically:
- Detects the contradiction
- Updates the fact
- Keeps the full history

Next session:

> "What port is my server on?"
> → **"9000 (updated from 8080)"**

No prompts. No manual logic. Just works.

---

Or picture this: You're debugging a deployment issue at a coffee shop. Claude Code recalls the architecture decisions from last week, the server configs from yesterday, and the troubleshooting steps that worked before — all from local SQLite, no internet required. Later, at your desktop at home, Gemini CLI picks up exactly where you left off. Same memories. Same knowledge graph. Synced the moment you hit the local network.

> **Your AI's memory belongs to you, lives on your hardware, and follows you across every device and every agent.**

---

## 🎯 Who This Is For

**Use M3 Memory if you:**
- Build with MCP agents (Claude Code, Gemini CLI, Aider)
- Want persistent memory across sessions and devices
- Care about privacy — no cloud, no API keys, works offline
- Don't want to build memory infrastructure yourself

**Not for you if:**
- You only need short-term chat context
- You're building LangChain/CrewAI pipelines (consider [Mem0](https://mem0.ai))
- You want a full stateful agent runtime (consider [Letta](https://letta.ai))

---

## 🎯 Use Cases

| | |
|---|---|
| 🤖 **Coding agents** | Remember architecture decisions, configs, debugging steps across sessions |
| 🧠 **Personal assistants** | Persist user preferences, goals, and history long-term |
| 🧑‍💻 **Dev workflows** | Track environment changes, server configs, and fixes over time |
| 🧪 **Research agents** | Build evolving knowledge that compounds across sessions |

---

## ✨ Core Features

### 🔍 Hybrid Search
**TL;DR: Better results than vector search alone.**
FTS5 keyword + semantic vector + MMR diversity re-ranking in one pipeline. Full score breakdown via `memory_suggest`.

### 🚫 Automatic Contradiction Detection
**TL;DR: Old facts fix themselves.**
Write conflicting info → M3 detects it, supersedes the old memory, records a `supersedes` relationship, preserves full history. No stale data. No manual cleanup.

### ⏳ Bitemporal History
**TL;DR: Time-travel debugging.**
Query `as_of="2026-01-15"` to see exactly what your agent believed on any past date. Essential for compliance and debugging.

### 🕸️ Knowledge Graph
**TL;DR: Memories connect automatically.**
Related facts link on write (cosine > 0.7). Seven relationship types. Traverse up to 3 hops with `memory_graph`.

### 🔄 Cross-Device Sync
**TL;DR: Same memory everywhere.**
Write on laptop → continue on desktop. Bi-directional delta sync: SQLite ↔ PostgreSQL ↔ ChromaDB.

### 🛡️ GDPR Built-In
**TL;DR: Compliance out of the box.**
`gdpr_forget` (Article 17 hard delete) + `gdpr_export` (Article 20 portable JSON) as first-class MCP tools.

### 🔒 Fully Local + Private
**TL;DR: Your data never leaves your machine.**
Local embeddings via Ollama, LM Studio, or any OpenAI-compatible server. Zero API costs. Works offline.

---

## 🧰 Core Tools (start here)

You don't need all 25. Start with these:

- `memory_write` — store a memory
- `memory_search` — retrieve relevant memories
- `memory_suggest` — ranked results with score breakdown
- `memory_get` — fetch by ID
- `memory_update` — refine existing knowledge

→ [Full list of 25 tools](./ARCHITECTURE.md)

---

## 🆚 How It Compares

| Feature | **M3-Memory** | **Mem0** | **Letta** | **LangChain Memory** |
|---------|:------------:|:--------:|:---------:|:--------------------:|
| **Local-first** | ✅ 100% | ⚠️ partial | ✅ good | ⚠️ partial |
| **MCP native** | ✅ 25 tools | ⚠️ wrappers | ⚠️ indirect | ❌ no |
| **Contradiction handling** | ✅ automatic | ⚠️ LLM-based | ⚠️ agent-driven | ⚠️ manual |
| **GDPR tools** | ✅ built-in | ⚠️ supported | ⚠️ via tools | ❌ custom |
| **Cross-device sync** | ✅ built-in | ⚠️ limited | ⚠️ git-based | ⚠️ limited |
| **Setup** | ✅ 1 line | ⚠️ SDK needed | ❌ full runtime | ❌ framework only |
| **Cost** | ✅ free, MIT | ⚠️ $249/mo Pro | ⚠️ OSS + SaaS | ✅ free |

**Choose M3 Memory** if you want simple, private, MCP-native memory that just works — no framework lock-in.

→ Full breakdown: [COMPARISON.md](./COMPARISON.md)

---

## 🏗️ Architecture

```mermaid
graph TD
    subgraph "🤖 AI Agents"
        C[Claude Code]
        G[Gemini CLI]
        A[Aider / OpenClaw]
    end

    subgraph "🌉 MCP Bridge"
        MB[memory_bridge.py — 25 MCP tools]
    end

    subgraph "💾 Storage Layers"
        SQ[(SQLite — Local L1)]
        PG[(PostgreSQL — Sync L2)]
        CH[(ChromaDB — Federated L3)]
    end

    C & G & A <--> MB
    MB <--> SQ
    SQ <-->|Bi-directional Delta Sync| PG
    SQ <-->|Push/Pull| CH
```

### The Memory Write Pipeline

```mermaid
sequenceDiagram
    participant A as Agent
    participant M as M3 Memory
    participant L as Local LLM
    participant S as SQLite

    A->>M: memory_write(content)
    M->>M: Safety Check (XSS / injection / poisoning)
    M->>L: Generate Embedding
    L-->>M: Vector [0.12, -0.05, ...]
    M->>M: Contradiction Detection
    M->>M: Auto-Link Related Memories
    M->>M: SHA-256 Content Hash
    M->>S: Store Memory + Vector
    S-->>M: Success
    M-->>A: Created: <uuid>
```

---

## 🎬 See It in Action

> **Demo 1 — Automatic contradiction resolution**
> ![Demo: agent writes conflicting facts — old memory auto-superseded, full history preserved](docs/demo_contradiction.gif)

> **Demo 2 — Hybrid search across 1,000 memories**
> ![Demo: memory_search returns FTS5 + vector + MMR ranked results with score breakdown](docs/demo_search.gif)

> **Demo 3 — Cross-device sync**
> ![Demo: memory written on laptop appears on desktop via SQLite→PostgreSQL bidirectional sync](docs/demo_sync.gif)

*GIFs coming soon — [contribute a recording](./CONTRIBUTING.md) or watch [#showcase](https://discord.gg/ZcJ3EGC99B).*

---

## 📚 Documentation

| File | Purpose |
|------|---------|
| [QUICKSTART.md](./QUICKSTART.md) | Plain-English guide — new here? Start here |
| [CORE_FEATURES.md](./CORE_FEATURES.md) | Feature overview |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Full system internals + all 25 MCP tools |
| [TECHNICAL_DETAILS.md](./TECHNICAL_DETAILS.md) | Deep dive: search pipeline, schema, sync, security |
| [COMPARISON.md](./COMPARISON.md) | M3 vs Mem0 vs Letta vs LangChain Memory |
| [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md) | Config and credential setup |
| [ROADMAP.md](./ROADMAP.md) | Upcoming milestones |
| [CHANGELOG.md](./CHANGELOG.md) | Release history |

---

## 🤝 Community

[![Discord](https://img.shields.io/badge/Join%20Discord-M3--Memory%20Community-5865F2?logo=discord&logoColor=white&style=for-the-badge)](https://discord.gg/ZcJ3EGC99B)

Get help, share your setup, and follow development. **M3_Bot** is live — use `!ask <question>` in any channel.

---

## 🛣️ Roadmap

| Milestone | Highlights |
|-----------|------------|
| **v0.2** | Docker image · auto MCP Registry · CLI polish |
| **v0.3** | Local web dashboard · Prometheus metrics · search explain mode |
| **v0.4** | Multi-agent shared namespaces · P2P encrypted sync |
| **v1.0** | Public benchmark suite · stable Python SDK · full docs site |

Vote on features → [ROADMAP.md](./ROADMAP.md)

---

## 🧩 Project Structure

```
bin/          MCP bridge, SDK, and automation scripts
memory/       SQLite database and migrations
docs/         Architecture diagrams and install guides
examples/     Demo notebooks and mcp.json snippets
tests/        End-to-end test suite (41 tests)
```

---

## 🤝 Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) · Good first issues: [GOOD_FIRST_ISSUES.md](./GOOD_FIRST_ISSUES.md)

---

[![Star History Chart](https://api.star-history.com/svg?repos=skynetcmd/m3-memory&type=Date)](https://star-history.com/#skynetcmd/m3-memory&Date)

**Your AI should remember. Your data should stay yours.**

*M3 Memory: the foundation for agents that don't forget.*

<!-- mcp-name: io.github.skynetcmd/m3-memory -->
