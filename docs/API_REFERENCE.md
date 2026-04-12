# M3 Memory: API Reference

All 44 MCP tools exposed by the memory bridge (`bin/memory_bridge.py`), sourced from `bin/mcp_tool_catalog.py`. For behavioral rules and usage guidance, see [AGENT_INSTRUCTIONS.md](../AGENT_INSTRUCTIONS.md).

---

## Memory Operations

### `memory_write`
Store a fact, decision, preference, config, observation, or any persistent knowledge.
- **Args**: `type` (str, required), `content` (str, required), `title` (str), `importance` (float, 0.0–1.0), `agent_id` (str), `user_id` (str), `scope` (str), `valid_from` (ISO 8601), `valid_to` (ISO 8601), `embed` (bool, default true), `metadata` (JSON string)

### `memory_search`
Retrieve relevant memories using hybrid search (FTS5 + vector + MMR).
- **Args**: `query` (str, required), `k` (int, default 8), `type_filter` (str), `agent_filter` (str), `user_id` (str), `scope` (str), `as_of` (ISO 8601), `search_mode` (str: hybrid|semantic)

### `memory_suggest`
Same as `memory_search` but returns full score breakdowns (vector, BM25, MMR penalty) per result.
- **Args**: Same as `memory_search`

### `memory_get`
Retrieve a specific memory by UUID.
- **Args**: `id` (str, required)

### `memory_update`
Update content, title, metadata, or importance of an existing memory. Records audit trail.
- **Args**: `id` (str, required), plus any fields to update

### `memory_delete`
Soft-delete (default, recoverable) or hard-delete a memory.
- **Args**: `id` (str, required), `hard` (bool, default false — hard delete cascades to embeddings, relationships, history)

### `memory_verify`
Check content integrity by re-computing SHA-256 hash and comparing to stored value.
- **Args**: `id` (str, required)

---

## Knowledge Graph

### `memory_link`
Create a directed relationship between two memories.
- **Args**: `from_id` (str, required), `to_id` (str, required), `type` (str, required — one of: `related`, `supports`, `contradicts`, `extends`, `supersedes`, `references`, `consolidates`)

### `memory_graph`
Explore connected memories up to 3 hops from a starting memory.
- **Args**: `id` (str, required), `depth` (int, default 1, max 3)

### `memory_history`
View the full audit trail for a memory — every create, update, delete, and supersede event.
- **Args**: `id` (str, required)

---

## Conversations

### `conversation_start`
Begin a new conversation thread.
- **Args**: `title` (str, required), `agent_id` (str), `user_id` (str)

### `conversation_append`
Add a message to an existing conversation.
- **Args**: `conversation_id` (str, required), `role` (str, required), `content` (str, required)

### `conversation_search`
Search across all conversation messages.
- **Args**: `query` (str, required)

### `conversation_summarize`
Generate an LLM summary when a conversation exceeds a message threshold.
- **Args**: `conversation_id` (str, required), `threshold` (int, default 20)

---

## Lifecycle & Maintenance

### `memory_maintenance`
Run decay, expiry purge, orphan pruning, auto-archival, and retention enforcement.
- **Args**: None

### `memory_dedup`
Find and optionally remove near-duplicate memories.
- **Args**: `threshold` (float, default 0.92), `dry_run` (bool, default true)

### `memory_consolidate`
Merge groups of old memories into LLM-generated summaries.
- **Args**: `type_filter` (str), `agent_filter` (str), `threshold` (int)

### `memory_set_retention`
Set per-agent retention limits, enforced automatically by `memory_maintenance`.
- **Args**: `agent_id` (str, required), `max_memories` (int), `ttl_days` (int)

### `memory_feedback`
Mark a memory as `useful` (boosts importance +0.1) or `wrong` (soft-deletes).
- **Args**: `memory_id` (str, required), `feedback` (str: useful|wrong)

---

## Data Governance

### `gdpr_export`
Export all memories for a user as portable JSON (Article 20).
- **Args**: `user_id` (str, required)

### `gdpr_forget`
Hard-delete all data for a user — memories, embeddings, relationships, history, sync queue (Article 17).
- **Args**: `user_id` (str, required)

### `memory_export`
Export memories as portable JSON for backup or migration.
- **Args**: `agent_filter` (str), `type_filter` (str), `since` (ISO 8601)

### `memory_import`
Import from a previous export. UPSERT semantics — safe to re-run.
- **Args**: `data` (JSON string, required)

---

## Operations

### `memory_cost_report`
Check session operation counts (embed calls, tokens, searches, writes).
- **Args**: None

### `chroma_sync`
Manual sync with ChromaDB.
- **Args**: `direction` (str: push|pull|both)
