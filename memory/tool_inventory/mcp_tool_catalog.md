---
tool: bin/mcp_tool_catalog.py
sha1: 7b244c1c8665
mtime_utc: 2026-04-18T04:42:38.466230+00:00
generated_utc: 2026-04-18T05:16:53.143140+00:00
private: false
---

# bin/mcp_tool_catalog.py

## Purpose

Single source of truth for the m3-memory MCP tool catalog. Defines `ToolSpec` dataclass and `TOOLS` list used by FastMCP stdio server to register tools and by orchestrators to dispatch calls. Provides validation, helpers, and inline implementation wrappers.

Imported by:
  - `bin/memory_bridge.py` — FastMCP stdio server registers each spec via `@mcp.tool()`
  - `examples/multi-agent-team/dispatch.py` — orchestrator-side dispatch loop

Zero FastMCP dependency. Pure Python + `memory_core` + `memory_sync` + `memory_maintenance`.

## Entry points / Public API

- **`ToolSpec`** dataclass (line 34) — frozen dataclass with fields: `name`, `description`, `parameters` (dict), `impl` (Callable), `is_async` (bool), `validators` (tuple), `default_allowed` (bool), `inject_agent_id` (bool)
- **`TOOLS`** list (line 210) — list[ToolSpec] containing all 44 tool definitions
- **`_BY_NAME`** dict (line 1060) — lookup map {name → ToolSpec}
- **`get_tool(name: str)`** — retrieves ToolSpec by name
- **`default_allowlist()`** — returns set of tool names allowed by default
- **`validate_args(spec, args)`** — runs validators and returns (args, error_msg)
- **`execute_tool(spec, args, agent_id)`** — async dispatcher that validates, injects agent_id, and calls impl

## Validators (line 45–100)

- `_memory_write_validator` — validates type in VALID_MEMORY_TYPES, content size ≤50KB, JSON metadata
- `_memory_search_validator` — truncates query to 2000 chars, clamps k to [1, 100], default 8
- `_memory_update_validator` — JSON-encodes dict metadata
- `_memory_set_retention_validator` — int coercion for max_memories, ttl_days, auto_archive
- `_gdpr_user_id_validator` — requires non-empty user_id

## Tool count

- **Total tools defined:** 44
- **Destructive tools (default_allowed=False):** 8
  - `memory_delete`, `chroma_sync`, `memory_maintenance`, `memory_set_retention`, `memory_export`, `memory_import`, `gdpr_export`, `gdpr_forget`

## Tool categories

**Memory (read/write/search):** memory_write, memory_search, memory_suggest, memory_get, memory_update, memory_delete, memory_verify, memory_feedback, memory_history, memory_link, memory_graph, memory_refresh_queue

## memory_write schema (Phase 2)

The `memory_write` tool now exposes parameters for Phase 1 optimizations (line ~240–241):

| Parameter | Type | Default | Description |
|---|---|---|---|
| `type` | string | (required) | Memory type: auto, code, config, conversation, decision, fact, etc. |
| `content` | string | (required) | Main body content (≤50KB). |
| `variant` | string | `""` | Pipeline identifier for A/B variant tracking. |
| `embed_text` | string | `""` | Override text used for embedding; falls back to content when empty. |
| `auto_classify` | boolean | `False` | Let the LLM pick the type (forced true if type='auto'). |

**Known gap:** No `memory_write_bulk` tool schema exists in the catalog. Bulk ingestion is available as a library function (`memory_write_bulk_impl`) called by benchmarks but is not exposed as an MCP tool.

**Conversation:** conversation_start, conversation_append, conversation_search, conversation_summarize

**Maintenance/Lifecycle:** chroma_sync, memory_maintenance, memory_consolidate, memory_dedup, memory_set_retention, memory_export, memory_import, memory_cost_report

**GDPR:** gdpr_export, gdpr_forget

**Handoff/Inbox:** memory_handoff, memory_inbox, memory_inbox_ack

**Agent lifecycle:** agent_register, agent_heartbeat, agent_list, agent_get, agent_offline

**Notifications:** notify, notifications_poll, notifications_ack, notifications_ack_all

**Task management:** task_create, task_assign, task_update, task_delete, task_set_result, task_get, task_list, task_tree

## CLI flags / arguments

_(no CLI surface — invoked as a library/module by memory_bridge and orchestrators)_

## Environment variables read

_(none)_

## Calls INTO this repo (intra-repo imports)

- `memory_core` — all core memory/agent/task/notification functions
- `memory_sync` — `chroma_sync_impl`
- `memory_maintenance` — consolidate, dedup, feedback, retention, export/import, GDPR, cost_report

## Calls OUT (external side-channels)

- **Inline impl:** `_conversation_search_impl` (line 136) — pulls from `memory_core.memory_search_scored_impl` with type_filter="message", performs adjacent-turn pairing via direct SQLite query
- **Inline impl:** `_memory_verify_impl` (line 206) — wraps `memory_core.memory_verify_impl(id)`

## File dependencies

_(none — pure Python with imports only)_

## Validation constants (hoisted, line 22–31)

- `MAX_CONTENT_SIZE` = 50,000 bytes
- `MAX_QUERY_LENGTH` = 2,000 chars
- `MAX_K` = 100 results
- `VALID_MEMORY_TYPES` = frozenset of 19 types: auto, code, config, conversation, decision, event_extraction, fact, home, knowledge, log, message, note, observation, plan, preference, reference, scratchpad, snippet, summary, task, user_fact

## Re-validation

If `sha1` above differs from the current file's sha1, the inventory is stale. Re-read `bin/mcp_tool_catalog.py`, confirm tool count and validator logic still match, and regenerate via `python bin/gen_tool_inventory.py`.
