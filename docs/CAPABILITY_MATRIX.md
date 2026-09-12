# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Capability Matrix

> **Generated** by `bin/gen_capability_matrix.py` from `docs/tools/MCP_CATALOG.json` — do not edit by hand; re-run after any tool-catalog change. This is the single scannable index of *what M3 can do* and *which tool does it*, for humans, search engines, and AI agents.

**111 tools across 9 capability groups.** The **Consent** column reflects the dispatch gate: a ⚠️ tool will not run until it is explicitly allowed (it deletes, exports, or runs a bulk/long operation), while a default-allowed tool runs without extra opt-in. It is **not** a read/write distinction — `memory_write` is default-allowed, and read-only `memory_export` is not.

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
- [⚙️ Admin & Sync](#admin--sync) — Maintenance, cross-store sync, import/export, and lifecycle ops. (15 tools)

## 🧠 Memory

_Write, retrieve, version, and reconcile long-term agent memory._

| Tool | Description | Consent |
|---|---|---|
| `curate_memory_apply` | Deterministically apply a memory.db curator plan in ONE call. | ⚠️ opt-in required |
| `memory_consolidate` | Consolidate old memories of the same type into summaries using the local LLM. | default-allowed |
| `memory_cost_report` | Returns current session operation counts and estimated token usage for memory operations. | default-allowed |
| `memory_count_entities` | Count distinct entities mentioned in a single conversation. | default-allowed |
| `memory_count_mentions` | Per-entity mention frequency within a single conversation, sorted DESC by count. | default-allowed |
| `memory_dedup` | Find (and optionally soft-delete) near-duplicate memory items by cosine similarity over embeddings. | default-allowed |
| `memory_delete` | Deletes a MemoryItem (soft or hard). | ⚠️ opt-in required |
| `memory_delete_bulk` | Deletes a list of MemoryItems (soft or hard) in one transaction per chunk. | ⚠️ opt-in required |
| `memory_export` | Export memories as portable JSON. | ⚠️ opt-in required |
| `memory_feedback` | Provide feedback on a memory item to improve quality. | default-allowed |
| `memory_get` | Retrieves a full MemoryItem; accepts full UUID or 8-char prefix; ambiguous prefixes return an error. | default-allowed |
| `memory_graph` | Returns the local graph neighborhood of a memory item (connected memories up to N hops, max 3). | default-allowed |
| `memory_handoff` | Hand off a task from one agent to another. | default-allowed |
| `memory_history` | Returns the change history (audit trail) for a memory item. | default-allowed |
| `memory_import` | Import memories from a JSON export. | ⚠️ opt-in required |
| `memory_inbox` | List handoff messages addressed to agent_id, newest first. | default-allowed |
| `memory_inbox_ack` | Mark a handoff memory as read (sets read_at = now). | default-allowed |
| `memory_lifecycle_summary` | Windowed summary of lifecycle & contradiction activity over the last `window_days` days: counts of… | default-allowed |
| `memory_link` | Creates a directional link between two memory items. | default-allowed |
| `memory_link_bulk` | Create many memory_relationships rows in one transaction per chunk. | default-allowed |
| `memory_maintenance` | Runs maintenance tasks on the memory store. | ⚠️ opt-in required |
| `memory_pin` | Pin a memory to exempt it from decay, expiry, and retention purges. | default-allowed |
| `memory_refresh_queue` | List memories whose refresh_on timestamp has arrived and need review. | default-allowed |
| `memory_search` | Search across memory items using semantic similarity or keyword matching. | default-allowed |
| `memory_search_multi_db` | Search across multiple SQLite databases (e.g. | ⚠️ opt-in required |
| `memory_search_routed` | Temporal-aware routed retrieval. | ⚠️ opt-in required |
| `memory_search_scored` | Structured hybrid FTS5+vector+MMR search. | default-allowed |
| `memory_set_retention` | Set or update per-agent memory retention policy. | ⚠️ opt-in required |
| `memory_suggest` | Preview which memories would be retrieved for a query, with score breakdowns explaining why each wa… | default-allowed |
| `memory_supersede` | Explicitly supersede an existing memory with a new one. | default-allowed |
| `memory_unpin` | Unpin a memory, restoring normal decay/expiry/retention handling. | default-allowed |
| `memory_update` | Updates a MemoryItem by ID. | default-allowed |
| `memory_update_bulk` | Apply many metadata-only updates in one transaction per chunk. | default-allowed |
| `memory_verify` | Verify content integrity by comparing stored hash with computed hash. | default-allowed |
| `memory_write` | Creates a MemoryItem and optionally embeds it for semantic search. | default-allowed |
| `memory_write_from_file` | Write a memory whose content is read from a file on disk. | default-allowed |

## 💬 Chat Log

_Capture verbatim conversation turns before compaction; audit and replay._

| Tool | Description | Consent |
|---|---|---|
| `chatlog_cost_report` | Aggregate tokens and cost_usd across chat_log rows. | default-allowed |
| `chatlog_list_conversations` | List distinct conversation_ids with turn counts and timespans. | default-allowed |
| `chatlog_promote` | Promote chat_log rows into the main memory DB under a new type (default 'conversation'). | default-allowed |
| `chatlog_rescrub` | Re-apply redaction to existing chat_log rows. | default-allowed |
| `chatlog_search` | Search chat_log rows. | default-allowed |
| `chatlog_set_redaction` | Flip redaction on/off and update patterns. | default-allowed |
| `chatlog_status` | One-call health summary of the chat log subsystem: mode, DB paths, row counts, queue depth, spill f… | default-allowed |
| `chatlog_write` | Append one chat turn to the chat log DB. | default-allowed |
| `chatlog_write_bulk` | Bulk-append N chat turns. | default-allowed |
| `curate_chatlog_apply` | Deterministically apply a chatlog.db curator plan in ONE call. | ⚠️ opt-in required |

## 📁 Files Memory

_Index, search, and recall project files as memory._

| Tool | Description | Consent |
|---|---|---|
| `files_corpus_create` | Register a new corpus with optional default overrides. | default-allowed |
| `files_corpus_delete` | Delete a corpus's settings row. | ⚠️ opt-in required |
| `files_corpus_get` | Fetch a single corpus's settings + counts. | default-allowed |
| `files_corpus_list` | Enumerate corpora with row counts. | default-allowed |
| `files_corpus_set` | Update settings for an existing corpus. | default-allowed |
| `files_dedup` | Scan leaf embeddings for near-duplicates above cosine threshold. | default-allowed |
| `files_dedup_list` | List near-duplicate candidate pairs with text snippets and paths. | default-allowed |
| `files_dedup_review` | Record a review decision on a near-duplicate candidate: 'kept' \| 'merged' \| 'ignored'. | default-allowed |
| `files_entity_coalesce` | Detect provisional-entity coalescing candidates (quarantine noise + flag near-duplicate entities). | default-allowed |
| `files_entity_coalesce_apply` | Apply the reversible same_as/cluster overlay. | default-allowed |
| `files_entity_coalesce_list` | List entity-coalescing candidate pairs (name + score + band). | default-allowed |
| `files_entity_coalesce_review` | Record entity-coalescing review decisions in BULK: a list of {uuid, action} where action is 'merge'… | default-allowed |
| `files_entity_coalesce_unapply` | Reverse one coalescence cluster (drop edges, clear flags, strip aliases, tombstone the candidate so… | default-allowed |
| `files_extract_pending` | Drain leaves with extraction_status='pending' through the LLM fact extractor. | default-allowed |
| `files_get` | Fetch one record by UUID. | default-allowed |
| `files_health` | DB integrity + FTS5 sync check. | default-allowed |
| `files_index` | Return file-level summaries for triage (wiki-index primitive). | default-allowed |
| `files_ingest` | Walk a directory and ingest supported files into files.db. | default-allowed |
| `files_link_rename` | Re-point an existing file_node at a new path (rename / move). | default-allowed |
| `files_promotable` | List top promotion candidates by usage-weighted heuristic score. | default-allowed |
| `files_promote` | Promote (ascend) a fact / leaf / file_summary from files.db to memory.db. | default-allowed |
| `files_promotion_list` | List existing promotions. | default-allowed |
| `files_search` | Hybrid FTS5 + vector search over file-ingestion leaves. | default-allowed |
| `files_staleness_review` | Compare filesystem against files.db. | default-allowed |
| `files_stats` | Corpus-level counters: file_nodes, leaves, embed coverage, by-filetype. | default-allowed |
| `files_watch_once` | Single-pass staleness check + notification dispatch. | default-allowed |

## 🕸️ Entity Graph

_Extract and query entities and their relationships across sessions._

| Tool | Description | Consent |
|---|---|---|
| `entity_get` | Load a single entity with its full neighborhood: predecessors, successors, and linked memory items. | default-allowed |
| `entity_mentions` | List memory_ids that mention a specific entity in a single conversation. | default-allowed |
| `entity_search` | Search entities by canonical_name and optionally by entity_type. | default-allowed |

## 🗂️ Conversations

_Group and inspect turns by conversation / team session._

| Tool | Description | Consent |
|---|---|---|
| `conversation_append` | Appends a message to a conversation. | default-allowed |
| `conversation_search` | Search messages across conversations using hybrid semantic/keyword search. | default-allowed |
| `conversation_start` | Starts a new conversation thread. | default-allowed |
| `conversation_summarize` | Summarize a conversation into key points using the local LLM. | default-allowed |

## 👥 Agents

_Register agents, hand off tasks, and route multi-agent work._

| Tool | Description | Consent |
|---|---|---|
| `agent_get` | Get full record for one registered agent. | default-allowed |
| `agent_heartbeat` | Update last_seen and set status=active. | default-allowed |
| `agent_list` | List registered agents, optionally filtered by status and/or role. | default-allowed |
| `agent_offline` | Mark an agent as offline. | ⚠️ opt-in required |
| `agent_register` | Register an agent (UPSERT). | default-allowed |
| `agent_set_trust` | Set an agent's trust score (0.5-1.0, clamped). | default-allowed |

## ✅ Tasks

_Track and coordinate agent tasks and their state._

| Tool | Description | Consent |
|---|---|---|
| `task_assign` | Assign a task to an owner. | default-allowed |
| `task_create` | Create a new task in 'pending' state. | default-allowed |
| `task_delete` | Delete a task. | default-allowed |
| `task_get` | Get full record for one task. | default-allowed |
| `task_list` | List tasks with optional filters. | default-allowed |
| `task_set_result` | Set the result memory pointer for a task. | default-allowed |
| `task_tree` | Render a recursive subtree of tasks rooted at root_task_id. | default-allowed |
| `task_update` | Partial update for a task. | default-allowed |

## 🩺 Diagnostics

_Health, cost, and integrity checks for the memory store._

| Tool | Description | Consent |
|---|---|---|
| `embedder_status` | Check the status of the local sovereign embedder server (default port 8082, override via M3_EMBED_F… | default-allowed |
| `memory_doctor` | Self-service diagnostic for the m3-memory embedding cascade. | default-allowed |
| `memory_doctor_fix` | Run the m3-memory self-repair mode (m3 doctor --fix). | default-allowed |

## ⚙️ Admin & Sync

_Maintenance, cross-store sync, import/export, and lifecycle ops._

| Tool | Description | Consent |
|---|---|---|
| `enrich_pending` | Enrich pending memory items with SLM-distilled facts. | ⚠️ opt-in required |
| `extract_entities` | Accepts raw text, extracts entities and relationship predicates based on the configured pluggable e… | default-allowed |
| `extract_pending` | Extract pending entities from the queue. | ⚠️ opt-in required |
| `gdpr_export` | Export all memories for a data subject (GDPR data portability). | ⚠️ opt-in required |
| `gdpr_forget` | Right to be forgotten — hard-deletes ALL data for a user_id including memories, embeddings, relatio… | ⚠️ opt-in required |
| `m3_call` | Invoke ANY m3 catalog tool by name without loading its domain — the low-token path to the full tool… | default-allowed |
| `m3_help_capabilities` | Discover m3-memory tool capabilities, parameters, and availability. | default-allowed |
| `m3_index` | List m3 catalog tools (optionally one domain) as structured rows: name, domain, one-line summary, d… | default-allowed |
| `notifications_ack` | Mark one notification as read. | default-allowed |
| `notifications_ack_all` | Bulk-ack all unread notifications for an agent. | default-allowed |
| `notifications_mark_received` | Stamp transport receipt on an agent's undelivered notifications. | default-allowed |
| `notifications_poll` | List notifications addressed to agent_id, newest first. | default-allowed |
| `notify` | Send a notification to an agent. | default-allowed |
| `tools_list_domains` | List m3 tool domains (memory, chatlog, files, entity, agent, tasks, conversations, diagnostics, adm… | default-allowed |
| `tools_load_domain` | Register a tool domain's full surface for the current MCP session. | default-allowed |

