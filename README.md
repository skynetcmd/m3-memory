<p align="center">
  <a href="https://github.com/skynetcmd/m3-memory">
    <img src="docs/M3-banner.jpg" alt="M3 Memory" width="100%">
  </a>
</p>

# M3 Memory

Persistent, local memory for MCP agents.

Your agent forgets everything between sessions. M3 Memory fixes that. Install it, add one line to your MCP config, and your agent remembers across sessions, detects contradictions, and keeps its own knowledge current — all on your hardware, fully offline.

<p align="center">
  <a href="https://pypi.org/project/m3-memory/"><img alt="PyPI" src="https://img.shields.io/pypi/v/m3-memory?style=flat-square"></a>
  <a href="https://pypi.org/project/m3-memory/"><img alt="Downloads" src="https://img.shields.io/pypi/dm/m3-memory?style=flat-square"></a>
  <a href="https://www.python.org"><img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square"></a>
  <a href="LICENSE"><img alt="Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-green?style=flat-square"></a>
  <a href="https://modelcontextprotocol.io"><img alt="MCP" src="https://img.shields.io/badge/MCP-25_tools-orange?style=flat-square"></a>
  <img alt="macOS" src="https://img.shields.io/badge/macOS-000000?style=flat-square&logo=apple&logoColor=white">
  <img alt="Windows" src="https://img.shields.io/badge/Windows-0078D4?style=flat-square&logo=windows&logoColor=white">
  <img alt="Linux" src="https://img.shields.io/badge/Linux-FCC624?style=flat-square&logo=linux&logoColor=black">
</p>

Works with Claude Code, Gemini CLI, Aider, and any MCP-compatible agent.

---

## Install

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

Requires a local embedding model. [Ollama](https://ollama.com) is the easiest:

```bash
ollama pull nomic-embed-text && ollama serve
```

Prefer a GUI? [LM Studio](https://lmstudio.ai) works too — load any embedding model (e.g. `nomic-embed-text-v1.5`) and start its server (defaults to port 1234).

Restart your agent. Done.

---

## What happens next

You're at a coffee shop on your MacBook, asking Claude to debug a deployment issue. It remembers the architecture decisions you made last week, the server configs you stored yesterday, and the troubleshooting steps that worked last time — all from local SQLite, no internet required.

Later, you're at your Windows desktop at home with Gemini CLI, and it picks up exactly where you left off. Same memories, same context, same knowledge graph. You didn't copy files, didn't export anything, didn't push to someone else's cloud. Your PostgreSQL sync handled everything in the background the moment your laptop hit the local network.

---

## Why this exists

Most AI agents don't persist state between sessions. You re-paste context, re-explain architecture, re-correct mistakes. When facts change, the agent has no mechanism to update what it "knows."

M3 Memory gives agents a structured, persistent memory layer that handles this.

---

## What it does

**Persistent memory** — facts, decisions, preferences survive across sessions. Stored in local SQLite.

**Hybrid retrieval** — FTS5 keyword matching + semantic vector similarity + MMR diversity re-ranking. Scored and explainable.

**Contradiction handling** — conflicting facts are automatically superseded. Bitemporal versioning preserves the full history.

**Knowledge graph** — related memories linked automatically on write. Eight relationship types, 3-hop traversal.

**Local and private** — embeddings generated locally. No cloud calls. No API costs. Works offline.

**Cross-device sync** — optional bi-directional delta sync across SQLite, PostgreSQL, and ChromaDB. Same memory on every machine.

---

## Who this is for

| Good fit | Not the right tool |
|---|---|
| You use Claude Code, Gemini CLI, Aider, or any MCP agent | You need LangChain/CrewAI pipeline memory — see [Mem0](https://mem0.ai) |
| You're coordinating multiple agents on a shared local store | You need a hosted agent runtime with managed scaling — see [Letta](https://letta.ai) |
| You want memory that persists across sessions and devices | You only need in-session chat context |

---

## Why trust this

| | |
|---|---|
| **44 MCP tools** | Memory, search, GDPR — plus agent registry, handoffs, notifications, and tasks for multi-agent orchestration |
| **193 end-to-end tests** | Covering write, search, contradiction, sync, GDPR, maintenance, and orchestration paths |
| **Explainable retrieval** | `memory_suggest` returns vector, BM25, and MMR scores per result |
| **SQLite core** | No external database required. Single-file, portable, inspectable |
| **GDPR compliance** | `gdpr_forget` (Article 17) and `gdpr_export` (Article 20) as built-in tools |
| **Self-maintaining** | Automatic decay, dedup, orphan pruning, retention enforcement |
| **Apache 2.0 licensed** | Free. No SaaS tier, no usage limits, no lock-in |

---

## Core tools

Most sessions use three tools. The rest is there when you need it.

| Tool | Purpose |
|------|---------|
| `memory_write` | Store a fact, decision, preference, config, or observation |
| `memory_search` | Retrieve relevant memories (hybrid search) |
| `memory_update` | Refine existing knowledge |
| `memory_suggest` | Search with full score breakdown |
| `memory_get` | Fetch a specific memory by ID |

All 25 tools are documented in [AGENT_INSTRUCTIONS.md](./AGENT_INSTRUCTIONS.md).

---

## For AI agents

M3 Memory exposes 25 MCP tools for storing, searching, updating, and linking knowledge. Any MCP-compatible agent can use them automatically.

To teach your agent best practices (search before answering, write aggressively, update instead of duplicating), drop the compact rules file into your project:

```
examples/AGENT_RULES.md
```

Full tool reference with all parameters and behaviors: [AGENT_INSTRUCTIONS.md](./AGENT_INSTRUCTIONS.md)

---

## Let your agent install it

Already inside Claude Code or Gemini CLI? Paste one of these prompts:

**Claude Code:**
```
Install m3-memory for persistent memory. Run: pip install m3-memory
Then add {"mcpServers":{"memory":{"command":"mcp-memory"}}} to my
~/.claude/settings.json under "mcpServers". Make sure Ollama is running
with nomic-embed-text. Then use /mcp to verify the memory server loaded.
```

**Gemini CLI:**
```
Install m3-memory for persistent memory. Run: pip install m3-memory
Then add {"mcpServers":{"memory":{"command":"mcp-memory"}}} to my
~/.gemini/settings.json under "mcpServers". Make sure Ollama is running
with nomic-embed-text.
```

After install, test it:
```
Write a memory: "M3 Memory installed successfully on [today's date]"
Then search for: "M3 install"
```

---

## See it in action

### Contradiction detection
<p align="center">
  <img src="docs/demo_contradiction.svg" alt="Demo: contradiction detection and automatic resolution" width="100%">
</p>

### Hybrid search with scores
<p align="center">
  <img src="docs/demo_search.svg" alt="Demo: hybrid search with score breakdown" width="100%">
</p>

### Cross-device, cross-platform sync
<p align="center">
  <img src="docs/demo_sync.svg" alt="Demo: cross-device, cross-platform memory sync" width="100%">
</p>

---

## Learn more

- **Get running** → [QUICKSTART.md](./QUICKSTART.md)
- **Understand features** → [CORE_FEATURES.md](./CORE_FEATURES.md)
- **System design** → [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)
- **Implementation details** → [TECHNICAL_DETAILS.md](./TECHNICAL_DETAILS.md)
- **Agent rules + all 25 tools** → [AGENT_INSTRUCTIONS.md](./AGENT_INSTRUCTIONS.md)
- **M3 vs alternatives** → [COMPARISON.md](./COMPARISON.md)
- **Configuration** → [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md)
- **Roadmap** → [ROADMAP.md](./ROADMAP.md)

---

## Community

[![Discord](https://img.shields.io/badge/Discord-M3_Memory-5865F2?logo=discord&logoColor=white&style=flat-square)](https://discord.gg/ZcJ3EGC99B)
&nbsp;
[![GitHub Issues](https://img.shields.io/badge/GitHub-Issues-181717?logo=github&style=flat-square)](https://github.com/skynetcmd/m3-memory/issues)
&nbsp;
[Contributing](./CONTRIBUTING.md) · [Good first issues](./GOOD_FIRST_ISSUES.md)

---

[![Star History](https://api.star-history.com/svg?repos=skynetcmd/m3-memory&type=Date)](https://star-history.com/#skynetcmd/m3-memory&Date)

<!-- mcp-name: io.github.skynetcmd/m3-memory -->
