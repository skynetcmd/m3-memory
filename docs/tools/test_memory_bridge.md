---
tool: bin/test_memory_bridge.py
sha1: 68fb79a5c131
mtime_utc: 2026-07-02T21:51:11.656462+00:00
generated_utc: 2026-07-03T20:00:04.046297+00:00
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

- `async def run()` (line 135)
- `async def main()` (line 1347)
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
- `memory_bridge (VALID_MEMORY_TYPES, _content_hash, _ensure_sync_tables, _pack, agent_get, agent_heartbeat, agent_list, agent_offline, agent_register, chroma_sync, conversation_append, conversation_messages, conversation_search, conversation_start, conversation_summarize, gdpr_export, gdpr_forget, memory_consolidate, memory_cost_report, memory_delete, memory_export, memory_get, memory_graph, memory_handoff, memory_history, memory_import, memory_inbox, memory_inbox_ack, memory_link, memory_maintenance, memory_search, memory_set_retention, memory_suggest, memory_update, memory_verify, memory_write, notifications_ack, notifications_ack_all, notifications_poll, notify, sync_status, task_assign, task_create, task_get, task_list, task_set_result, task_tree, task_update)`
- `memory_core`

---

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 65)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 108)
- `sqlite3.connect()  → `DB_PATH`` (line 1090)
- `sqlite3.connect()  → `DB_PATH`` (line 1134)
- `sqlite3.connect()  → `DB_PATH`` (line 1170)
- `sqlite3.connect()  → `DB_PATH`` (line 1233)
- `sqlite3.connect()  → `DB_PATH`` (line 1251)
- `sqlite3.connect()  → `DB_PATH`` (line 1260)
- `sqlite3.connect()  → `DB_PATH`` (line 1321)
- `sqlite3.connect()  → `DB_PATH`` (line 205)
- `sqlite3.connect()  → `DB_PATH`` (line 247)
- `sqlite3.connect()  → `DB_PATH`` (line 254)
- `sqlite3.connect()  → `DB_PATH`` (line 427)
- `sqlite3.connect()  → `DB_PATH`` (line 438)
- `sqlite3.connect()  → `DB_PATH`` (line 486)
- `sqlite3.connect()  → `DB_PATH`` (line 506)
- `sqlite3.connect()  → `DB_PATH`` (line 519)
- `sqlite3.connect()  → `DB_PATH`` (line 528)
- `sqlite3.connect()  → `DB_PATH`` (line 558)
- `sqlite3.connect()  → `DB_PATH`` (line 574)
- `sqlite3.connect()  → `DB_PATH`` (line 587)
- `sqlite3.connect()  → `DB_PATH`` (line 607)
- `sqlite3.connect()  → `DB_PATH`` (line 624)
- `sqlite3.connect()  → `DB_PATH`` (line 744)
- `sqlite3.connect()  → `DB_PATH`` (line 787)
- `sqlite3.connect()  → `DB_PATH`` (line 796)
- `sqlite3.connect()  → `DB_PATH`` (line 817)
- `sqlite3.connect()  → `DB_PATH`` (line 837)
- `sqlite3.connect()  → `DB_PATH`` (line 889)
- `sqlite3.connect()  → `DB_PATH`` (line 922)
- `sqlite3.connect()  → `DB_PATH`` (line 960)
- `sqlite3.connect()  → `DB_PATH`` (line 97)
- `sqlite3.connect()  → `DB_PATH`` (line 987)


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
