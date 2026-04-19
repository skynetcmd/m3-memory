---
tool: bin/test_memory_bridge.py
sha1: 248a578537c8
mtime_utc: 2026-04-18T22:29:31.727456+00:00
generated_utc: 2026-04-19T00:39:16.147009+00:00
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

- `async def run()` (line 126)
- `async def main()` (line 1338)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `ORIGIN_DEVICE`

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `memory_bridge (VALID_MEMORY_TYPES, _content_hash, _ensure_sync_tables, _pack, agent_get, agent_heartbeat, agent_list, agent_offline, agent_register, chroma_sync, conversation_append, conversation_messages, conversation_search, conversation_start, conversation_summarize, gdpr_export, gdpr_forget, memory_consolidate, memory_cost_report, memory_delete, memory_export, memory_get, memory_graph, memory_handoff, memory_history, memory_import, memory_inbox, memory_inbox_ack, memory_link, memory_maintenance, memory_search, memory_set_retention, memory_suggest, memory_update, memory_verify, memory_write, notifications_ack, notifications_ack_all, notifications_poll, notify, sync_status, task_assign, task_create, task_get, task_list, task_set_result, task_tree, task_update)`
- `memory_core`

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 56)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 1081)
- `sqlite3.connect()  → `DB_PATH`` (line 1125)
- `sqlite3.connect()  → `DB_PATH`` (line 1161)
- `sqlite3.connect()  → `DB_PATH`` (line 1224)
- `sqlite3.connect()  → `DB_PATH`` (line 1242)
- `sqlite3.connect()  → `DB_PATH`` (line 1251)
- `sqlite3.connect()  → `DB_PATH`` (line 1312)
- `sqlite3.connect()  → `DB_PATH`` (line 196)
- `sqlite3.connect()  → `DB_PATH`` (line 238)
- `sqlite3.connect()  → `DB_PATH`` (line 245)
- `sqlite3.connect()  → `DB_PATH`` (line 418)
- `sqlite3.connect()  → `DB_PATH`` (line 429)
- `sqlite3.connect()  → `DB_PATH`` (line 477)
- `sqlite3.connect()  → `DB_PATH`` (line 497)
- `sqlite3.connect()  → `DB_PATH`` (line 510)
- `sqlite3.connect()  → `DB_PATH`` (line 519)
- `sqlite3.connect()  → `DB_PATH`` (line 549)
- `sqlite3.connect()  → `DB_PATH`` (line 565)
- `sqlite3.connect()  → `DB_PATH`` (line 578)
- `sqlite3.connect()  → `DB_PATH`` (line 598)
- `sqlite3.connect()  → `DB_PATH`` (line 615)
- `sqlite3.connect()  → `DB_PATH`` (line 735)
- `sqlite3.connect()  → `DB_PATH`` (line 778)
- `sqlite3.connect()  → `DB_PATH`` (line 787)
- `sqlite3.connect()  → `DB_PATH`` (line 808)
- `sqlite3.connect()  → `DB_PATH`` (line 828)
- `sqlite3.connect()  → `DB_PATH`` (line 88)
- `sqlite3.connect()  → `DB_PATH`` (line 880)
- `sqlite3.connect()  → `DB_PATH`` (line 913)
- `sqlite3.connect()  → `DB_PATH`` (line 951)
- `sqlite3.connect()  → `DB_PATH`` (line 978)
- `sqlite3.connect()  → `DB_PATH`` (line 99)


## Notable external imports

- `httpx`
- `importlib`
- `platform`

## File dependencies (repo paths referenced)

- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
