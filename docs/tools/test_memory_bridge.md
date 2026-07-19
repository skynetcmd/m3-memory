---
tool: bin/test_memory_bridge.py
sha1: e6db6c0fbbd0
mtime_utc: 2026-07-19T03:04:59.640251+00:00
generated_utc: 2026-07-19T19:29:23.002479+00:00
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
- `async def main()` (line 1179)
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
- `memory_bridge (VALID_MEMORY_TYPES, _content_hash, _ensure_sync_tables, agent_get, agent_heartbeat, agent_list, agent_offline, agent_register, conversation_append, conversation_messages, conversation_search, conversation_start, conversation_summarize, gdpr_export, gdpr_forget, memory_consolidate, memory_cost_report, memory_delete, memory_export, memory_get, memory_graph, memory_handoff, memory_history, memory_import, memory_inbox, memory_inbox_ack, memory_link, memory_maintenance, memory_search, memory_set_retention, memory_suggest, memory_update, memory_verify, memory_write, notifications_ack, notifications_ack_all, notifications_poll, notify, task_assign, task_create, task_get, task_list, task_set_result, task_tree, task_update)`
- `memory_core`

---

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 66)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 1002)
- `sqlite3.connect()  → `DB_PATH`` (line 1065)
- `sqlite3.connect()  → `DB_PATH`` (line 108)
- `sqlite3.connect()  → `DB_PATH`` (line 1083)
- `sqlite3.connect()  → `DB_PATH`` (line 1092)
- `sqlite3.connect()  → `DB_PATH`` (line 1153)
- `sqlite3.connect()  → `DB_PATH`` (line 201)
- `sqlite3.connect()  → `DB_PATH`` (line 243)
- `sqlite3.connect()  → `DB_PATH`` (line 250)
- `sqlite3.connect()  → `DB_PATH`` (line 439)
- `sqlite3.connect()  → `DB_PATH`` (line 456)
- `sqlite3.connect()  → `DB_PATH`` (line 576)
- `sqlite3.connect()  → `DB_PATH`` (line 619)
- `sqlite3.connect()  → `DB_PATH`` (line 628)
- `sqlite3.connect()  → `DB_PATH`` (line 649)
- `sqlite3.connect()  → `DB_PATH`` (line 669)
- `sqlite3.connect()  → `DB_PATH`` (line 721)
- `sqlite3.connect()  → `DB_PATH`` (line 754)
- `sqlite3.connect()  → `DB_PATH`` (line 792)
- `sqlite3.connect()  → `DB_PATH`` (line 819)
- `sqlite3.connect()  → `DB_PATH`` (line 922)
- `sqlite3.connect()  → `DB_PATH`` (line 966)
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
