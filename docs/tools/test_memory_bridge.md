---
tool: bin/test_memory_bridge.py
sha1: 7cfa183c3dee
mtime_utc: 2026-07-17T02:19:16.069260+00:00
generated_utc: 2026-07-17T02:39:19.830046+00:00
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

---

## Entry points

- `async def run()` (line 134)
- `async def main()` (line 1180)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `COMPUTERNAME`
- `HOSTNAME`
- `SEARCH_ROW_CAP`

---

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `m3_sdk (getenv_compat)`
- `m3_sdk (resolve_db_path)`
- `memory_bridge (VALID_MEMORY_TYPES, _content_hash, _ensure_sync_tables, _pack, agent_get, agent_heartbeat, agent_list, agent_offline, agent_register, conversation_append, conversation_messages, conversation_search, conversation_start, conversation_summarize, gdpr_export, gdpr_forget, memory_consolidate, memory_cost_report, memory_delete, memory_export, memory_get, memory_graph, memory_handoff, memory_history, memory_import, memory_inbox, memory_inbox_ack, memory_link, memory_maintenance, memory_search, memory_set_retention, memory_suggest, memory_update, memory_verify, memory_write, notifications_ack, notifications_ack_all, notifications_poll, notify, task_assign, task_create, task_get, task_list, task_set_result, task_tree, task_update)`
- `memory_core`

---

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 66)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 1003)
- `sqlite3.connect()  → `DB_PATH`` (line 1066)
- `sqlite3.connect()  → `DB_PATH`` (line 108)
- `sqlite3.connect()  → `DB_PATH`` (line 1084)
- `sqlite3.connect()  → `DB_PATH`` (line 1093)
- `sqlite3.connect()  → `DB_PATH`` (line 1154)
- `sqlite3.connect()  → `DB_PATH`` (line 202)
- `sqlite3.connect()  → `DB_PATH`` (line 244)
- `sqlite3.connect()  → `DB_PATH`` (line 251)
- `sqlite3.connect()  → `DB_PATH`` (line 440)
- `sqlite3.connect()  → `DB_PATH`` (line 457)
- `sqlite3.connect()  → `DB_PATH`` (line 577)
- `sqlite3.connect()  → `DB_PATH`` (line 620)
- `sqlite3.connect()  → `DB_PATH`` (line 629)
- `sqlite3.connect()  → `DB_PATH`` (line 650)
- `sqlite3.connect()  → `DB_PATH`` (line 670)
- `sqlite3.connect()  → `DB_PATH`` (line 722)
- `sqlite3.connect()  → `DB_PATH`` (line 755)
- `sqlite3.connect()  → `DB_PATH`` (line 793)
- `sqlite3.connect()  → `DB_PATH`` (line 820)
- `sqlite3.connect()  → `DB_PATH`` (line 923)
- `sqlite3.connect()  → `DB_PATH`` (line 967)
- `sqlite3.connect()  → `DB_PATH`` (line 97)


---

## Notable external imports

- `httpx`
- `importlib`
- `platform`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
