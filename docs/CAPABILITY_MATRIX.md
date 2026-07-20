# M3 Capability Matrix

> **Generated** by `bin/gen_capability_matrix.py` from `docs/tools/MCP_CATALOG.json` — do not edit by hand; re-run after any tool-catalog change. This is the single scannable index of *what M3 can do* and *which tool does it*, for humans, search engines, and AI agents.

**110 tools across 9 capability groups.** A ⚠️ marks a destructive tool (mutates or deletes).

> **Beyond MCP tools:** M3 also ships a **storage backend** choice (SQLite default; PostgreSQL as a first-class primary via `M3_DB_BACKEND=postgres`) and native **framework adapters** — LangChain/LangGraph, CrewAI, and PydanticAI. These are deployment/framework facts, not MCP tools, so they don't appear in the table below. See [CORE_FEATURES](CORE_FEATURES.md) and [COMPARISON](COMPARISON.md).

## Capability groups

- [🧠 Memory](#memory) — Write, retrieve, version, and reconcile long-term agent memory. (36 tools)
- [💬 Chat Log](#chat-log) — Capture verbatim conversation turns before compaction; audit and replay. (10 tools)
- [📁 Files Memory](#files-memory) — Index, search, and recall project files as memory. (26 tools)
- [🕸️ Entity Graph](#entity-graph) — Extract and query entities and their relationships across sessions. (3 tools)
- [🗂️ Conversations](#conversations) — Group and inspect turns by conversation / team session. (4 tools)
- [👥 Agents](#agents) — Register agents, hand off tasks, and route multi-agent work. (6 tools)
- [✅ Tasks](#tasks) — Track and coordinate agent tasks and their state. (8 tools)
- [🩺 Diagnostics](#diagnostics) — Health, cost, and integrity checks for the memory store. (3 tools)
- [⚙️ Admin & Sync](#admin--sync) — Maintenance, cross-store sync, import/export, and lifecycle ops. (14 tools)

## 🧠 Memory

_Write, retrieve, version, and reconcile long-term agent memory._

| Tool | Description | Mutates? |
|---|---|---|
| `curate_memory_apply` | Deterministically apply a memory.db curator plan in ONE call. | ⚠️ yes |
| `memory_consolidate` | Consolidate old memories of the same type into summaries using the local LLM. | read-only |
| `memory_cost_report` | Returns current session operation counts and estimated token usage for memory operations. | read-only |
| `memory_count_entities` | Count distinct entities mentioned in a single conversation. | read-only |
| `memory_count_mentions` | Per-entity mention frequency within a single conversation, sorted DESC by count. | read-only |
| `memory_dedup` | Find (and optionally soft-delete) near-duplicate memory items by cosine similarity over embeddings. | read-only |
| `memory_delete` | Deletes a MemoryItem (soft or hard). | ⚠️ yes |
| `memory_delete_bulk` | Deletes a list of MemoryItems (soft or hard) in one transaction per chunk. | ⚠️ yes |
| `memory_export` | Export memories as portable JSON. | ⚠️ yes |
| `memory_feedback` | Provide feedback on a memory item to improve quality. | read-only |
| `memory_get` | Retrieves a full MemoryItem; accepts full UUID or 8-char prefix; ambiguous prefixes return an error. | read-only |
| `memory_graph` | Returns the local graph neighborhood of a memory item (connected memories up to N hops, max 3). | read-only |
| `memory_handoff` | Hand off a task from one agent to another. | read-only |
| `memory_history` | Returns the change history (audit trail) for a memory item. | read-only |
| `memory_import` | Import memories from a JSON export. | ⚠️ yes |
| `memory_inbox` | List handoff messages addressed to agent_id, newest first. | read-only |
| `memory_inbox_ack` | Mark a handoff memory as read (sets read_at = now). | read-only |
| `memory_lifecycle_summary` | Windowed summary of lifecycle & contradiction activity over the last `window_days` days: counts of… | read-only |
| `memory_link` | Creates a directional link between two memory items. | read-only |
| `memory_link_bulk` | Create many memory_relationships rows in one transaction per chunk. | read-only |
| `memory_maintenance` | Runs maintenance tasks on the memory store. | ⚠️ yes |
| `memory_pin` | Pin a memory to exempt it from decay, expiry, and retention purges. | read-only |
| `memory_refresh_queue` | List memories whose refresh_on timestamp has arrived and need review. | read-only |
| `memory_search` | Search across memory items using semantic similarity or keyword matching. | read-only |
| `memory_search_multi_db` | Search across multiple SQLite databases (e.g. | ⚠️ yes |
| `memory_search_routed` | Temporal-aware routed retrieval. | ⚠️ yes |
| `memory_search_scored` | Structured hybrid FTS5+vector+MMR search. | read-only |
| `memory_set_retention` | Set or update per-agent memory retention policy. | ⚠️ yes |
| `memory_suggest` | Preview which memories would be retrieved for a query, with score breakdowns explaining why each wa… | read-only |
| `memory_supersede` | Explicitly supersede an existing memory with a new one. | read-only |
| `memory_unpin` | Unpin a memory, restoring normal decay/expiry/retention handling. | read-only |
| `memory_update` | Updates a MemoryItem by ID. | read-only |
| `memory_update_bulk` | Apply many metadata-only updates in one transaction per chunk. | read-only |
| `memory_verify` | Verify content integrity by comparing stored hash with computed hash. | read-only |
| `memory_write` | Creates a MemoryItem and optionally embeds it for semantic search. | read-only |
| `memory_write_from_file` | Write a memory whose content is read from a file on disk. | read-only |

## 💬 Chat Log

_Capture verbatim conversation turns before compaction; audit and replay._

| Tool | Description | Mutates? |
|---|---|---|
| `chatlog_cost_report` | Aggregate tokens and cost_usd across chat_log rows. | read-only |
| `chatlog_list_conversations` | List distinct conversation_ids with turn counts and timespans. | read-only |
| `chatlog_promote` | Promote chat_log rows into the main memory DB under a new type (default 'conversation'). | read-only |
| `chatlog_rescrub` | Re-apply redaction to existing chat_log rows. | read-only |
| `chatlog_search` | Search chat_log rows. | read-only |
| `chatlog_set_redaction` | Flip redaction on/off and update patterns. | read-only |
| `chatlog_status` | One-call health summary of the chat log subsystem: mode, DB paths, row counts, queue depth, spill f… | read-only |
| `chatlog_write` | Append one chat turn to the chat log DB. | read-only |
| `chatlog_write_bulk` | Bulk-append N chat turns. | read-only |
| `curate_chatlog_apply` | Deterministically apply a chatlog.db curator plan in ONE call. | ⚠️ yes |

## 📁 Files Memory

_Index, search, and recall project files as memory._

| Tool | Description | Mutates? |
|---|---|---|
| `files_corpus_create` | Register a new corpus with optional default overrides. | read-only |
| `files_corpus_delete` | Delete a corpus's settings row. | ⚠️ yes |
| `files_corpus_get` | Fetch a single corpus's settings + counts. | read-only |
| `files_corpus_list` | Enumerate corpora with row counts. | read-only |
| `files_corpus_set` | Update settings for an existing corpus. | read-only |
| `files_dedup` | Scan leaf embeddings for near-duplicates above cosine threshold. | read-only |
| `files_dedup_list` | List near-duplicate candidate pairs with text snippets and paths. | read-only |
| `files_dedup_review` | Record a review decision on a near-duplicate candidate: 'kept' \| 'merged' \| 'ignored'. | read-only |
| `files_entity_coalesce` | Detect provisional-entity coalescing candidates (quarantine noise + flag near-duplicate entities). | read-only |
| `files_entity_coalesce_apply` | Apply the reversible same_as/cluster overlay. | read-only |
| `files_entity_coalesce_list` | List entity-coalescing candidate pairs (name + score + band). | read-only |
| `files_entity_coalesce_review` | Record entity-coalescing review decisions in BULK: a list of {uuid, action} where action is 'merge'… | read-only |
| `files_entity_coalesce_unapply` | Reverse one coalescence cluster (drop edges, clear flags, strip aliases, tombstone the candidate so… | read-only |
| `files_extract_pending` | Drain leaves with extraction_status='pending' through the LLM fact extractor. | read-only |
| `files_get` | Fetch one record by UUID. | read-only |
| `files_health` | DB integrity + FTS5 sync check. | read-only |
| `files_index` | Return file-level summaries for triage (wiki-index primitive). | read-only |
| `files_ingest` | Walk a directory and ingest supported files into files.db. | read-only |
| `files_link_rename` | Re-point an existing file_node at a new path (rename / move). | read-only |
| `files_promotable` | List top promotion candidates by usage-weighted heuristic score. | read-only |
| `files_promote` | Promote (ascend) a fact / leaf / file_summary from files.db to memory.db. | read-only |
| `files_promotion_list` | List existing promotions. | read-only |
| `files_search` | Hybrid FTS5 + vector search over file-ingestion leaves. | read-only |
| `files_staleness_review` | Compare filesystem against files.db. | read-only |
| `files_stats` | Corpus-level counters: file_nodes, leaves, embed coverage, by-filetype. | read-only |
| `files_watch_once` | Single-pass staleness check + notification dispatch. | read-only |

## 🕸️ Entity Graph

_Extract and query entities and their relationships across sessions._

| Tool | Description | Mutates? |
|---|---|---|
| `entity_get` | Load a single entity with its full neighborhood: predecessors, successors, and linked memory items. | read-only |
| `entity_mentions` | List memory_ids that mention a specific entity in a single conversation. | read-only |
| `entity_search` | Search entities by canonical_name and optionally by entity_type. | read-only |

## 🗂️ Conversations

_Group and inspect turns by conversation / team session._

| Tool | Description | Mutates? |
|---|---|---|
| `conversation_append` | Appends a message to a conversation. | read-only |
| `conversation_search` | Search messages across conversations using hybrid semantic/keyword search. | read-only |
| `conversation_start` | Starts a new conversation thread. | read-only |
| `conversation_summarize` | Summarize a conversation into key points using the local LLM. | read-only |

## 👥 Agents

_Register agents, hand off tasks, and route multi-agent work._

| Tool | Description | Mutates? |
|---|---|---|
| `agent_get` | Get full record for one registered agent. | read-only |
| `agent_heartbeat` | Update last_seen and set status=active. | read-only |
| `agent_list` | List registered agents, optionally filtered by status and/or role. | read-only |
| `agent_offline` | Mark an agent as offline. | ⚠️ yes |
| `agent_register` | Register an agent (UPSERT). | read-only |
| `agent_set_trust` | Set an agent's trust score (0.5-1.0, clamped). | read-only |

## ✅ Tasks

_Track and coordinate agent tasks and their state._

| Tool | Description | Mutates? |
|---|---|---|
| `task_assign` | Assign a task to an owner. | read-only |
| `task_create` | Create a new task in 'pending' state. | read-only |
| `task_delete` | Delete a task. | read-only |
| `task_get` | Get full record for one task. | read-only |
| `task_list` | List tasks with optional filters. | read-only |
| `task_set_result` | Set the result memory pointer for a task. | read-only |
| `task_tree` | Render a recursive subtree of tasks rooted at root_task_id. | read-only |
| `task_update` | Partial update for a task. | read-only |

## 🩺 Diagnostics

_Health, cost, and integrity checks for the memory store._

| Tool | Description | Mutates? |
|---|---|---|
| `embedder_status` | Check the status of the local sovereign embedder server (default port 8082, override via M3_EMBED_F… | read-only |
| `memory_doctor` | Self-service diagnostic for the m3-memory embedding cascade. | read-only |
| `memory_doctor_fix` | Run the m3-memory self-repair mode (m3 doctor --fix). | read-only |

## ⚙️ Admin & Sync

_Maintenance, cross-store sync, import/export, and lifecycle ops._

| Tool | Description | Mutates? |
|---|---|---|
| `enrich_pending` | Enrich pending memory items with SLM-distilled facts. | ⚠️ yes |
| `extract_entities` | Accepts raw text, extracts entities and relationship predicates based on the configured pluggable e… | read-only |
| `extract_pending` | Extract pending entities from the queue. | ⚠️ yes |
| `gdpr_export` | Export all memories for a data subject (GDPR data portability). | ⚠️ yes |
| `gdpr_forget` | Right to be forgotten — hard-deletes ALL data for a user_id including memories, embeddings, relatio… | ⚠️ yes |
| `m3_call` | Invoke ANY m3 catalog tool by name without loading its domain — the low-token path to the full tool… | read-only |
| `m3_help_capabilities` | Discover m3-memory tool capabilities, parameters, and availability. | read-only |
| `m3_index` | List m3 catalog tools (optionally one domain) as structured rows: name, domain, one-line summary, d… | read-only |
| `notifications_ack` | Mark one notification as read. | read-only |
| `notifications_ack_all` | Bulk-ack all unread notifications for an agent. | read-only |
| `notifications_poll` | List notifications addressed to agent_id, newest first. | read-only |
| `notify` | Send a notification to an agent. | read-only |
| `tools_list_domains` | List m3 tool domains (memory, chatlog, files, entity, agent, tasks, conversations, diagnostics, adm… | read-only |
| `tools_load_domain` | Register a tool domain's full surface for the current MCP session. | read-only |

