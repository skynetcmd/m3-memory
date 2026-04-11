# M3 Memory: Underlying Tools

This document details the core services, frameworks, and engines that power the M3 Memory system.

## 💾 Storage & Databases

### 1. SQLite (Local Storage)
- **Role**: Primary low-latency transactional database for local agents.
- **Location**: `memory/agent_memory.db`
- **Features**: WAL (Write-Ahead Logging) mode enabled for concurrency; FTS5 for full-text search.
- **Version**: Built-in Python 3.11+ `sqlite3` (SQLite 3.35.0+ required for UPSERT/RETURNING).

### 2. ChromaDB (Federated Memory)
- **Role**: Distributed vector database for semantic retrieval across the LAN.
- **API Version**: `v2` (Introduced in ChromaDB v0.6.0).
- **Communication**: REST API over HTTP/HTTPS.
- **Integration**: Handled by `bin/memory_sync.py`.

### 3. PostgreSQL (Data Warehouse)
- **Role**: Long-term archival and multi-device synchronization.
- **Recommended Version**: v15 or v16.
- **Location**: Hosted on Proxmox VM (10.x.x.x).
- **Driver**: `psycopg2-binary` (v2.9.11+).

## 🧠 Intelligence Engines

### 1. DeepSeek-R1 (Primary Reasoning)
- **Role**: Complex task orchestration and deep reasoning.
- **Deployment**: Local MLX distillation (70B) served via LM Studio.
- **Interface**: OpenAI-compatible REST API.

### 2. Jina Embeddings / Nomic Embed (Vectorization)
- **Role**: Converting text to semantic vectors.
- **Models**: `jina-embeddings-v2-base-en` or `nomic-embed-text-v1.5`.
- **Deployment**: Loaded in LM Studio.

## 🛠️ Frameworks & Libraries

### 1. Model Context Protocol (MCP)
- **Implementation**: `FastMCP` (v3.2+).
- **Role**: Standardized interface for tool exposure and inter-agent communication.

### 2. HTTP Stack
- **Library**: `httpx` (v0.28+) for asynchronous calls with connection pooling.
- **Server**: `FastAPI` + `Uvicorn` for the MCP Proxy.

### 3. Encryption
- **Library**: `cryptography` (v46.0+).
- **Algorithm**: Fernet (AES-128-CBC) with PBKDF2HMAC salted key derivation.
