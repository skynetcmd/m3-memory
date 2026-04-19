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
- **Tool catalog**: `bin/mcp_tool_catalog.py` is the single source of truth for all MCP tool definitions via the `ToolSpec` dataclass. 55 tools total (46 default-allowed + 9 destructive opt-in).
- **Identity injection**: Tools marked `inject_agent_id=True` (`memory_write`, `agent_heartbeat`, `agent_offline`, `memory_inbox`, `notifications_poll`, `notifications_ack_all`) cannot be spoofed — the dispatcher overrides client-claimed `agent_id` with the authenticated identity.

### MCP Proxy (`bin/mcp_proxy.py`)
- **Role**: Bridges OpenAI-compatible chat completion clients (Aider, OpenClaw, custom HTTP clients) to the MCP tool catalog. Listens on `localhost:9000`.
- **Sources**: Composes its tool list from three places — `PROTOCOL_TOOLS` (5 inline), `DEBUG_TOOLS` (6 inline), and `bin/mcp_tool_catalog.py` (46 default / 55 with destructive enabled).
- **Agent identity**: Reads `X-Agent-Id` HTTP header and propagates it to catalog dispatch, enforcing `inject_agent_id` semantics so client requests cannot bypass identity.
- **Destructive gating**: Set `MCP_PROXY_ALLOW_DESTRUCTIVE=1` to expose the 9 destructive tools (`memory_delete`, `chroma_sync`, `memory_maintenance`, `memory_set_retention`, `memory_export`, `memory_import`, `gdpr_export`, `gdpr_forget`, `agent_offline`). Default mode hides them.
- **Health check**: `GET /health` reports per-source counts and the `allow_destructive` flag.

### HTTP Stack
- **Library**: `httpx` (v0.28+) for asynchronous calls with connection pooling.
- **Server**: `FastAPI` + `Uvicorn` for the MCP bridge server and `mcp_proxy`.

### Encryption
- **Library**: `cryptography` (v46.0+).
- **Algorithm**: Fernet (AES-128-CBC) with PBKDF2HMAC salted key derivation (600K iterations).
