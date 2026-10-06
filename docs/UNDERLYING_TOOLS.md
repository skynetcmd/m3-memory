# m3 Memory: Underlying Tools

This document details the core services, frameworks, and engines that power the m3 Memory system.

## Storage & Databases

### SQLite (Local Storage)
- **Role**: Primary low-latency transactional database for local agents.
- **Location**: `<M3_ENGINE_ROOT>/agent_memory.db` (default `~/.m3/engine/agent_memory.db`).
- **Features**: WAL (Write-Ahead Logging) mode enabled for concurrency; FTS5 for full-text search.
- **Version**: Built-in Python 3.12+ `sqlite3` (SQLite 3.35.0+ required for UPSERT/RETURNING).

### PostgreSQL (Data Warehouse) — Optional
- **Role**: Long-term archival and multi-device synchronization.
- **Recommended Version**: v15 or v16.
- **Driver**: `psycopg2-binary` (v2.9.13+); the `postgres` extra adds `psycopg[binary]` (v3.3.5+).

---

## Intelligence Engines

### Local LLM (Reasoning)
- **Role**: Complex task orchestration, auto-classification, and consolidation summaries.
- **Deployment**: Any model served via LM Studio, Ollama, or vLLM.
- **Interface**: OpenAI-compatible REST API.
- **Selection**: `bin/llm_failover.py` auto-selects the largest available model across configured endpoints.

### Embedding Models (Vectorization)
- **Role**: Converting text to semantic vectors for similarity search.
- **Default**: BGE-M3 (1024-dim), served by the `m3-embed-server` binary (from the m3-core-rs wheel) on `127.0.0.1:8082`. In-process embedding is opt-in (`M3_EMBED_INPROC=1`).
- **Deployment**: The shared embed server is the default; any OpenAI-compatible embedding endpoint can be configured instead.

---

## Frameworks & Libraries

### Model Context Protocol (MCP)
- **Implementation**: `FastMCP` (v3.4.7+).
- **Role**: Standardized interface for tool exposure and inter-agent communication.
- **Tool catalog**: `bin/mcp_tool_catalog.py` is the single source of truth for all MCP tool definitions via the `ToolSpec` dataclass. 100+ tools; those with `default_allowed=False` (17 at present) are the destructive opt-in set.
- **Identity injection**: Tools marked `inject_agent_id=True` (e.g. `memory_write`, `memory_supersede`, `agent_heartbeat`, `agent_offline`, `memory_inbox`, `notifications_poll`, `notifications_ack_all`) cannot be spoofed — the dispatcher overrides client-claimed `agent_id` with the authenticated identity.

### MCP Proxy (`bin/mcp_proxy.py`)
- **Role**: Bridges OpenAI-compatible chat completion clients (Aider, custom HTTP clients) to the MCP tool catalog. Listens on `localhost:9000`. OpenClaw no longer needs it — it is a native MCP client since 2026.3.22.
- **Sources**: Serves only m3's own catalog, `bin/mcp_tool_catalog.py` (default-allowed tools, plus the destructive set when enabled). The former inline `PROTOCOL_TOOLS` / `DEBUG_TOOLS` sets were removed.
- **Agent identity**: Reads `X-Agent-Id` HTTP header and propagates it to catalog dispatch, enforcing `inject_agent_id` semantics so client requests cannot bypass identity.
- **Destructive gating**: Set `MCP_PROXY_ALLOW_DESTRUCTIVE=1` to expose the destructive (`default_allowed=False`) tools: `memory_delete`, `memory_delete_bulk`, `memory_maintenance`, `memory_set_retention`, `memory_export`, `memory_import`, `memory_restore`, `memory_search_multi_db`, `memory_search_routed`, `curate_memory_apply`, `curate_chatlog_apply`, `enrich_pending`, `extract_pending`, `files_corpus_delete`, `gdpr_export`, `gdpr_forget`, `agent_offline`. Default mode hides them.
- **Health check**: `GET /health` reports per-source counts and the `allow_destructive` flag.

### HTTP Stack
- **Library**: `httpx` (v0.28.1+) for asynchronous calls with connection pooling.
- **Server**: `FastAPI` + `Uvicorn` for the MCP bridge server and `mcp_proxy`.

### Encryption
- **Library**: `cryptography` (v50.0.1+).
- **Algorithm**: AES-256-GCM for new vault writes, with PBKDF2-HMAC-SHA256 salted key derivation (600K iterations). Fernet (AES-128-CBC) is used only to decrypt legacy secrets.
