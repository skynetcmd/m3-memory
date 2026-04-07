# M3 Max Agentic OS: API Reference

## 🧠 Memory Bridge
Central tools for semantic persistence and retrieval.

### `memory_search`
Search across memory items using semantic similarity or keyword matching.
- **Args**: `query` (str), `k` (int, default 8), `search_mode` (str: hybrid|semantic|keyword)

### `memory_write`
Creates a MemoryItem and optionally embeds it.
- **Args**: `type` (str), `content` (str), `importance` (float), `embed` (bool)

### `chroma_sync`
Bi-directional sync between local SQLite and remote ChromaDB.
- **Args**: `direction` (str: both|push|pull), `max_items` (int)

---

## 🛠️ Custom Tool Bridge
System sensing and environmental integration.

### `check_thermal_load`
Check system thermal/RAM pressure.
- **Returns**: `Nominal` | `Fair` | `Serious` | `Critical`

### `log_activity`
Archive data to the agent log (Protocols #1–#3).
- **Args**: `category` (thought|hardware|decision), `detail_a`, `detail_b`

---

## 🔍 Debug Agent Bridge
Autonomous system analysis.

### `debug_analyze`
Root cause analysis with memory-augmented reasoning.
- **Args**: `error_message` (str), `context` (str)

### `debug_bisect`
Automated git bisect with LLM analysis.
- **Args**: `test_command` (str), `good_commit`, `bad_commit`

---

## 🌐 MCP Proxy
Endpoint: `http://localhost:9000/v1`
Auth: `Authorization: Bearer <MCP_PROXY_KEY>`

### Endpoints
- `GET /v1/models`: List available models.
- `POST /v1/chat/completions`: Standard OpenAI-compatible chat endpoint with tool injection.
