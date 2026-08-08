---
tool: bin/dashboard_server.py
sha1: fd70bdd75839
mtime_utc: 2026-08-07T23:53:51.859050+00:00
generated_utc: 2026-08-08T14:40:49.809933+00:00
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

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--host` | Bind address (default 127.0.0.1). | None |  | str |  |
| `--port` | TCP port (default 8088). | None |  | int |  |
| `--foreground` | Run the server in THIS process (used by the detached child and the boot task). Default launches detached. | `False` |  | store_true |  |
| `--stop` | Stop a running dashboard. | `False` |  | store_true |  |
| `--status` | Report dashboard status. | `False` |  | store_true |  |
| `--log-file` | argparse.SUPPRESS | None |  | str |  |

---

## Environment variables read

- `M3_DASHBOARD_HOST`
- `M3_DASHBOARD_PORT`
- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime`
- `_task_runtime (no_window_kwargs)`
- `chatlog_config (DEFAULT_DB_PATH)`
- `chatlog_config (resolve_config)`
- `m3_halt`
- `m3_sdk (acquire_or_exit)`
- `m3_sdk (active_database)`
- `m3_sdk (resolve_db_path)`
- `memory_core (memory_delete_impl)`
- `memory_core (memory_update_impl)`
- `memory_maintenance (gdpr_export_impl, gdpr_forget_impl)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.Popen()  → `[exe, script, '--foreground']`` (line 2864)
- `subprocess.Popen()  → `[sys.executable, script, '--foreground']`` (line 2874)
- `subprocess.Popen()  → `cmd`` (line 2585)
- `subprocess.run()  → `['powershell', '-NoProfile', '-Command', ps]`` (line 2744)
- `subprocess.run()  → `['taskkill', '/F', '/PID', str(pid)]`` (line 2779)


---

## Notable external imports

- `base64`
- `dashboard.health (_backend_display)`
- `dashboard.health (collect_health)`
- `dashboard.queue_stats (_entity_backlog_count)`
- `dashboard.queue_stats (collect_governor, collect_pipeline_stats)`
- `dashboard.templates (_WIKI_PAGE_HTML, AUDIT_HTML, BROWSE_HTML, HEADER_HTML, INDEX_HTML, STYLE_CSS)`
- `difflib`
- `fastapi (FastAPI, Form, HTTPException, Request)`
- `fastapi.responses (FileResponse, HTMLResponse, JSONResponse, StreamingResponse)`
- `files_memory.config (files_table)`
- `files_memory.db (_db)`
- `files_memory.db (_is_postgres)`
- `files_memory.search (files_search)`
- `html`
- `m3_core.paths (get_m3_engine_root)`
- `memory.backends (active_backend)`
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
