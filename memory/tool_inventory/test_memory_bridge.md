---
tool: bin/test_memory_bridge.py
sha1: 5eb51e5dfca1
mtime_utc: 2026-04-21T20:59:33.529255+00:00
generated_utc: 2026-04-21T21:22:27.240852+00:00
private: false
---

# bin/test_memory_bridge.py

## Purpose

End-to-end test suite for memory_bridge.py.

Tests all 38 MCP tools (including agent registry, notifications, task orchestration,
memory_history, memory_link, memory_graph, memory_verify, memory_set_retention,
gdpr_export, gdpr_forget, memory_cost_report, memory_handoff, memory_inbox, memory_inbox_ack).
Embedding-dependent tests are attempted and gracefully skipped when an
embedding model is not loaded in LM Studio.

## Entry points

- `async def run()` (line 134)
- `async def main()` (line 1346)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `ORIGIN_DEVICE`

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `m3_sdk (resolve_db_path)`
- `memory_bridge (VALID_MEMORY_TYPES, _content_hash, _ensure_sync_tables, _pack, agent_get, agent_heartbeat, agent_list, agent_offline, agent_register, chroma_sync, conversation_append, conversation_messages, conversation_search, conversation_start, conversation_summarize, gdpr_export, gdpr_forget, memory_consolidate, memory_cost_report, memory_delete, memory_export, memory_get, memory_graph, memory_handoff, memory_history, memory_import, memory_inbox, memory_inbox_ack, memory_link, memory_maintenance, memory_search, memory_set_retention, memory_suggest, memory_update, memory_verify, memory_write, notifications_ack, notifications_ack_all, notifications_poll, notify, sync_status, task_assign, task_create, task_get, task_list, task_set_result, task_tree, task_update)`
- `memory_core`

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 64)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 107)
- `sqlite3.connect()  → `DB_PATH`` (line 1089)
- `sqlite3.connect()  → `DB_PATH`` (line 1133)
- `sqlite3.connect()  → `DB_PATH`` (line 1169)
- `sqlite3.connect()  → `DB_PATH`` (line 1232)
- `sqlite3.connect()  → `DB_PATH`` (line 1250)
- `sqlite3.connect()  → `DB_PATH`` (line 1259)
- `sqlite3.connect()  → `DB_PATH`` (line 1320)
- `sqlite3.connect()  → `DB_PATH`` (line 204)
- `sqlite3.connect()  → `DB_PATH`` (line 246)
- `sqlite3.connect()  → `DB_PATH`` (line 253)
- `sqlite3.connect()  → `DB_PATH`` (line 426)
- `sqlite3.connect()  → `DB_PATH`` (line 437)
- `sqlite3.connect()  → `DB_PATH`` (line 485)
- `sqlite3.connect()  → `DB_PATH`` (line 505)
- `sqlite3.connect()  → `DB_PATH`` (line 518)
- `sqlite3.connect()  → `DB_PATH`` (line 527)
- `sqlite3.connect()  → `DB_PATH`` (line 557)
- `sqlite3.connect()  → `DB_PATH`` (line 573)
- `sqlite3.connect()  → `DB_PATH`` (line 586)
- `sqlite3.connect()  → `DB_PATH`` (line 606)
- `sqlite3.connect()  → `DB_PATH`` (line 623)
- `sqlite3.connect()  → `DB_PATH`` (line 743)
- `sqlite3.connect()  → `DB_PATH`` (line 786)
- `sqlite3.connect()  → `DB_PATH`` (line 795)
- `sqlite3.connect()  → `DB_PATH`` (line 816)
- `sqlite3.connect()  → `DB_PATH`` (line 836)
- `sqlite3.connect()  → `DB_PATH`` (line 888)
- `sqlite3.connect()  → `DB_PATH`` (line 921)
- `sqlite3.connect()  → `DB_PATH`` (line 959)
- `sqlite3.connect()  → `DB_PATH`` (line 96)
- `sqlite3.connect()  → `DB_PATH`` (line 986)


## Notable external imports

- `httpx`
- `importlib`
- `platform`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
