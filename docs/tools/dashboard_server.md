---
tool: bin/dashboard_server.py
sha1: 74e7c796de77
mtime_utc: 2026-05-31T18:57:47.132714+00:00
generated_utc: 2026-05-31T18:58:02.588220+00:00
private: false
---

# bin/dashboard_server.py

## Purpose

M3 Cognitive & Observability Portal.
FastAPI + HTMX unified local control center for Graph Exploration & KB Browsing.
Listens on port 8088 by default.

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `M3_DASHBOARD_HOST`
- `M3_DASHBOARD_PORT`
- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `chatlog_config (DEFAULT_DB_PATH)`
- `chatlog_config (resolve_config)`
- `m3_sdk (active_database)`
- `m3_sdk (resolve_db_path)`
- `memory_core (memory_delete_impl)`
- `memory_core (memory_update_impl)`
- `memory_maintenance (gdpr_export_impl, gdpr_forget_impl)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.Popen()  → `cmd`` (line 2887)

**sqlite**

- `sqlite3.connect()  → `chatlog_db`` (line 1919)
- `sqlite3.connect()  → `files_db`` (line 1928)
- `sqlite3.connect()  → `main_db`` (line 1892)
- `sqlite3.connect()  → `main_db`` (line 1914)
- `sqlite3.connect()  → `selected_db_path`` (line 2055)
- `sqlite3.connect()  → `selected_db_path`` (line 2228)


---

## Notable external imports

- `difflib`
- `fastapi (FastAPI, Form, HTTPException, Request)`
- `fastapi.responses (HTMLResponse, JSONResponse, StreamingResponse)`
- `files_memory.search (files_search)`
- `memory (config)`
- `memory.config (FILES_DB_PATH)`
- `memory.db (_db)`
- `memory.db (_record_history)`
- `memory.search (memory_search_scored_impl)`
- `uvicorn`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
