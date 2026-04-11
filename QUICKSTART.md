# 🧠 M3 Memory

**Persistent, local memory for AI agents — no APIs, no cloud, no lock-in.**

Give your agent the ability to **remember across sessions**, **connect ideas**, and **retrieve context intelligently** — all running locally.

---

## ⚡ 30-second quickstart

```bash
pip install m3-memory
```

```json
{
  "mcpServers": {
    "memory": {
      "command": "mcp-memory"
    }
  }
}
```

Restart your agent runtime (Claude Code, Gemini CLI, Aider), and your agent now has **persistent memory**.

---

## ❓ Why this exists

Most AI agents are **stateless**.

That means:

- They forget everything between sessions
- They lose important user context
- They repeat work and hallucinate inconsistently

**M3 Memory fixes that** by giving agents a structured, persistent memory layer.

---

## ✨ What it feels like

**Without memory:**

> User: "What did I say about my startup idea last week?"
> Agent: "I don't have that context."

**With M3 Memory:**

> Agent retrieves prior conversations →
> "You were exploring a B2B SaaS idea focused on developer tooling…"

---

## 🧩 Mental model

```
User ↔ Agent ↔ M3 Memory
                 ├── semantic search
                 ├── keyword search
                 ├── memory linking (graph)
                 └── local persistent storage
```

> **SQLite + vector search + knowledge graph — for agents**

---

## 🧠 Core capabilities

**Memory**
- Store structured information over time
- Update and refine knowledge
- Link related memories together

**Retrieval**
- Hybrid search (semantic + keyword + re-ranking)
- Context-aware recall
- Fast local queries

**Privacy**
- 100% local-first
- No API keys
- No external services

---

## 🔑 Start with these 5 tools

You don't need all 25. Use these first:

| Tool | What it does |
|------|-------------|
| `memory_write` | Store important info |
| `memory_search` | Retrieve context |
| `memory_link` | Connect related ideas |
| `memory_update` | Refine existing knowledge |
| `memory_delete` | Clean up |

Everything else is advanced.

---

## 🤖 How agents should use this

A simple pattern:

```
If user provides durable info  → memory_write
If question may depend on past → memory_search
If new info updates old        → memory_update
```

This turns your agent from **reactive** → **context-aware**.

---

## 🧪 Recipes

**🧑 Personal assistant**
- Remember preferences, goals, history
- Build long-term user context

**💻 Coding agent**
- Track decisions across sessions
- Recall architecture and constraints

**📚 Research agent**
- Store findings over time
- Connect related concepts

**🧾 Lightweight CRM**
- Track users, interactions, notes

---

## ⚖️ When NOT to use this

M3 Memory may not be the right fit if:

- You need fully managed cloud infrastructure
- You're building LangChain/CrewAI pipelines (consider [Mem0](https://mem0.ai))
- You want a full stateful agent runtime (consider [Letta](https://letta.ai))

---

## 🏗️ How it works

- Memories stored locally in SQLite
- Embeddings via your local LLM (Ollama, LM Studio, vLLM)
- FTS5 keyword index improves precision
- MMR re-ranking improves diversity
- Knowledge graph links connect related entries
- Bitemporal history tracks what was true when

---

## ⚡ Performance

- Local queries — sub-millisecond on SQLite
- No network calls
- Scales with your hardware

---

## 🆚 Comparison

| Feature | M3 Memory | Vector DB only | Basic RAG |
|---------|:---------:|:--------------:|:---------:|
| Persistent across sessions | ✅ | ❌ | ❌ |
| Automatic contradiction detection | ✅ | ❌ | ❌ |
| Structured linking (graph) | ✅ | ❌ | ❌ |
| Local-first | ✅ | ⚠️ | ⚠️ |
| Native MCP tools | ✅ | ❌ | ❌ |
| GDPR forget + export | ✅ | ❌ | ❌ |

---

## 🧠 For AI agents (machine-readable summary)

```yaml
name: m3-memory
purpose: persistent memory for AI agents
capabilities:
  - write
  - search
  - update
  - link
  - contradiction-detection
  - gdpr-forget
  - gdpr-export
storage: local SQLite + optional PostgreSQL + ChromaDB
retrieval: hybrid (FTS5 + semantic + MMR rerank)
mcp_tools: 25
local_only: true
```

---

## 🚀 Philosophy

AI agents shouldn't start from zero every time.

Memory is what makes intelligence **compound over time**.

---

## 📦 Full documentation

- [README.md](./README.md) — full technical + marketing overview
- [ARCHITECTURE.md](./ARCHITECTURE.md) — internals, MCP tools, protocols
- [COMPARISON.md](./COMPARISON.md) — M3 vs Mem0 vs Letta vs LangChain Memory
- [ROADMAP.md](./ROADMAP.md) — what's coming next

---

## 💡 Final thought

The difference between a *demo agent* and a *useful agent* is simple:

> **memory**
