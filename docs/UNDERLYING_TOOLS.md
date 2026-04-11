# M3 Memory: Underlying Tools

This document details the core services, frameworks, and engines that power the M3 Memory system.

## Storage & Databases

### SQLite (Local Storage)
- **Role**: Primary low-latency transactional database for local agents.
- **Location**: `memory/agent_memory.db`
- **Features**: WAL (Write-Ahead Logging) mode enabled for concurrency; FTS5 for full-text search.
- **Version**: Built-in Python 3.11+ `sqlite3` (SQLite 3.35.0+ required for UPSERT/RETURNING).

### ChromaDB (Federated Memory) — Optional
- **Role**: Distributed vector database for semantic retrieval across machines.
- **API Version**: `v2` (ChromaDB v0.6.0+).
- **Communication**: REST API over HTTP/HTTPS.
- **Integration**: Handled by `bin/memory_sync.py`.

### PostgreSQL (Data Warehouse) — Optional
- **Role**: Long-term archival and multi-device synchronization.
- **Recommended Version**: v15 or v16.
- **Driver**: `psycopg2-binary` (v2.9.11+).

## Intelligence Engines

### Local LLM (Reasoning)
- **Role**: Complex task orchestration, auto-classification, and consolidation summaries.
- **Deployment**: Any model served via LM Studio, Ollama, or vLLM.
- **Interface**: OpenAI-compatible REST API.
- **Selection**: `bin/llm_failover.py` auto-selects the largest available model across configured endpoints.

### Embedding Models (Vectorization)
- **Role**: Converting text to semantic vectors for similarity search.
- **Recommended**: `nomic-embed-text` via Ollama, or `jina-embeddings-v2-base-en` via LM Studio.
- **Deployment**: Any OpenAI-compatible embedding endpoint.

## Frameworks & Libraries

### Model Context Protocol (MCP)
- **Implementation**: `FastMCP` (v3.2+).
- **Role**: Standardized interface for tool exposure and inter-agent communication.

### HTTP Stack
- **Library**: `httpx` (v0.28+) for asynchronous calls with connection pooling.
- **Server**: `FastAPI` + `Uvicorn` for the MCP bridge server.

### Encryption
- **Library**: `cryptography` (v46.0+).
- **Algorithm**: Fernet (AES-128-CBC) with PBKDF2HMAC salted key derivation (600K iterations).
