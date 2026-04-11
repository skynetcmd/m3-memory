# M3 Memory: Architecture

> High-level overview. For schema details, search internals, sync protocol, and security, see [TECHNICAL_DETAILS.md](../TECHNICAL_DETAILS.md).

## Overview

M3 Memory is a local-first persistent memory system for MCP agents. It provides a semantic memory layer with hybrid search, encrypted credential management, and optional cross-device sync — all running on your hardware.

## Core Components

### 1. MCP Memory Bridge (`bin/memory_bridge.py`)

The memory bridge exposes 25 MCP tools for writing, searching, linking, maintaining, and governing agent memory. It is the only bridge required to use M3 Memory.

### 2. Storage

- **SQLite** (`memory/agent_memory.db`) — primary low-latency store. All reads and writes hit local SQLite first. WAL mode enabled for concurrent access.
- **ChromaDB** *(optional)* — distributed vector search across LAN machines. Falls back to a local `chroma_mirror` table during outages.
- **PostgreSQL** *(optional)* — bi-directional delta sync for cross-device memory sharing. No hardcoded credentials; uses environment variables or OS keyring.

### 3. Search Pipeline

Three-stage hybrid retrieval:

1. **FTS5 keyword matching** — BM25-ranked full-text search with query sanitization
2. **Vector similarity** — cosine similarity against locally-generated embeddings
3. **MMR diversity re-ranking** — prevents near-duplicate results in top-k

Score formula: `0.7 × vector + 0.3 × BM25`. Falls back to pure semantic search when FTS returns no results.

### 4. Intelligence

- **Contradiction detection** — automatic on write. Conflicting facts are superseded with full history preserved.
- **Auto-linking** — related memories connected via knowledge graph (cosine > 0.7).
- **LLM features** — auto-classification, conversation summarization, and memory consolidation via any local OpenAI-compatible server.

### 5. Security (`bin/auth_utils.py`)

- **Credential resolution**: environment variables → OS keyring → encrypted vault (AES-256, PBKDF2, 600K iterations)
- **Content integrity**: SHA-256 hash on every write, verified on demand
- **Input safety**: rejects XSS, SQL injection, code injection, and prompt injection at the write boundary

## Data Flow

```
Agent (Claude Code / Gemini CLI / Aider)
    ↕ MCP protocol
Memory Bridge (25 tools)
    ↕
SQLite (local, primary)
    ↕ optional delta sync
PostgreSQL (cross-device)    ChromaDB (federated vector search)
```

1. Agent calls an MCP tool (e.g., `memory_write`, `memory_search`)
2. Memory bridge processes the request against local SQLite
3. On write: safety check → embedding → contradiction detection → auto-link → store
4. On search: FTS5 + vector + MMR pipeline → ranked results
5. Optional: delta sync pushes/pulls changes to PostgreSQL and ChromaDB
