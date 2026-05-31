---
tool: bin/dashboard_server.py
sha1: 7627c4d36339
mtime_utc: 2026-05-31T18:39:28.267858+00:00
generated_utc: 2026-05-31T18:42:52.690276+00:00
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
- `memory_maintenance (gdpr_export_impl, gdpr_forget_impl)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.Popen()  → `cmd`` (line 2296)

**sqlite**

- `sqlite3.connect()  → `chatlog_db`` (line 1699)
- `sqlite3.connect()  → `files_db`` (line 1708)
- `sqlite3.connect()  → `main_db`` (line 1672)
- `sqlite3.connect()  → `main_db`` (line 1694)
- `sqlite3.connect()  → `selected_db_path`` (line 1835)
- `sqlite3.connect()  → `selected_db_path`` (line 2008)


---

## Notable external imports

- `fastapi (FastAPI, Form, HTTPException, Request)`
- `fastapi.responses (HTMLResponse, JSONResponse, StreamingResponse)`
- `files_memory.search (files_search)`
- `memory (config)`
- `memory.config (FILES_DB_PATH)`
- `memory.db (_db)`
- `memory.search (memory_search_scored_impl)`
- `uvicorn`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
