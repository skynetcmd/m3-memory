---
tool: bin/dashboard_server.py
sha1: 4f3afb2ee8ac
mtime_utc: 2026-07-02T21:51:11.638834+00:00
generated_utc: 2026-07-03T20:00:03.253887+00:00
private: false
---

# bin/dashboard_server.py

## Purpose

M3 Cognitive & Observability Portal.
FastAPI + HTMX unified local control center for Graph Exploration & KB Browsing.
Listens on port 8088 by default.

Requirements
------------
Python 3.11+ and the packages pinned in repo-root ``requirements.txt``
(at minimum: ``fastapi>=0.136.1``, ``uvicorn>=0.46.0``, plus the m3 deps
imported below: ``m3_sdk``, ``memory.db``, ``memory.search``,
``memory_maintenance``).

Install (run from the repo root)
--------------------------------
The recommended path on every platform is an isolated virtualenv at
``.venv/`` so the system Python stays clean.

macOS (Homebrew Python is PEP 668 "externally managed" — do NOT
``pip install`` into it directly):

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python bin/dashboard_server.py

Linux (same pattern; on Debian/Ubuntu you may need ``apt install
python3-venv`` first):

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python bin/dashboard_server.py

Windows (PowerShell):

    py -3 -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
    .venv\Scripts\python bin\dashboard_server.py

Common failure
--------------
``ModuleNotFoundError: No module named 'uvicorn'`` (or ``fastapi``)
means the interpreter you launched with does not have the deps
installed — re-run the install step above using the same interpreter
you intend to launch the server with (typically ``.venv``).

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

- `subprocess.Popen()  → `cmd`` (line 1318)

**sqlite**

- `sqlite3.connect()  → `chatlog_db`` (line 287)
- `sqlite3.connect()  → `files_db`` (line 296)
- `sqlite3.connect()  → `main_db`` (line 260)
- `sqlite3.connect()  → `main_db`` (line 282)
- `sqlite3.connect()  → `selected_db_path`` (line 487)
- `sqlite3.connect()  → `selected_db_path`` (line 660)


---

## Notable external imports

- `dashboard.queue_stats (collect_pipeline_stats, collect_governor)`
- `dashboard.templates (HEADER_HTML, STYLE_CSS, INDEX_HTML, BROWSE_HTML, AUDIT_HTML)`
- `difflib`
- `fastapi (FastAPI, Form, HTTPException, Request)`
- `fastapi.responses (HTMLResponse, JSONResponse, StreamingResponse)`
- `files_memory.search (files_search)`
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
