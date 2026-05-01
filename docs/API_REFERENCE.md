# M3 Memory: API Reference

All 72 MCP tools exposed by the memory bridge (`bin/memory_bridge.py`) and the MCP proxy (`bin/mcp_proxy.py`). For behavioral rules and usage guidance, see [AGENT_INSTRUCTIONS.md](AGENT_INSTRUCTIONS.md).

---

## Memory Operations

### `memory_write`
Creates a MemoryItem and optionally embeds it for semantic search. Contradiction detection is automatic — if new content conflicts with an existing memory of the same type/title, the old one is superseded. Use type='auto' to let the LLM decide the best category.
- **Args**: `type` (str, required), `content` (str, required), `title` (str), `importance` (float, 0.0–1.0), `agent_id` (str), `user_id` (str), `scope` (str), `valid_from` (ISO 8601), `valid_to` (ISO 8601), `embed` (bool, default true), `metadata` (JSON string), `auto_classify` (bool)

### `memory_search`
Search across memory items using semantic similarity or keyword matching. Filter by user_id and scope for isolation.
- **Args**: `query` (str, required), `k` (int, default 8), `type_filter` (str), `agent_filter` (str), `user_id` (str), `scope` (str), `as_of` (ISO 8601), `search_mode` (str: hybrid|semantic|keyword), `recency_bias` (float), `adaptive_k` (bool)

### `memory_suggest`
Preview which memories would be retrieved for a query, with score breakdowns explaining why each was selected.
- **Args**: `query` (str, required), `k` (int, default 5)

### `memory_get`
Retrieves a full MemoryItem by UUID.
- **Args**: `id` (str, required)

### `memory_update`
Updates a MemoryItem by ID. Records audit trail.
- **Args**: `id` (str, required), `content` (str), `title` (str), `metadata` (JSON), `importance` (float), `reembed` (bool), `refresh_on` (ISO 8601)

### `memory_delete`
Deletes a MemoryItem (soft or hard).
- **Args**: `id` (str, required), `hard` (bool, default false)

### `memory_verify`
Verify content integrity by comparing stored hash with computed hash. Returns OK if content hasn't been tampered with.
- **Args**: `id` (str, required)

### `memory_feedback`
Provide feedback on a memory item to improve quality (useful/not_useful/misleading).
- **Args**: `memory_id` (str, required), `feedback` (str)

---

## Knowledge Graph

### `memory_link`
Creates a directional link between two memory items. Valid types: related, supports, contradicts, extends, supersedes, references, consolidates, message, handoff.
- **Args**: `from_id` (str, required), `to_id` (str, required), `relationship_type` (str)

### `memory_graph`
Returns the local graph neighborhood of a memory item (connected memories up to N hops, max 3).
- **Args**: `memory_id` (str, required), `depth` (int, default 1)

### `memory_history`
Returns the change history (audit trail) for a memory item. Tracks create, update, delete, and supersede events.
- **Args**: `memory_id` (str, required), `limit` (int, default 20)

---

## Conversations

### `conversation_start`
Starts a new conversation thread.
- **Args**: `title` (str, required), `agent_id` (str), `tags` (str)

### `conversation_append`
Appends a message to a conversation.
- **Args**: `conversation_id` (str, required), `role` (str, required), `content` (str, required), `embed` (bool)

### `conversation_search`
Search messages across conversations using hybrid semantic/keyword search.
- **Args**: `query` (str, required), `k` (int, default 8)

### `conversation_summarize`
Summarize a conversation into key points using the local LLM.
- **Args**: `conversation_id` (str, required), `threshold` (int, default 20)

---

## Task Management

### `task_create`
Create a new task in 'pending' state. Returns task id.
- **Args**: `title` (str, required), `created_by` (str, required), `description` (str), `owner_agent` (str), `parent_task_id` (str), `metadata` (dict)

### `task_assign`
Assign a task to an owner. Sets state=in_progress and notifies the new owner.
- **Args**: `task_id` (str, required), `owner_agent` (str, required)

### `task_update`
Partial update for a task. Validates state transitions. On terminal state, sets completed_at.
- **Args**: `task_id` (str, required), `state` (str), `description` (str), `metadata` (dict), `actor` (str)

### `task_delete`
Delete a task (soft or hard).
- **Args**: `task_id` (str, required), `hard` (bool), `actor` (str)

### `task_set_result`
Set the result memory pointer for a task. Does NOT change state.
- **Args**: `task_id` (str, required), `result_memory_id` (str, required)

### `task_get`
Get full record for one task.
- **Args**: `task_id` (str, required)

### `task_list`
List tasks with optional filters (owner, state, parent). Newest updated first.
- **Args**: `owner_agent` (str), `state` (str), `parent_task_id` (str), `limit` (int)

### `task_tree`
Render a recursive subtree of tasks rooted at root_task_id.
- **Args**: `root_task_id` (str, required), `max_depth` (int, default 3)

---

## Agent Registry & Notifications

### `agent_register`
Register an agent (UPSERT). Sets status=active, last_seen=now.
- **Args**: `agent_id` (str, required), `role` (str), `capabilities` (list), `metadata` (dict)

### `agent_heartbeat`
Update last_seen and set status=active. Errors if not registered.
- **Args**: `agent_id` (str, required)

### `agent_list`
List registered agents, optionally filtered by status and/or role.
- **Args**: `status` (str), `role` (str)

### `agent_get`
Get full record for one registered agent.
- **Args**: `agent_id` (str, required)

### `agent_offline`
Mark an agent as offline.
- **Args**: `agent_id` (str, required)

### `notify`
Send a notification to an agent. Lightweight wake signal — agents poll notifications_poll.
- **Args**: `agent_id` (str, required), `kind` (str, required), `payload` (dict)

### `notifications_poll`
List notifications addressed to agent_id, newest first.
- **Args**: `agent_id` (str, required), `unread_only` (bool), `limit` (int)

### `notifications_ack`
Mark one notification as read.
- **Args**: `notification_id` (int, required)

### `notifications_ack_all`
Bulk-ack all unread notifications for an agent.
- **Args**: `agent_id` (str, required)

---

## Multi-Agent Coordination

### `memory_handoff`
Hand off a task from one agent to another. Writes a 'handoff' memory and links context.
- **Args**: `from_agent` (str, required), `to_agent` (str, required), `task` (str, required), `context_ids` (list), `note` (str), `task_id` (str)

### `memory_inbox`
List handoff messages addressed to agent_id, newest first.
- **Args**: `agent_id` (str, required), `unread_only` (bool), `limit` (int)

### `memory_inbox_ack`
Mark a handoff memory as read.
- **Args**: `memory_id` (str, required)

### `memory_refresh_queue`
List memories whose refresh_on timestamp has arrived and need review.
- **Args**: `agent_id` (str), `limit` (int), `include_future` (bool)

---

## Chat Log System

### `chatlog_write`
Append one chat turn to the chat log DB. Provenance (host_agent, provider, model_id, conversation_id) is required.
- **Args**: `content` (str, required), `role` (str, required), `conversation_id` (str, required), `host_agent` (str, required), `provider` (str, required), `model_id` (str, required), `turn_index` (int), `tokens_in` (int), `tokens_out` (int), `cost_usd` (float), `latency_ms` (int)

### `chatlog_write_bulk`
Bulk-append N chat turns.
- **Args**: `items` (list, required)

### `chatlog_search`
Search chat_log rows using FTS5.
- **Args**: `query` (str, required), `k` (int), `conversation_id` (str), `host_agent` (str), `since` (ISO 8601), `until` (ISO 8601)

### `chatlog_promote`
Promote chat_log rows into the main memory DB under a new type (default 'conversation').
- **Args**: `ids` (list), `conversation_id` (str), `since` (ISO 8601), `until` (ISO 8601), `copy` (bool), `target_type` (str)

### `chatlog_list_conversations`
List distinct conversation_ids with turn counts and timespans.
- **Args**: `host_agent` (str), `limit` (int), `offset` (int)

### `chatlog_cost_report`
Aggregate tokens and cost_usd across chat_log rows.
- **Args**: `since` (ISO 8601), `until` (ISO 8601), `group_by` (str: provider|model_id|host_agent|conversation_id|day)

### `chatlog_set_redaction`
Flip redaction on/off and update patterns.
- **Args**: `enabled` (bool, required), `patterns` (list), `redact_pii` (bool), `custom_regex` (list), `store_original_hash` (bool)

### `chatlog_status`
One-call health summary of the chat log subsystem.
- **Args**: None

### `chatlog_rescrub`
Re-apply redaction to existing chat_log rows.
- **Args**: `conversation_id` (str), `since` (ISO 8601), `until` (ISO 8601), `limit` (int)

---

## Operational Protocol (Proxy-Only)

### `log_activity`
Archive activity to the agent log (Protocols #1-#3).
- **Args**: `category` (str: thought|hardware|decision), `detail_a` (str, required), `detail_b` (str), `detail_c` (str)

### `query_decisions`
Protocol #4 - MUST call before starting any new task. Search project_decisions table.
- **Args**: `keyword` (str, required), `limit` (int, default 10)

### `update_focus`
Protocol #5 - Call every 3 turns with a <=10-word trajectory summary.
- **Args**: `summary` (str, required)

### `retire_focus`
Protocol #5 - Clear dashboard focus when a task completes.
- **Args**: None

### `check_thermal_load`
Protocol #2 - Check M3 Max thermal/RAM pressure. Returns Nominal|Fair|Serious|Critical.
- **Args**: None

---

## Debug Agent (Proxy-Only)

### `debug_analyze`
Root cause analysis with memory-augmented reasoning.
- **Args**: `error_message` (str, required), `context` (str), `file_path` (str)

### `debug_bisect`
Automated git bisect with LLM analysis of the offending commit.
- **Args**: `test_command` (str, required), `good_commit` (str, required), `bad_commit` (str)

### `debug_trace`
Execution flow analysis - reads source, finds callers, identifies failure points.
- **Args**: `file_path` (str, required), `function_name` (str, required), `error_type` (str)

### `debug_correlate`
Cross-reference logs, git commits, and decisions to build a causal timeline.
- **Args**: `log_file` (str, required), `time_range` (str), `pattern` (str)

### `debug_history`
Search past debugging sessions and patterns. No LLM required.
- **Args**: `keyword` (str, required), `limit` (int)

### `debug_report`
Generate and persist a structured debugging report to memory.
- **Args**: `title` (str, required), `issue_id` (str), `findings` (str)

---

## Lifecycle & Maintenance

### `memory_maintenance`
Run decay, expiry purge, orphan pruning, auto-archival, and retention enforcement.
- **Args**: `decay` (bool), `purge_expired` (bool), `prune_orphan_embeddings` (bool)

### `memory_dedup`
Find and optionally remove near-duplicate memories.
- **Args**: `threshold` (float, default 0.92), `dry_run` (bool, default true)

### `memory_consolidate`
Merge groups of old memories into LLM-generated summaries.
- **Args**: `type_filter` (str), `agent_filter` (str), `threshold` (int)

### `memory_set_retention`
Set per-agent retention limits.
- **Args**: `agent_id` (str, required), `max_memories` (int), `ttl_days` (int), `auto_archive` (int)

---

## Data Governance

### `gdpr_export`
Export all memories for a user as portable JSON (Article 20).
- **Args**: `user_id` (str, required)

### `gdpr_forget`
Hard-delete all data for a user (Article 17).
- **Args**: `user_id` (str, required)

### `memory_export`
Export memories as portable JSON for backup or migration.
- **Args**: `agent_filter` (str), `type_filter` (str), `since` (ISO 8601)

### `memory_import`
Import from a previous export. UPSERT semantics — safe to re-run.
- **Args**: `data` (JSON string, required)

---

## Infrastructure Operations

### `memory_cost_report`
Check session operation counts (embed calls, tokens, searches, writes).
- **Args**: None

### `chroma_sync`
Manual sync with ChromaDB.
- **Args**: `direction` (str: push|pull|both), `max_items` (int), `reset_stalled` (bool)
