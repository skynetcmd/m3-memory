---
tool: bin/memory_core.py
sha1: d3ba61ceefcb
mtime_utc: 2026-04-18T04:42:34.536917+00:00
generated_utc: 2026-04-18T05:16:53.186253+00:00
private: false
---

# bin/memory_core.py

## Purpose

Central SQLite schema manager and semantic search engine for m3-memory. Implements CRUD operations, vector search, contradiction detection, relationship graphs, task workflows, and agent lifecycle management. No CLI surface—invoked as a library by MCP proxies and bridge modules.

## Entry points / Public API

**Memory CRUD & Versioning**
- `memory_get_impl(id)` (line 1593) — Fetch full MemoryItem by UUID
- `memory_verify_impl(memory_id)` (line 1604) — Verify content integrity via hash
- `memory_history_impl(memory_id, limit=20)` (line 732) — Audit trail (create/update/delete/supersede events)
- `memory_cost_report_impl()` (line 1618) — Token usage & operation counts
- `memory_delete_impl(id, hard=False)` (line 1677) — Soft-delete (tombstone) or hard-delete

**Linking & Graphs**
- `memory_link_impl(from_id, to_id, relationship_type="related")` (line 1716) — Create directional edge
- `memory_graph_impl(memory_id, depth=1)` (line 1727) — Fetch N-hop neighborhood
- `VALID_RELATIONSHIP_TYPES` (line 1694) — {"related", "supports", "contradicts", "extends", "supersedes", "references", "message", "consolidates", "handoff", "precedes", "follows"}

**Handoff & Inbox**
- `memory_handoff_impl(from_agent, to_agent, task, context_ids, note)` (line 1780) — Assign task + notify
- `memory_inbox_impl(agent_id, unread_only=True, limit=20)` (line 1830) — List handoffs to agent
- `memory_inbox_ack_impl(memory_id)` (line 1865) — Mark handoff as read

**Refresh Queue**
- `memory_refresh_queue_impl(agent_id="", limit=50, include_future=False)` (line 1927) — Overdue memories needing review

**Agent Registry & Lifecycle**
- `agent_register_impl(agent_id, role, capabilities, metadata)` (line 2233) — Create/upsert agent
- `agent_get_impl(agent_id)` (line 2302) — Fetch agent record
- `agent_list_impl(status="", role="")` (line 2273) — Filter agents by status/role
- `agent_heartbeat_impl(agent_id)` (line 2257) — Update last_seen + set status=active
- `agent_offline_impl(agent_id)` (line 2328) — Mark agent offline
- `VALID_AGENT_STATUSES` (line 2211) — {"active", "idle", "offline"}

**Notifications**
- `notify_impl(agent_id, kind, payload={})` (line 2344) — Send lightweight wake signal
- `notifications_poll_impl(agent_id, unread_only=True, limit=20)` (line 2361) — Fetch queued notifications
- `notifications_ack_impl(notification_id)` (line 2385) — Mark notification read
- `notifications_ack_all_impl(agent_id)` (line 2401) — Bulk ack all unread

**Task Management**
- `task_create_impl(title, created_by, description="", owner_agent="", parent_task_id="", metadata={})` (line 2416) — Create in pending state
- `task_get_impl(task_id, include_deleted=False)` (line 2546) — Fetch full task record
- `task_list_impl(owner_agent="", state="", parent_task_id="", limit=20)` (line 2617) — Filter tasks
- `task_assign_impl(task_id, owner_agent)` (line 2435) — Reassign + notify new owner
- `task_update_impl(task_id, state="", description="", metadata={}, actor="")` (line 2469) — Partial update; validates transitions
- `task_set_result_impl(task_id, result_memory_id)` (line 2530) — Link result memory without changing state
- `task_delete_impl(task_id, hard=False, actor="")` (line 2574) — Soft-delete (tombstone) or hard-delete
- `task_tree_impl(root_task_id, max_depth=10)` (line 2651) — Render recursive subtree
- `VALID_TASK_STATES` (line 2209) — State transition graph keys

**Internal Helpers**
- `_db()` (line 697) — Context manager for SQLite connection pooling
- `_conn()` (line 708) — Manual connection handle (careful with nesting)
- `_record_history(memory_id, event, prev_value, new_value, field, actor_id)` (line 713) — Audit log insertion
- `_content_hash(content)` (line 748) — SHA256 for integrity checks
- `_get_embed_client()` (line 755) — Async httpx client for ChromaDB

## CLI flags / arguments

_(no CLI surface — invoked as a library/module.)_

## Bulk ingest function signature (Phase 1)

**`memory_write_bulk_impl(items, *, enrich=None, check_contradictions=None, emit_conversation=None, variant=None)`** (line 957)

Keyword-only parameters controlling bulk ingestion behavior:

| Parameter | Default | Default behavior | Type | Overridable |
|---|---|---|---|---|
| `enrich` | `None` | Inherit env gates M3_INGEST_AUTO_TITLE / M3_INGEST_AUTO_ENTITIES | bool \| None | True forces on; False disables |
| `check_contradictions` | `None` | OFF by default in bulk (perf optimization) — differs from single-path which always checks | bool \| None | True enables with Semaphore(8) bounded concurrency; False disables |
| `emit_conversation` | `None` | ON if item has conversation_id and type=="message" (mirrors single-path behavior) | bool \| None | True forces on; False disables |
| `variant` | `None` | Each item carries its own variant if set; otherwise no variant tag | str \| None | When set, becomes default for all items lacking `variant` field (propagated to enrichers for A/B tracking) |

**Note:** `auto_classify=True` per-item is now honored in bulk (line 1014) and routes to LLM type inference.

## Environment variables read

**Database & Embedding**
- `CHROMA_BASE_URL` — ChromaDB HTTP endpoint (default: local)
- `EMBED_MODEL` — Embedding model name (default: "qwen3-embedding")
- `EMBED_DIM` — Embedding dimension (default: 1024)
- `ORIGIN_DEVICE` — Device hostname for sync tracking (default: platform.node())

**Search & Dedup**
- `DEDUP_LIMIT` — Max rows for dedup check (default: 1000)
- `DEDUP_THRESHOLD` — Cosine similarity cutoff for near-duplicates (default: 0.92)
- `CONTRADICTION_THRESHOLD` — Cosine similarity cutoff for contradiction detection (default: 0.85)
- `SEARCH_ROW_CAP` — Max rows from initial search before scoring (default: 500)

**LLM & Timeouts**
- `LLM_TIMEOUT` — HTTP timeout for LLM calls in seconds (default: 120.0)

**Search Ranking Tuning**
- `M3_SPEAKER_IN_TITLE` — Prepend speaker metadata to title for ranking (default: "1" = enabled)
- `M3_SHORT_TURN_THRESHOLD` — Token cutoff for short-turn content (default: 20)
- `M3_TITLE_MATCH_BOOST` — Score bonus for title overlap (default: 0.05)
- `M3_IMPORTANCE_WEIGHT` — Weight for importance signal in ranker (default: 0.05)
- `M3_QUERY_TYPE_ROUTING` — Route query to type-filtered search (default: "0" = disabled)

**Ingest Optimizations (Phase 1)**
- `M3_INGEST_WINDOW_CHUNKS` — Emit sliding-window gists (default: "0" = off)
- `M3_INGEST_GIST_ROWS` — Emit conversation gist rows (default: "0" = off)
- `M3_INGEST_EVENT_ROWS` — Emit sentence-level event rows (default: "0" = off)
- `M3_INGEST_WINDOW_SIZE` — Window size in turns (default: 3)
- `M3_INGEST_GIST_MIN_TURNS` — Min conversation length for gisting (default: 8)
- `M3_INGEST_GIST_STRIDE` — Stride between gist window boundaries (default: 8)

**Constants & Validation**
- `VALID_CHANGE_AGENTS` — {"claude", "gemini", "aider", "openclaw", "deepseek", "grok", "manual", "system", "unknown", "legacy"}
- `VALID_SCOPES` (line 2010) — {"user", "session", "agent", "org"}

## Calls INTO this repo (intra-repo imports)

- `embedding_utils` — `pack()`, `unpack()`, `batch_cosine()`, `infer_change_agent()`
- `llm_failover` — `get_best_embed()`, `get_best_llm()`, `get_smallest_llm()`
- `m3_sdk` — `M3Context()` for config/secrets/SQLite pool

## Calls OUT (external side-channels)

**sqlite3**
- `sqlite3.connect()` via `_db()` context manager and `ctx.get_sqlite_conn()` pool
- Writes to `agent_memory.db` and `agent_memory_archive.db`

**httpx (async)**
- `httpx.AsyncClient` for ChromaDB HTTP requests (line 755, 790, 886)
- POST to `/add`, `/query`, `/delete` endpoints
- Configurable timeout via `EMBED_TIMEOUT_READ` (default 30s)

**subprocess**
- `subprocess.run([sys.executable, migration_script])` (line 674) — Run SQL migration scripts

**filesystem**
- Reads `agent_memory.db`, `agent_memory_archive.db` (default: `memory/` subdir)
- Reads/writes via SQLite only; no direct file I/O

**platform**
- `platform.node()` — Hostname for `ORIGIN_DEVICE` default

## File dependencies (repo paths referenced)

- `memory/agent_memory.db` — Primary SQLite store (memory_items, memory_relationships, agents, tasks, notifications, etc.)
- `memory/agent_memory_archive.db` — Archived memories (soft-deleted items)

## Re-validation

If `sha1` differs from current file's sha1, the inventory is stale. Re-read the tool, confirm functions/constants/env vars still match, and regenerate via:

```bash
python bin/gen_tool_inventory.py
```
