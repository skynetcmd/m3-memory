# MCP Tool Inventory

This document provides a comprehensive inventory of all 71 MCP tools available in the M3 Memory system.

## Summary Table

| Name | Category | Description |
| --- | --- | --- |
| `memory_delete` | Memory Operations | Deletes a MemoryItem (soft or hard). |
| `memory_feedback` | Memory Operations | Provide feedback on a memory item to improve quality. |
| `memory_get` | Memory Operations | Retrieves a full MemoryItem by UUID. |
| `memory_search` | Memory Operations | Search across memory items using semantic similarity or keyword matching. Filter by user_id and scope for isolation. |
| `memory_search_routed` | Memory Operations | Temporal-aware routed retrieval. Routes temporal queries to verbatim search at k+temporal_k_bump; non-temporal queries to (optionally fact-fused) max-kind search at k. Pass fact_variant for two-tier fact-fusion. Optional graph_depth and expand_sessions add post-retrieval neighbor expansion. |
| `memory_suggest` | Memory Operations | Preview which memories would be retrieved for a query, with score breakdowns explaining why each was selected. |
| `memory_update` | Memory Operations | Updates a MemoryItem by ID. |
| `memory_verify` | Memory Operations | Verify content integrity by comparing stored hash with computed hash. Returns OK if content hasn't been tampered with. |
| `memory_write` | Memory Operations | Creates a MemoryItem and optionally embeds it for semantic search. Contradiction detection is automatic — if new content conflicts with an existing memory of the same type/title, the old one is superseded. Use type='auto' to let the LLM decide the best category. |
| `enrich_pending` | Knowledge Graph | Enrich pending memory items with SLM-distilled facts. Default dry_run=true reports count + ETA; pass dry_run=false to execute. |
| `entity_get` | Knowledge Graph | Load a single entity with its full neighborhood: predecessors, successors, and linked memory items. |
| `entity_search` | Knowledge Graph | Search entities by canonical_name and optionally by entity_type. Returns list of matching entities with optional neighbor counts. |
| `extract_pending` | Knowledge Graph | Extract pending entities from the queue. Default dry_run=true reports count + ETA; pass dry_run=false to execute. |
| `memory_graph` | Knowledge Graph | Returns the local graph neighborhood of a memory item (connected memories up to N hops, max 3). |
| `memory_history` | Knowledge Graph | Returns the change history (audit trail) for a memory item. Tracks create, update, delete, and supersede events. |
| `memory_link` | Knowledge Graph | Creates a directional link between two memory items. Valid types: related, supports, contradicts, extends, supersedes, references, consolidates, message, handoff. |
| `conversation_append` | Conversations | Appends a message to a conversation. |
| `conversation_search` | Conversations | Search messages across conversations using hybrid semantic/keyword search. |
| `conversation_start` | Conversations | Starts a new conversation thread. |
| `conversation_summarize` | Conversations | Summarize a conversation into key points using the local LLM. |
| `task_assign` | Task Management | Assign a task to an owner. Sets state=in_progress and notifies the new owner. |
| `task_create` | Task Management | Create a new task in 'pending' state. Returns task id. |
| `task_delete` | Task Management | Delete a task. Soft-delete (default) sets a tombstone that propagates via pg_sync to the warehouse and peers. Hard-delete removes the row locally and requires a prior soft-delete. |
| `task_get` | Task Management | Get full record for one task. |
| `task_list` | Task Management | List tasks with optional filters. Newest updated first. |
| `task_set_result` | Task Management | Set the result memory pointer for a task. Does NOT change state. |
| `task_tree` | Task Management | Render a recursive subtree of tasks rooted at root_task_id. |
| `task_update` | Task Management | Partial update for a task. Validates state transitions. On terminal state, sets completed_at. |
| `agent_get` | Agent Registry & Notifications | Get full record for one registered agent. |
| `agent_heartbeat` | Agent Registry & Notifications | Update last_seen and set status=active. Errors if not registered. |
| `agent_list` | Agent Registry & Notifications | List registered agents, optionally filtered by status and/or role. |
| `agent_offline` | Agent Registry & Notifications | Mark an agent as offline. |
| `agent_register` | Agent Registry & Notifications | Register an agent (UPSERT). Sets status=active, last_seen=now. |
| `notifications_ack` | Agent Registry & Notifications | Mark one notification as read. |
| `notifications_ack_all` | Agent Registry & Notifications | Bulk-ack all unread notifications for an agent. Returns count acked. |
| `notifications_poll` | Agent Registry & Notifications | List notifications addressed to agent_id, newest first. |
| `notify` | Agent Registry & Notifications | Send a notification to an agent. Lightweight wake signal — agents poll notifications_poll. |
| `memory_handoff` | Multi-Agent Coordination | Hand off a task from one agent to another. Writes a new handoff-type memory owned by to_agent and links it to the given context memories with 'handoff' edges. Returns a confirmation string with the new memory id. |
| `memory_inbox` | Multi-Agent Coordination | List handoff messages addressed to agent_id, newest first. Pass unread_only=False to include already-acked items. |
| `memory_inbox_ack` | Multi-Agent Coordination | Mark a handoff memory as read (sets read_at = now). |
| `memory_refresh_queue` | Multi-Agent Coordination | List memories whose refresh_on timestamp has arrived and need review. Read-only — to actually refresh a memory, call memory_update with new content/refresh_on. Pass include_future=True to see all memories with refresh_on set, not just overdue ones. |
| `chatlog_cost_report` | Chat Log System | Aggregate tokens and cost_usd across chat_log rows. Groups: provider|model_id|host_agent|conversation_id|day. |
| `chatlog_list_conversations` | Chat Log System | List distinct conversation_ids with turn counts and timespans. |
| `chatlog_promote` | Chat Log System | Promote chat_log rows into the main memory DB under a new type (default 'conversation'). ATTACH + INSERT SELECT in separate/hybrid; UPDATE type in integrated. |
| `chatlog_rescrub` | Chat Log System | Re-apply redaction to existing chat_log rows. Requires redaction.enabled=true. |
| `chatlog_search` | Chat Log System | Search chat_log rows. FTS5 keyword when query is non-empty; filter-only when empty. |
| `chatlog_set_redaction` | Chat Log System | Flip redaction on/off and update patterns. Persists to memory/.chatlog_config.json. |
| `chatlog_status` | Chat Log System | One-call health summary of the chat log subsystem: mode, DB paths, row counts, queue depth, spill files, embed backlog, hook timestamps, redaction state, warnings. |
| `chatlog_write` | Chat Log System | Append one chat turn to the chat log DB. Provenance (host_agent, provider, model_id, conversation_id) is required. Writes are async-queued — returns the row id immediately. |
| `chatlog_write_bulk` | Chat Log System | Bulk-append N chat turns. Each item needs the same required fields as chatlog_write. |
| `check_thermal_load` | Operational Protocol | Protocol #2 - Check M3 Max thermal/RAM pressure. Returns Nominal|Fair|Serious|Critical. |
| `log_activity` | Operational Protocol | Archive activity to the agent log (Protocols #1-#3). category=thought for complex reasoning, hardware after thermal check, decision when user agrees to any code change, file move, or direction. |
| `query_decisions` | Operational Protocol | Protocol #4 - MUST call before starting any new task. Full-text search across project_decisions table for prior decisions. |
| `retire_focus` | Operational Protocol | Protocol #5 - Clear dashboard focus when a task completes. |
| `update_focus` | Operational Protocol | Protocol #5 - Call every 3 turns with a <=10-word trajectory summary. |
| `debug_analyze` | Debug Agent | Root cause analysis with memory-augmented reasoning. Searches past issues, reads source, uses local LLM to diagnose. |
| `debug_bisect` | Debug Agent | Automated git bisect with LLM analysis of the offending commit. |
| `debug_correlate` | Debug Agent | Cross-reference logs, git commits, and decisions to build a causal timeline. |
| `debug_history` | Debug Agent | Search past debugging sessions and patterns. No LLM required. |
| `debug_report` | Debug Agent | Generate and persist a structured debugging report to memory. |
| `debug_trace` | Debug Agent | Execution flow analysis - reads source, finds callers, identifies failure points. |
| `memory_consolidate` | Lifecycle & Maintenance | Consolidate old memories of the same type into summaries using the local LLM. Reduces clutter while preserving knowledge. |
| `memory_dedup` | Lifecycle & Maintenance | Find and merge near-duplicate memory items. |
| `memory_maintenance` | Lifecycle & Maintenance | Runs maintenance tasks on the memory store. |
| `memory_set_retention` | Lifecycle & Maintenance | Set or update per-agent memory retention policy. Controls max memory count, TTL expiry, and auto-archival. |
| `gdpr_export` | Data Governance | Export all memories for a data subject (GDPR data portability). Returns JSON with all memory items for the given user_id. |
| `gdpr_forget` | Data Governance | Right to be forgotten — hard-deletes ALL data for a user_id including memories, embeddings, relationships, and history. |
| `memory_export` | Data Governance | Export memories as portable JSON. Filter by agent, type, or date. |
| `memory_import` | Data Governance | Import memories from a JSON export. UPSERT semantics — safe to re-run. |
| `chroma_sync` | Infrastructure Operations | Bi-directional sync between local SQLite and ChromaDB. |
| `memory_cost_report` | Infrastructure Operations | Returns current session operation counts and estimated token usage for memory operations. |

---

## Memory Operations

### `memory_delete`

Deletes a MemoryItem (soft or hard).

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `id` | `string` | Yes | Memory item UUID. | `-` |
| `hard` | `boolean` | No | Hard delete (permanent). | `False` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_feedback`

Provide feedback on a memory item to improve quality.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `memory_id` | `string` | Yes | Memory item UUID. | `-` |
| `feedback` | `string` | No | Feedback type. | `useful` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_get`

Retrieves a full MemoryItem by UUID.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `id` | `string` | Yes | Memory item UUID. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_search`

Search across memory items using semantic similarity or keyword matching. Filter by user_id and scope for isolation.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `query` | `string` | Yes | Search query. | `-` |
| `k` | `integer` | No | Max results (1-100). | `8` |
| `type_filter` | `string` | No | Restrict to a memory type. | `` |
| `agent_filter` | `string` | No | Restrict to an agent id. | `` |
| `search_mode` | `string` | No | Retrieval mode. | `hybrid` |
| `include_scratchpad` | `boolean` | No | Include ephemeral scratchpad items. | `False` |
| `user_id` | `string` | No | Filter by data subject. | `` |
| `scope` | `string` | No | Filter by isolation scope. | `` |
| `as_of` | `string` | No | ISO-8601 time-travel cutoff. | `` |
| `conversation_id` | `string` | No | Restrict to a conversation / team session. | `` |
| `recency_bias` | `number` | No | Boost newer items (0.0=off, 0.1-0.2=moderate, higher=aggressive). Useful for 'current' or 'latest' queries. | `0.0` |
| `adaptive_k` | `boolean` | No | Auto-trim results at the score drop-off point. WARNING: can regress on temporal, knowledge-update, and multi-session queries; safe only for sharp-curve queries where most retrievals would be noise. Prefer `auto_route=True` on memory_search_routed for safer multi-signal routing. | `False` |
| `variant` | `string` | No | Ingest-pipeline filter. '' = real user data only (default, equivalent to IS NULL). Pass a specific variant name (e.g. 'heuristic_c1c4') to scope to that bench ingest. | `` |
| `include_bench_data` | `boolean` | No | Opt in to LOCOMO / LongMemEval bench rows. Default False hides any row with a variant tag. | `False` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_search_routed`

Temporal-aware routed retrieval. Routes temporal queries to verbatim search at k+temporal_k_bump; non-temporal queries to (optionally fact-fused) max-kind search at k. Pass fact_variant for two-tier fact-fusion. Optional graph_depth and expand_sessions add post-retrieval neighbor expansion.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `query` | `string` | Yes | Search query. | `-` |
| `k` | `integer` | No | Top-K to return. | `10` |
| `fact_variant` | `string` | No | Optional fact-tier variant to fuse with base. Empty = single-variant. | `` |
| `temporal_k_bump` | `integer` | No | Extra slots added when query is temporal. | `5` |
| `graph_depth` | `integer` | No | If > 0, traverse memory_relationships up to N hops from each top-K hit and re-fuse. Clamped to 3. | `0` |
| `expand_sessions` | `boolean` | No | If true, pull all turns sharing each top-K hit's conversation_id (capped at session_cap) and re-fuse. | `False` |
| `session_cap` | `integer` | No | Per-session turn cap when expand_sessions=true. | `12` |
| `user_id` | `string` | No |  | `` |
| `scope` | `string` | No |  | `` |
| `type_filter` | `string` | No |  | `` |
| `agent_filter` | `string` | No |  | `` |
| `search_mode` | `string` | No |  | `hybrid` |
| `variant` | `string` | No |  | `` |
| `as_of` | `string` | No |  | `` |
| `conversation_id` | `string` | No |  | `` |
| `explain` | `boolean` | No |  | `False` |
| `entity_graph` | `boolean` | No | Direct lever for entity-graph expansion. Default False = OFF (production default; matches memory_search behavior). True = parse query for named entities, traverse entity_relationships up to entity_graph_depth hops, fold matched memory_ids into the result set tagged expanded_via='entity_graph'. The AUTO layer can also flip this on for the entity_anchored branch (see auto_entity_graph_enabled), but caller-explicit entity_graph=True works without auto_route. Use for benchmarking + production opt-in once empirically validated. See decision memory `c98817ca` and `931774b0` for context. | `False` |
| `rerank` | `boolean` | No | Cross-encoder reranking. Default False = OFF (production behavior unchanged). True = re-score top (rerank_pool_k or 3*k) hits with sentence-transformers CrossEncoder, blend with hybrid score per rerank_blend, re-sort. Adds ~12MB-560MB model download (cached at ~/.cache/torch) + ~50ms/pair on GPU, ~200ms/pair on CPU. Opt-in for production retrieval. | `False` |
| `rerank_model` | `string` | No | Cross-encoder model id. Default empty = DEFAULT_RERANK_MODEL (cross-encoder/ms-marco-MiniLM-L-6-v2 — small, fast, English-tuned). Higher-accuracy alternative: BAAI/bge-reranker-v2-m3 (multilingual, larger, slower). Override via M3_RERANK_MODEL env var. | `` |
| `rerank_pool_k` | `integer` | No | Pool size before rerank. Default 0 = 3*k. Higher pool = more candidates rescored = slower but potentially higher recall. Never truncates below final k. Only used when rerank=True. | `0` |
| `rerank_blend` | `number` | No | Blend factor: final_score = rerank_blend * ce_score + (1 - rerank_blend) * hybrid_score. Default 1.0 = pure CE replacement (most aggressive). 0.5 = average. 0.3 = CE as tiebreaker over hybrid. 0.0 = no-op (skip rerank — same effect as rerank=False). Only used when rerank=True. | `1.0` |
| `entity_graph_depth` | `integer` | No | BFS hop count over entity_relationships when entity_graph=True. Default 1 = direct neighbors only. Clamped to [1,3] core-side. Higher depth pulls more neighbors but adds noise; depth=2 typically helps multi-hop questions but regresses sharp single-fact lookups. | `1` |
| `entity_graph_max_neighbors` | `integer` | No | Cap on entity nodes discovered during BFS traversal when entity_graph=True. Default 20. Clamped to [1,100] core-side. Lower = fewer rows folded in but tighter relevance; higher = broader recall at cost of precision. | `20` |
| `auto_route` | `boolean` | No | Multi-signal retrieval routing. Default False = no auto-routing (existing behavior). True = router picks branch (temporal/multi_session/sharp/default) and fills in unset parameters with branch-specific values; caller-explicit values still win. | `False` |
| `auto_top1_sharp_min` | `number` | No | Top-1 score above which query is marked sharp. Default 0.89. Used by sharp branch to detect high-confidence queries. | `0.89` |
| `auto_slope_at_3_sharp_min` | `number` | No | Slope-at-3 (score drop per rank) above which query is marked sharp. Default 0.08. Steeper curves = more relevance discrimination. | `0.08` |
| `auto_conv_id_diversity_threshold` | `integer` | No | Number of distinct conversation IDs in top-10 hits above which query is routed to multi_session. Default 5. Higher threshold = only very scattered results trigger expansion. | `5` |
| `auto_top1_low_threshold` | `number` | No | Score floor for sharp detection (OOD guard). Default 0.50. Below this, query is not marked sharp even if other signals fire (prevents misclassifying low-confidence matches). | `0.5` |
| `auto_temporal_k` | `integer` | No | k for temporal branch when auto_route=True. Default 15. Branch fires when query has temporal cues (when/since/before/after/dates). | `15` |
| `auto_temporal_recency_bias` | `number` | No | recency_bias for temporal branch. Default 0.05. Boosts recent memories over older ones (useful for 'recently'/'today' questions). | `0.05` |
| `auto_temporal_expand_sessions` | `boolean` | No | expand_sessions for temporal branch. Default True. Pulls full conversation context when a temporal hit is found (important for 'what happened after X' questions). | `True` |
| `auto_temporal_graph_depth` | `integer` | No | graph_depth for temporal branch (AUTO_v2 fix). Default 1. Traverses memory relationships to find cross-temporal references (e.g., follow-up discussions on an earlier event). | `1` |
| `auto_multi_k` | `integer` | No | k for multi_session branch when auto_route=True. Default 20. Branch fires on comparison queries (how many/count/total) or when hits scatter across multiple conversations. | `20` |
| `auto_multi_expand_sessions` | `boolean` | No | expand_sessions for multi_session branch. Default True. Pulls all turns from detected conversation IDs for aggregate comparisons ('list all X across conversations'). | `True` |
| `auto_sharp_threshold_ratio` | `number` | No | Trim hits below (top_score * ratio) in sharp branch. Default 0.85. Removes tail noise when there's a clear high-confidence cluster. | `0.85` |
| `auto_sharp_k_min` | `integer` | No | Floor for hit count after sharp threshold trim. Default 3. Ensures at least this many hits even if threshold trim is aggressive. | `3` |
| `auto_sharp_k_max` | `integer` | No | Ceiling for hit count after sharp threshold trim. Default 10. Caps result set when sharp curve is very steep. | `10` |
| `auto_entity_graph_enabled` | `boolean` | No | AUTO entity-anchored branch enable. Default True = branch fires when query has named entities AND auto_route=True. Caller can pass False to disable AUTO from enabling entity_graph (still works if caller passes entity_graph=True explicitly). | `True` |
| `auto_entity_graph_depth` | `integer` | No | Entity-graph traversal depth when AUTO entity_anchored branch fires. Default 1 = single-hop neighbors. Higher (2-3) traverses further but adds noise. | `1` |
| `auto_entity_graph_max_neighbors` | `integer` | No | Cap on entities expanded during AUTO entity-anchored traversal. Default 20. | `20` |
| `auto_entity_graph_named_entity_threshold` | `integer` | No | Minimum named-entity count in query for AUTO entity_anchored branch to fire. Default 1 = fire on any proper noun phrase (two+ capitalized words). Higher (2-3) is more conservative. | `1` |
| `entity_graph_valid_types` | `array` | No | Override list of allowed entity_type values for graph traversal. Default empty = use core defaults (person, place, organization, event, concept, object, date). Pass a list to filter traversal to specific types. | `[]` |
| `entity_graph_valid_predicates` | `array` | No | Override list of allowed predicate values for graph traversal. Default empty = use core defaults (works_at, located_in, before, after, same_as, contradicts, mentions, relates_to). Pass a list to filter traversal to specific predicates. | `[]` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_suggest`

Preview which memories would be retrieved for a query, with score breakdowns explaining why each was selected.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `query` | `string` | Yes | Search query. | `-` |
| `k` | `integer` | No | Max results to preview. | `5` |
| `variant` | `string` | No | Ingest-pipeline filter. Default '__none__' = real user data only. | `__none__` |
| `include_bench_data` | `boolean` | No | Opt in to bench rows. Default False. | `False` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_update`

Updates a MemoryItem by ID.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `id` | `string` | Yes | Memory item UUID. | `-` |
| `content` | `string` | No | New content (empty = no change). | `` |
| `title` | `string` | No | New title (empty = no change). | `` |
| `metadata` | `string` | No | JSON-encoded metadata (empty = no change). | `` |
| `importance` | `number` | No | New importance score (-1.0 = no change). | `-1.0` |
| `reembed` | `boolean` | No | Re-embed for semantic search. | `False` |
| `refresh_on` | `string` | No | New refresh timestamp. 'clear' removes the reminder; empty = no change. | `` |
| `refresh_reason` | `string` | No | New refresh reason. 'clear' removes; empty = no change. | `` |
| `conversation_id` | `string` | No | New conversation id. 'clear' removes; empty = no change. | `` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_verify`

Verify content integrity by comparing stored hash with computed hash. Returns OK if content hasn't been tampered with.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `id` | `string` | Yes | Memory item UUID. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_write`

Creates a MemoryItem and optionally embeds it for semantic search. Contradiction detection is automatic — if new content conflicts with an existing memory of the same type/title, the old one is superseded. Use type='auto' to let the LLM decide the best category.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `type` | `string` | Yes | Memory type. One of: auto, chat_log, code, config, conversation, decision, event_extraction, fact, fact_enriched, home, knowledge, log, message, note, observation, plan, preference, reference, scratchpad, snippet, summary, task, user_fact. | `-` |
| `content` | `string` | Yes | Memory body (max 50000 chars). | `-` |
| `title` | `string` | No | Short title. | `` |
| `metadata` | `string` | No | JSON-encoded metadata object. | `{}` |
| `agent_id` | `string` | No | Owning agent id. Injected by the orchestrator. | `` |
| `model_id` | `string` | No | Originating model id. | `` |
| `change_agent` | `string` | No | Agent causing the write (audit). | `` |
| `importance` | `number` | No | 0.0-1.0 relevance. | `0.5` |
| `source` | `string` | No | Provenance tag. | `agent` |
| `embed` | `boolean` | No | Embed for semantic search. | `True` |
| `user_id` | `string` | No | Data subject id. | `` |
| `scope` | `string` | No | Isolation scope. | `agent` |
| `valid_from` | `string` | No | ISO-8601 validity start. | `` |
| `valid_to` | `string` | No | ISO-8601 validity end. | `` |
| `auto_classify` | `boolean` | No | Let the LLM pick the type (forced true if type='auto'). | `False` |
| `conversation_id` | `string` | No | Groups this memory with a conversation / team session. Same ID space as conversation_start. | `` |
| `refresh_on` | `string` | No | ISO-8601 timestamp when this memory should be flagged for review (lifecycle / planned obsolescence). | `` |
| `refresh_reason` | `string` | No | Why this memory needs refreshing (e.g., 'quarterly policy review'). | `` |
| `variant` | `string` | No | Pipeline identifier for A/B variant tracking. | `` |
| `embed_text` | `string` | No | Override text used for embedding; falls back to content when empty. | `` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

---

## Knowledge Graph

### `enrich_pending`

Enrich pending memory items with SLM-distilled facts. Default dry_run=true reports count + ETA; pass dry_run=false to execute.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `dry_run` | `boolean` | No | If true, report count + ETA without executing; if false, execute enrichment. | `True` |
| `limit` | `integer` | No | Max items to enrich (0 = no limit). | `0` |
| `allowed_variants` | `array` | No | Variant names to include in enrichment (if empty, use default). | `[]` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `entity_get`

Load a single entity with its full neighborhood: predecessors, successors, and linked memory items.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `entity_id` | `string` | Yes | The entity ID to fetch. | `-` |
| `depth` | `integer` | No | Graph depth for neighborhood walk (currently unused; reserved for future multi-hop). | `1` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `entity_search`

Search entities by canonical_name and optionally by entity_type. Returns list of matching entities with optional neighbor counts.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `query` | `string` | No | Search term matched against canonical_name (LIKE %query%). | `` |
| `entity_type` | `string` | No | Filter by entity type (if provided). | `` |
| `limit` | `integer` | No | Max results to return. | `10` |
| `with_neighbors` | `boolean` | No | If true, compute neighbor_count for each entity. | `False` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `extract_pending`

Extract pending entities from the queue. Default dry_run=true reports count + ETA; pass dry_run=false to execute.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `dry_run` | `boolean` | No | If true, report count + ETA without executing; if false, execute extraction. | `True` |
| `limit` | `integer` | No | Max items to extract (0 = no limit). | `0` |
| `allowed_variants` | `array` | No | Variant names to include in extraction (if empty, use default). | `[]` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_graph`

Returns the local graph neighborhood of a memory item (connected memories up to N hops, max 3).

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `memory_id` | `string` | Yes | Memory item UUID. | `-` |
| `depth` | `integer` | No | Traversal depth (1-3). | `1` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_history`

Returns the change history (audit trail) for a memory item. Tracks create, update, delete, and supersede events.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `memory_id` | `string` | Yes | Memory item UUID. | `-` |
| `limit` | `integer` | No | Max history records. | `20` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_link`

Creates a directional link between two memory items. Valid types: related, supports, contradicts, extends, supersedes, references, consolidates, message, handoff.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `from_id` | `string` | Yes | Source memory UUID. | `-` |
| `to_id` | `string` | Yes | Target memory UUID. | `-` |
| `relationship_type` | `string` | No | Link type. | `related` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

---

## Conversations

### `conversation_append`

Appends a message to a conversation.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `conversation_id` | `string` | Yes | Conversation UUID. | `-` |
| `role` | `string` | Yes | Message role (e.g., 'user', 'assistant'). | `-` |
| `content` | `string` | Yes | Message body. | `-` |
| `agent_id` | `string` | No | Agent adding the message. | `` |
| `model_id` | `string` | No | Model that generated the message. | `` |
| `embed` | `boolean` | No | Embed for semantic search. | `True` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `conversation_search`

Search messages across conversations using hybrid semantic/keyword search.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `query` | `string` | Yes | Search query. | `-` |
| `k` | `integer` | No | Max results (1-100). | `8` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `conversation_start`

Starts a new conversation thread.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `title` | `string` | Yes | Conversation title. | `-` |
| `agent_id` | `string` | No | Owning agent id. | `` |
| `model_id` | `string` | No | Originating model id. | `` |
| `tags` | `string` | No | Comma-separated tags. | `` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `conversation_summarize`

Summarize a conversation into key points using the local LLM.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `conversation_id` | `string` | Yes | Conversation UUID. | `-` |
| `threshold` | `integer` | No | Min message count to summarize. | `20` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

---

## Task Management

### `task_assign`

Assign a task to an owner. Sets state=in_progress and notifies the new owner.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `task_id` | `string` | Yes | Task UUID. | `-` |
| `owner_agent` | `string` | Yes | Agent to assign to. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `task_create`

Create a new task in 'pending' state. Returns task id.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `title` | `string` | Yes | Task title. | `-` |
| `created_by` | `string` | Yes | Agent or user that created the task. | `-` |
| `description` | `string` | No | Longer description. | `` |
| `owner_agent` | `string` | No | Initial owner (blank = unassigned). | `` |
| `parent_task_id` | `string` | No | Optional parent task id for sub-tasks. | `` |
| `metadata` | `object` | No | Free-form metadata. | `{}` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `task_delete`

Delete a task. Soft-delete (default) sets a tombstone that propagates via pg_sync to the warehouse and peers. Hard-delete removes the row locally and requires a prior soft-delete.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `task_id` | `string` | Yes | Task UUID. | `-` |
| `hard` | `boolean` | No | If true, permanently remove an already-tombstoned row from local SQLite. | `False` |
| `actor` | `string` | No | Actor performing the delete (audit log). | `` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `task_get`

Get full record for one task.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `task_id` | `string` | Yes | Task UUID. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `task_list`

List tasks with optional filters. Newest updated first.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `owner_agent` | `string` | No | Filter by owner agent. | `` |
| `state` | `string` | No | Filter by task state. | `` |
| `parent_task_id` | `string` | No | Filter by parent task id. | `` |
| `limit` | `integer` | No | Max tasks to return. | `50` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `task_set_result`

Set the result memory pointer for a task. Does NOT change state.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `task_id` | `string` | Yes | Task UUID. | `-` |
| `result_memory_id` | `string` | Yes | Result memory UUID. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `task_tree`

Render a recursive subtree of tasks rooted at root_task_id.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `root_task_id` | `string` | Yes | Root task UUID. | `-` |
| `max_depth` | `integer` | No | Max recursion depth. | `3` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `task_update`

Partial update for a task. Validates state transitions. On terminal state, sets completed_at.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `task_id` | `string` | Yes | Task UUID. | `-` |
| `state` | `string` | No | New state (empty = no change). | `` |
| `description` | `string` | No | New description (empty = no change). | `` |
| `metadata` | `object` | No | New metadata (empty = no change). | `{}` |
| `actor` | `string` | No | Actor making the update. | `` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

---

## Agent Registry & Notifications

### `agent_get`

Get full record for one registered agent.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `agent_id` | `string` | Yes | Unique agent identifier. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `agent_heartbeat`

Update last_seen and set status=active. Errors if not registered.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `agent_id` | `string` | Yes | Unique agent identifier. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `agent_list`

List registered agents, optionally filtered by status and/or role.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `status` | `string` | No | Filter by agent status. | `` |
| `role` | `string` | No | Filter by agent role. | `` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `agent_offline`

Mark an agent as offline.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `agent_id` | `string` | Yes | Unique agent identifier. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `agent_register`

Register an agent (UPSERT). Sets status=active, last_seen=now.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `agent_id` | `string` | Yes | Unique agent identifier. | `-` |
| `role` | `string` | No | Agent role or function. | `` |
| `capabilities` | `array` | No | List of capabilities. | `[]` |
| `metadata` | `object` | No | Free-form metadata. | `{}` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `notifications_ack`

Mark one notification as read.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `notification_id` | `integer` | Yes | Notification ID. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `notifications_ack_all`

Bulk-ack all unread notifications for an agent. Returns count acked.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `agent_id` | `string` | Yes | Agent id. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `notifications_poll`

List notifications addressed to agent_id, newest first.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `agent_id` | `string` | Yes | Recipient agent id. | `-` |
| `unread_only` | `boolean` | No | Show only unread notifications. | `True` |
| `limit` | `integer` | No | Max notifications to return. | `20` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `notify`

Send a notification to an agent. Lightweight wake signal — agents poll notifications_poll.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `agent_id` | `string` | Yes | Recipient agent id. | `-` |
| `kind` | `string` | Yes | Notification kind/type. | `-` |
| `payload` | `object` | No | Free-form notification data. | `{}` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

---

## Multi-Agent Coordination

### `memory_handoff`

Hand off a task from one agent to another. Writes a new handoff-type memory owned by to_agent and links it to the given context memories with 'handoff' edges. Returns a confirmation string with the new memory id.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `from_agent` | `string` | Yes | Sending agent id. | `-` |
| `to_agent` | `string` | Yes | Receiving agent id. | `-` |
| `task` | `string` | Yes | What the receiver should do. | `-` |
| `context_ids` | `array` | No | Memory ids to link via 'handoff' edges. | `[]` |
| `note` | `string` | No | Optional free-text note. | `` |
| `task_id` | `string` | No | Optional tracked task id. | `` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_inbox`

List handoff messages addressed to agent_id, newest first. Pass unread_only=False to include already-acked items.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `agent_id` | `string` | Yes | Receiving agent id. | `-` |
| `unread_only` | `boolean` | No | Show only unread messages. | `True` |
| `limit` | `integer` | No | Max messages to return. | `20` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_inbox_ack`

Mark a handoff memory as read (sets read_at = now).

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `memory_id` | `string` | Yes | Handoff memory UUID. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_refresh_queue`

List memories whose refresh_on timestamp has arrived and need review. Read-only — to actually refresh a memory, call memory_update with new content/refresh_on. Pass include_future=True to see all memories with refresh_on set, not just overdue ones.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `agent_id` | `string` | No | Restrict to memories owned by this agent. | `` |
| `limit` | `integer` | No | Max rows to return (1-500). | `50` |
| `include_future` | `boolean` | No | Include memories whose refresh_on is still in the future. | `False` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

---

## Chat Log System

### `chatlog_cost_report`

Aggregate tokens and cost_usd across chat_log rows. Groups: provider|model_id|host_agent|conversation_id|day.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `since` | `string` | No | ISO-8601 lower bound. | `` |
| `until` | `string` | No | ISO-8601 upper bound. | `` |
| `group_by` | `string` | No | provider\|model_id\|host_agent\|conversation_id\|day | `model_id` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `chatlog_list_conversations`

List distinct conversation_ids with turn counts and timespans.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `host_agent` | `string` | No | Filter by host agent. | `` |
| `limit` | `integer` | No | Max conversations. | `50` |
| `offset` | `integer` | No | Pagination offset. | `0` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `chatlog_promote`

Promote chat_log rows into the main memory DB under a new type (default 'conversation'). ATTACH + INSERT SELECT in separate/hybrid; UPDATE type in integrated.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `ids` | `array` | No | Specific row ids to promote. | `-` |
| `conversation_id` | `string` | No | Promote all rows in a conversation. | `` |
| `since` | `string` | No | Promote rows at-or-after this ISO-8601. | `` |
| `until` | `string` | No | Promote rows at-or-before this ISO-8601. | `` |
| `copy` | `boolean` | No | If false, delete source rows after copy. | `True` |
| `target_type` | `string` | No | Type assigned in main DB. | `conversation` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `chatlog_rescrub`

Re-apply redaction to existing chat_log rows. Requires redaction.enabled=true.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `conversation_id` | `string` | No | Filter by conversation. | `` |
| `since` | `string` | No | ISO-8601 lower bound. | `` |
| `until` | `string` | No | ISO-8601 upper bound. | `` |
| `limit` | `integer` | No | Max rows to process. | `10000` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `chatlog_search`

Search chat_log rows. FTS5 keyword when query is non-empty; filter-only when empty.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `query` | `string` | Yes | FTS5 query; empty → filter-only listing. | `-` |
| `k` | `integer` | No | Max results. | `8` |
| `conversation_id` | `string` | No | Filter by conversation. | `` |
| `host_agent` | `string` | No | Filter by host agent. | `` |
| `provider` | `string` | No | Filter by provider. | `` |
| `model_id` | `string` | No | Filter by model id. | `` |
| `agent_id` | `string` | No | Filter by agent id. | `` |
| `search_mode` | `string` | No | hybrid\|fts\|vector (integrated mode only). | `hybrid` |
| `since` | `string` | No | ISO-8601 lower bound on created_at. | `` |
| `until` | `string` | No | ISO-8601 upper bound on created_at. | `` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `chatlog_set_redaction`

Flip redaction on/off and update patterns. Persists to memory/.chatlog_config.json.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `enabled` | `boolean` | Yes | Turn redaction on/off. | `-` |
| `patterns` | `array` | No | Enabled pattern groups. | `-` |
| `redact_pii` | `boolean` | No | Also redact PII (email/phone/SSN). | `-` |
| `custom_regex` | `array` | No | User-supplied regex patterns. | `-` |
| `store_original_hash` | `boolean` | No | Store SHA-256 of pre-scrub content in metadata. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `chatlog_status`

One-call health summary of the chat log subsystem: mode, DB paths, row counts, queue depth, spill files, embed backlog, hook timestamps, redaction state, warnings.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `chatlog_write`

Append one chat turn to the chat log DB. Provenance (host_agent, provider, model_id, conversation_id) is required. Writes are async-queued — returns the row id immediately.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `content` | `string` | Yes | Message text. | `-` |
| `role` | `string` | Yes | user\|assistant\|system\|tool | `-` |
| `conversation_id` | `string` | Yes | Session/conversation UUID. | `-` |
| `host_agent` | `string` | Yes | Client: claude-code\|gemini-cli\|opencode\|aider | `-` |
| `provider` | `string` | Yes | Model provider: anthropic\|google\|openai\|local\|xai\|deepseek\|mistral\|meta\|other | `-` |
| `model_id` | `string` | Yes | Exact model id, e.g. claude-opus-4-7 | `-` |
| `turn_index` | `integer` | No | 0-based turn index within conversation. | `-` |
| `agent_id` | `string` | No | Client agent id (host:user@machine). | `` |
| `user_id` | `string` | No | Owning user id. | `` |
| `metadata` | `string` | No | Extra metadata JSON string. | `{}` |
| `tokens_in` | `integer` | No | Prompt tokens (null if unknown). | `-` |
| `tokens_out` | `integer` | No | Completion tokens (null if unknown). | `-` |
| `cost_usd` | `number` | No | Cost in USD (null → computed from price table). | `-` |
| `latency_ms` | `integer` | No | End-to-end request latency. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `chatlog_write_bulk`

Bulk-append N chat turns. Each item needs the same required fields as chatlog_write.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `items` | `array` | Yes | List of chat-turn dicts. | `-` |
| `embed` | `boolean` | No | Reserved; ignored — sweeper handles. | `False` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

---

## Operational Protocol

### `check_thermal_load`

Protocol #2 - Check M3 Max thermal/RAM pressure. Returns Nominal|Fair|Serious|Critical.

**Source:** mcp_proxy.py (PROTOCOL_TOOLS)

No parameters.

### `log_activity`

Archive activity to the agent log (Protocols #1-#3). category=thought for complex reasoning, hardware after thermal check, decision when user agrees to any code change, file move, or direction.

**Source:** mcp_proxy.py (PROTOCOL_TOOLS)

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `category` | `string` | Yes |  | `-` |
| `detail_a` | `string` | Yes | Primary detail (<=500 chars) | `-` |
| `detail_b` | `string` | No | Secondary detail (<=2000 chars) | `-` |
| `detail_c` | `string` | No | Tertiary detail / root cause | `-` |

### `query_decisions`

Protocol #4 - MUST call before starting any new task. Full-text search across project_decisions table for prior decisions.

**Source:** mcp_proxy.py (PROTOCOL_TOOLS)

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `keyword` | `string` | Yes | Topic keywords for the task | `-` |
| `limit` | `integer` | No | Max results | `10` |

### `retire_focus`

Protocol #5 - Clear dashboard focus when a task completes.

**Source:** mcp_proxy.py (PROTOCOL_TOOLS)

No parameters.

### `update_focus`

Protocol #5 - Call every 3 turns with a <=10-word trajectory summary.

**Source:** mcp_proxy.py (PROTOCOL_TOOLS)

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `summary` | `string` | Yes | <=10-word current trajectory | `-` |

---

## Debug Agent

### `debug_analyze`

Root cause analysis with memory-augmented reasoning. Searches past issues, reads source, uses local LLM to diagnose.

**Source:** mcp_proxy.py (DEBUG_TOOLS)

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `error_message` | `string` | Yes | The error message or symptom to analyze | `-` |
| `context` | `string` | No | Additional context (stack trace, repro steps) | `-` |
| `file_path` | `string` | No | Source file path for context | `-` |

### `debug_bisect`

Automated git bisect with LLM analysis of the offending commit.

**Source:** mcp_proxy.py (DEBUG_TOOLS)

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `test_command` | `string` | Yes | Shell command that exits 0 on success | `-` |
| `good_commit` | `string` | No | Known-good commit hash or ref | `-` |
| `bad_commit` | `string` | No | Known-bad commit | `HEAD` |

### `debug_correlate`

Cross-reference logs, git commits, and decisions to build a causal timeline.

**Source:** mcp_proxy.py (DEBUG_TOOLS)

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `log_file` | `string` | No | Log file path to parse | `-` |
| `time_range` | `string` | No | Time window (e.g. 1h, 24h, 7d) | `24h` |
| `pattern` | `string` | No | Regex pattern to filter log entries | `-` |

### `debug_history`

Search past debugging sessions and patterns. No LLM required.

**Source:** mcp_proxy.py (DEBUG_TOOLS)

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `keyword` | `string` | No | Search term | `-` |
| `limit` | `integer` | No | Max results | `10` |

### `debug_report`

Generate and persist a structured debugging report to memory.

**Source:** mcp_proxy.py (DEBUG_TOOLS)

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `issue_id` | `string` | No | Issue/ticket ID | `-` |
| `title` | `string` | Yes | Report title (required) | `-` |
| `findings` | `string` | No | Debugging findings and resolution | `-` |

### `debug_trace`

Execution flow analysis - reads source, finds callers, identifies failure points.

**Source:** mcp_proxy.py (DEBUG_TOOLS)

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `file_path` | `string` | Yes | Path to the source file | `-` |
| `function_name` | `string` | No | Function to focus on | `-` |
| `error_type` | `string` | No | Error type to look for | `-` |

---

## Lifecycle & Maintenance

### `memory_consolidate`

Consolidate old memories of the same type into summaries using the local LLM. Reduces clutter while preserving knowledge.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `type_filter` | `string` | No | Restrict to a memory type. | `` |
| `agent_filter` | `string` | No | Restrict to an agent id. | `` |
| `threshold` | `integer` | No | Min items to consolidate. | `20` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_dedup`

Find and merge near-duplicate memory items.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `threshold` | `number` | No | Similarity threshold (0-1). | `0.92` |
| `dry_run` | `boolean` | No | Preview without applying. | `True` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_maintenance`

Runs maintenance tasks on the memory store.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `decay` | `boolean` | No | Apply importance decay. | `True` |
| `purge_expired` | `boolean` | No | Delete expired items. | `True` |
| `prune_orphan_embeddings` | `boolean` | No | Remove orphaned embeddings. | `True` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_set_retention`

Set or update per-agent memory retention policy. Controls max memory count, TTL expiry, and auto-archival.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `agent_id` | `string` | Yes | Agent id for policy. | `-` |
| `max_memories` | `integer` | No | Max items to retain. | `1000` |
| `ttl_days` | `integer` | No | Time-to-live in days (0 = no limit). | `0` |
| `auto_archive` | `integer` | No | Auto-archive threshold. | `1` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

---

## Data Governance

### `gdpr_export`

Export all memories for a data subject (GDPR data portability). Returns JSON with all memory items for the given user_id.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `user_id` | `string` | Yes | Data subject id. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `gdpr_forget`

Right to be forgotten — hard-deletes ALL data for a user_id including memories, embeddings, relationships, and history.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `user_id` | `string` | Yes | Data subject id to forget. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_export`

Export memories as portable JSON. Filter by agent, type, or date.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `agent_filter` | `string` | No | Restrict to an agent id. | `` |
| `type_filter` | `string` | No | Restrict to a memory type. | `` |
| `since` | `string` | No | ISO-8601 start date. | `` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_import`

Import memories from a JSON export. UPSERT semantics — safe to re-run.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `data` | `string` | Yes | JSON export string. | `-` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

---

## Infrastructure Operations

### `chroma_sync`

Bi-directional sync between local SQLite and ChromaDB.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `max_items` | `integer` | No | Max items per batch. | `50` |
| `direction` | `string` | No | Sync direction. | `both` |
| `reset_stalled` | `boolean` | No | Reset stalled sync records. | `True` |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

### `memory_cost_report`

Returns current session operation counts and estimated token usage for memory operations.

**Source:** mcp_tool_catalog.py

**Parameters:**

| Parameter | Type | Required | Description | Default |
| --- | --- | --- | --- | --- |
| `database` | `string` | No | Optional SQLite database path. Overrides M3_DATABASE env and the default memory/agent_memory.db for this call only. Empty = use default. | `` |

---

