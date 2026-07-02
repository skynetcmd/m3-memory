#!/usr/bin/env python3
"""
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
    .venv\\Scripts\\pip install -r requirements.txt
    .venv\\Scripts\\python bin\\dashboard_server.py

Common failure
--------------
``ModuleNotFoundError: No module named 'uvicorn'`` (or ``fastapi``)
means the interpreter you launched with does not have the deps
installed — re-run the install step above using the same interpreter
you intend to launch the server with (typically ``.venv``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

# Ensure bin/ is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from m3_sdk import resolve_db_path
from memory.db import _db
from memory.search import memory_search_scored_impl
from memory_maintenance import gdpr_export_impl, gdpr_forget_impl

PORT = 8088
HOST = "127.0.0.1"

# --- Common HTML Parts (Styling, Header, Nav) ---
# HTML/CSS templates extracted to bin/dashboard/templates.py (behavior-preserving).
from dashboard.templates import (  # noqa: E402
    HEADER_HTML, STYLE_CSS, INDEX_HTML, BROWSE_HTML, AUDIT_HTML,
)


# --- FastAPI Setup ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    active = resolve_db_path(None)
    print(f"M3 Cognitive Portal serving SQLite database: {active}", flush=True)
    print(f"Server available at http://{HOST}:{PORT}", flush=True)
    yield

app = FastAPI(
    title="M3 Cognitive Portal",
    description="Observability and Browser Portal.",
    lifespan=lifespan
)

# --- Helpers ---
# --- Helpers & DB Selectors ---
_DB_PATHS = None

def get_db_paths() -> dict[str, str]:
    global _DB_PATHS
    if _DB_PATHS is None:
        from chatlog_config import DEFAULT_DB_PATH
        from m3_sdk import resolve_db_path
        from memory.config import FILES_DB_PATH
        _DB_PATHS = {
            "main": resolve_db_path(None),
            "chatlog": DEFAULT_DB_PATH,
            "files": FILES_DB_PATH
        }
    return _DB_PATHS

def get_active_db_path(request: Request) -> str:
    cookie_val = request.cookies.get("selected_db", "main")
    paths = get_db_paths()
    return paths.get(cookie_val, paths["main"])

def set_active_db_env(selected_db: str):
    paths = get_db_paths()
    if selected_db == "files":
        os.environ["M3_DATABASE"] = paths["main"]
    else:
        os.environ["M3_DATABASE"] = paths.get(selected_db, paths["main"])

def build_db_selector_html(selected_db: str) -> str:
    paths = get_db_paths()
    sizes = {}
    for k, path in paths.items():
        if os.path.exists(path):
            size_mb = os.path.getsize(path) / (1024 * 1024)
            sizes[k] = f"{size_mb:.1f} MB"
        else:
            sizes[k] = "0.0 MB"

    display_names = {
        "main": "Main DB",
        "chatlog": "Chatlog DB",
        "files": "Files DB"
    }

    active_display = display_names.get(selected_db, "Main DB")

    main_active = "active" if selected_db == "main" else ""
    chatlog_active = "active" if selected_db == "chatlog" else ""
    files_active = "active" if selected_db == "files" else ""

    return f"""
    <div class="db-selector-container">
        <button class="db-selector-btn" onclick="toggleDbMenu(event)">
            <svg class="db-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2C6.5 2 2 4 2 6.5v11C2 20 6.5 22 12 22s10-2 10-4.5v-11C22 4 17.5 2 12 2z"/><path d="M2 12c0 2.5 4.5 4.5 10 4.5s10-2 10-4.5"/><path d="M2 6.5C2 9 6.5 11 12 11s10-2 10-4.5"/></svg>
            <span id="activeDbName">{active_display}</span>
            <svg class="chevron-icon" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
        <div class="db-menu" id="dbMenu">
            <div class="db-menu-header">Select Cognitive Core</div>

            <div class="db-menu-item {main_active}" onclick="selectDatabase('main')">
                <div class="db-item-title-row">
                    <span class="db-item-title">Main DB</span>
                    <span class="db-item-size">{sizes['main']}</span>
                </div>
                <div class="db-item-meta">agent_memory.db</div>
                <div class="db-item-desc">Knowledge graph, core facts & decision records.</div>
            </div>

            <div class="db-menu-item {chatlog_active}" onclick="selectDatabase('chatlog')">
                <div class="db-item-title-row">
                    <span class="db-item-title">Chatlog DB</span>
                    <span class="db-item-size">{sizes['chatlog']}</span>
                </div>
                <div class="db-item-meta">agent_chatlog.db</div>
                <div class="db-item-desc">Historical chat turns, session history, promoting cues.</div>
            </div>

            <div class="db-menu-item {files_active}" onclick="selectDatabase('files')">
                <div class="db-item-title-row">
                    <span class="db-item-title">Files DB</span>
                    <span class="db-item-size">{sizes['files']}</span>
                </div>
                <div class="db-item-meta">files_database.db</div>
                <div class="db-item-desc">Ingested files, leaf chunks, fact extractions.</div>
            </div>
        </div>
    </div>
    """

def parse_metadata(metadata_json: str) -> tuple[list, dict]:
    """Helper to extract tags and extras from JSON string."""
    if not metadata_json:
        return [], {}
    try:
        meta = json.loads(metadata_json)
    except Exception:
        return [], {}
    tags = meta.get("tags") or []
    extras = {k: v for k, v in meta.items() if k != "tags" and v}
    return tags, extras

# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def get_index(request: Request):
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    db_selector_html = build_db_selector_html(selected_db)
    header = HEADER_HTML.format(explorer_active="active", browse_active="", audit_active="", db_selector_html=db_selector_html)
    content = INDEX_HTML.replace("{{ STYLE_CSS }}", STYLE_CSS).replace("{{ HEADER }}", header).replace("{{ db_path }}", selected_db_path)
    return HTMLResponse(content=content, status_code=200, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/browse", response_class=HTMLResponse)
async def get_browse(request: Request):
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    db_selector_html = build_db_selector_html(selected_db)
    header = HEADER_HTML.format(explorer_active="", browse_active="active", audit_active="", db_selector_html=db_selector_html)
    content = BROWSE_HTML.replace("{{ STYLE_CSS }}", STYLE_CSS).replace("{{ HEADER }}", header).replace("{{ db_path }}", selected_db_path)
    return HTMLResponse(content=content, status_code=200, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/audit", response_class=HTMLResponse)
async def get_audit(request: Request):
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    db_selector_html = build_db_selector_html(selected_db)
    header = HEADER_HTML.format(explorer_active="", browse_active="", audit_active="active", db_selector_html=db_selector_html)
    content = AUDIT_HTML.replace("{{ STYLE_CSS }}", STYLE_CSS).replace("{{ HEADER }}", header).replace("{{ db_path }}", selected_db_path)
    return HTMLResponse(content=content, status_code=200, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/api/stats", response_class=HTMLResponse)
async def get_stats(request: Request):
    """Returns dynamic HTML stats counters cards across Main, Chatlog and Files DBs."""
    selected_db = request.cookies.get("selected_db", "main")
    get_active_db_path(request)
    set_active_db_env(selected_db)

    total_mems = 0
    total_ents = 0
    total_rels = 0
    queue_len = 0
    chatlog_turns = 0
    chatlog_sessions = 0
    file_chunks = 0
    files_count = 0
    file_lines = 0

    from chatlog_config import DEFAULT_DB_PATH
    from m3_sdk import resolve_db_path
    from memory.config import FILES_DB_PATH

    main_db = resolve_db_path(None)
    chatlog_db = DEFAULT_DB_PATH
    files_db = FILES_DB_PATH

    # Query Main DB
    try:
        if os.path.exists(main_db):
            with sqlite3.connect(main_db, timeout=5.0) as conn:
                conn.row_factory = sqlite3.Row
                total_mems = conn.execute("SELECT COUNT(*) FROM memory_items WHERE COALESCE(is_deleted, 0) = 0 AND type != 'chat_log'").fetchone()[0]

                ent_exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entities'").fetchone()
                if ent_exists:
                    total_ents = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]

                rel_exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entity_relationships'").fetchone()
                if rel_exists:
                    total_rels = conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0]

                queue_exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entity_extraction_queue'").fetchone()
                if queue_exists:
                    queue_len = conn.execute("SELECT COUNT(*) FROM entity_extraction_queue").fetchone()[0]
    except Exception as e:
        print(f"Failed to query Main DB stats: {e}", flush=True)

    # Query Chatlog DB
    try:
        if chatlog_db == main_db:
            if os.path.exists(main_db):
                with sqlite3.connect(main_db, timeout=5.0) as conn:
                    chatlog_turns = conn.execute("SELECT COUNT(*) FROM memory_items WHERE type='chat_log'").fetchone()[0]
                    chatlog_sessions = conn.execute("SELECT COUNT(DISTINCT COALESCE(NULLIF(conversation_id, ''), 'legacy')) FROM memory_items WHERE type='chat_log'").fetchone()[0]
        else:
            if os.path.exists(chatlog_db):
                with sqlite3.connect(chatlog_db, timeout=5.0) as conn:
                    chatlog_turns = conn.execute("SELECT COUNT(*) FROM memory_items WHERE type='chat_log'").fetchone()[0]
                    chatlog_sessions = conn.execute("SELECT COUNT(DISTINCT COALESCE(NULLIF(conversation_id, ''), 'legacy')) FROM memory_items WHERE type='chat_log'").fetchone()[0]
    except Exception as e:
        print(f"Failed to query Chatlog DB stats: {e}", flush=True)

    # Query Files DB
    try:
        if os.path.exists(files_db):
            with sqlite3.connect(files_db, timeout=5.0) as conn:
                leaves_exist = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='leaves'").fetchone()
                if leaves_exist:
                    file_chunks = conn.execute("SELECT COUNT(*) FROM leaves").fetchone()[0]
                    # Count deduplicated non-blank lines in active leaves
                    dedup_leaves = conn.execute("SELECT text FROM leaves WHERE superseded_by IS NULL GROUP BY text_sha256").fetchall()
                    file_lines = sum(sum(1 for line in (leaf[0] or "").splitlines() if line.strip()) for leaf in dedup_leaves)
                nodes_exist = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='file_nodes'").fetchone()
                if nodes_exist:
                    files_count = conn.execute("SELECT COUNT(*) FROM file_nodes").fetchone()[0]
    except Exception as e:
        print(f"Failed to query Files DB stats: {e}", flush=True)

    # Dynamic CSS styling and sub-labels based on active selection
    highlight_main = ""
    highlight_chatlog = ""
    highlight_files = ""

    sub_main = ""
    sub_chatlog = ""
    sub_files = ""

    style_main = ""
    style_chatlog = ""
    style_files = ""

    if selected_db == "main":
        highlight_main = "highlight-main"
        sub_main = '<span style="font-size: 0.72rem; color: var(--m3-neon-cyan); opacity: 0.85;">(Active Core)</span>'
        sub_chatlog = '<span style="font-size: 0.72rem; color: hsl(300, 10%, 50%);">(Chatlog DB)</span>'
        sub_files = '<span style="font-size: 0.72rem; color: hsl(120, 10%, 50%);">(Files DB)</span>'
    elif selected_db == "chatlog":
        highlight_chatlog = "highlight-chatlog"
        style_main = "opacity: 0.55;"
        sub_main = '<span style="font-size: 0.72rem; color: hsl(210, 10%, 50%);">(Main DB only)</span>'
        sub_chatlog = '<span style="font-size: 0.72rem; color: hsl(300, 100%, 65%); opacity: 0.85;">(Active Core)</span>'
        sub_files = '<span style="font-size: 0.72rem; color: hsl(120, 10%, 50%);">(Files DB)</span>'
    elif selected_db == "files":
        highlight_files = "highlight-files"
        style_main = "opacity: 0.55;"
        style_chatlog = "opacity: 0.55;"
        sub_main = '<span style="font-size: 0.72rem; color: hsl(210, 10%, 50%);">(Main DB only)</span>'
        sub_chatlog = '<span style="font-size: 0.72rem; color: hsl(300, 10%, 50%);">(Chatlog DB)</span>'
        sub_files = '<span style="font-size: 0.72rem; color: var(--m3-neon-emerald); opacity: 0.85;">(Active Core)</span>'

    banner_html = ""
    if selected_db == "main":
        banner_html = """
        <div class="m3-alert-banner banner-main">
            <svg class="alert-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink: 0;"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>
            <div class="alert-content">
                <strong>Main Cognitive Core Active:</strong> Showing structured semantic memories, entities, and relationships.
            </div>
        </div>
        """
    elif selected_db == "chatlog":
        banner_html = """
        <div class="m3-alert-banner banner-chatlog">
            <svg class="alert-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink: 0;"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            <div class="alert-content">
                <strong>Chatlog Core Active:</strong> Showing raw user-agent conversation history. Structured memories, entities, and relationships are automatically compiled in the <em>Main DB</em> during agent compaction sweeps.
            </div>
        </div>
        """
    elif selected_db == "files":
        banner_html = """
        <div class="m3-alert-banner banner-files">
            <svg class="alert-icon" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink: 0;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
            <div class="alert-content">
                <strong>Files Core Active:</strong> Displaying ingested document files and parsed text chunks. Promotable facts are compiled in the <em>Main DB</em> during walker curation runs.
            </div>
        </div>
        """

    return f"""
    {banner_html}
    <div class="metric-card {highlight_main}" style="{style_main}">
        <div class="metric-value">{total_mems}</div>
        <div class="metric-label">Memories</div>
        <div style="margin-top: 0.35rem; font-weight: 500;">{sub_main}</div>
    </div>
    <div class="metric-card {highlight_main}" style="{style_main}">
        <div class="metric-value" style="color: var(--m3-neon-purple);">{total_ents}</div>
        <div class="metric-label">Entities</div>
        <div style="margin-top: 0.35rem; font-weight: 500;">{sub_main}</div>
    </div>
    <div class="metric-card {highlight_main}" style="{style_main}">
        <div class="metric-value" style="color: var(--m3-neon-cyan);">{total_rels}</div>
        <div class="metric-label">Relationships</div>
        <div style="margin-top: 0.35rem; font-weight: 500;">{sub_main}</div>
    </div>
    <div class="metric-card {highlight_main}" style="{style_main}">
        <div class="metric-value" style="color: var(--m3-neon-amber);">{queue_len}</div>
        <div class="metric-label">Queue Pending</div>
        <div style="margin-top: 0.35rem; font-weight: 500;">{sub_main}</div>
    </div>
    <div class="metric-card {highlight_chatlog}" style="{style_chatlog}">
        <div class="metric-value" style="color: hsl(300, 100%, 65%);">{chatlog_sessions:,}</div>
        <div class="metric-label">Chatlog Sessions</div>
        <div style="margin-top: 0.25rem; font-size: 0.8rem; color: hsl(300, 20%, 75%); font-family: 'Fira Code', monospace; font-weight: 500;">
            {chatlog_turns:,} turns
        </div>
        <div style="margin-top: 0.35rem; font-weight: 500;">{sub_chatlog}</div>
    </div>
    <div class="metric-card {highlight_files}" style="{style_files}">
        <div class="metric-value" style="color: var(--m3-neon-emerald);">{file_chunks:,}</div>
        <div class="metric-label">File Chunks</div>
        <div style="margin-top: 0.25rem; font-size: 0.8rem; color: hsl(120, 20%, 75%); font-family: 'Fira Code', monospace; font-weight: 500;">
            {file_lines:,} lines ({files_count:,} files)
        </div>
        <div style="margin-top: 0.35rem; font-weight: 500;">{sub_files}</div>
    </div>
    """


@app.get("/api/pipeline", response_class=HTMLResponse)
async def get_pipeline(request: Request):
    """Governor state + per-queue length, throughput (1/10/30/60 min), and
    estimated drain time. Data comes from dashboard.queue_stats (pure, tested);
    this route only renders. Polled by HTMX every few seconds."""
    selected_db = request.cookies.get("selected_db", "main")
    set_active_db_env(selected_db)
    from m3_sdk import resolve_db_path
    from dashboard.queue_stats import collect_pipeline_stats, collect_governor

    db = resolve_db_path(None)
    try:
        stats = collect_pipeline_stats(db)
        gov = collect_governor(db)
    except Exception as e:  # never break the panel — degrade to a message
        return HTMLResponse(
            f'<div class="metric-label" style="color:var(--m3-neon-orange);">'
            f'pipeline stats unavailable: {type(e).__name__}</div>'
        )

    # Governor line
    if gov.get("available"):
        mode = gov["mode"]
        mode_color = {
            "HALTED": "var(--m3-neon-orange)", "THROTTLED": "var(--m3-neon-orange)",
            "TAPERED": "var(--m3-neon-cyan)", "CONTINUOUS": "var(--m3-neon-emerald)",
        }.get(mode, "hsl(210,15%,75%)")
        gov_html = (
            f'<div class="metric-card" style="grid-column: 1 / -1;">'
            f'<div class="metric-label">Governor</div>'
            f'<div style="display:flex; gap:1.25rem; align-items:baseline; flex-wrap:wrap; margin-top:0.35rem;">'
            f'<span style="color:{mode_color}; font-weight:700; font-family:\'Fira Code\',monospace;">{mode}</span>'
            f'<span style="font-family:\'Fira Code\',monospace; font-size:0.85rem; color:hsl(210,15%,75%);">'
            f'load {gov["load"]:.0f}% (cpu {gov["cpu"]:.0f} · ram {gov["ram"]:.0f} · gpu {gov["gpu"]:.0f})</span>'
            f'<span style="font-family:\'Fira Code\',monospace; font-size:0.85rem; color:hsl(210,15%,55%);">'
            f'throttle {gov["initial_threshold"]}% · halt {gov["limit_threshold"]}% · {gov["thermal"]}</span>'
            f'</div></div>'
        )
    else:
        gov_html = ('<div class="metric-card" style="grid-column: 1 / -1;">'
                    '<div class="metric-label">Governor</div>'
                    '<div style="color:hsl(210,15%,55%);">telemetry unavailable</div></div>')

    # One card per pipeline: queue length, rates, drain ETA.
    cards = [gov_html]
    for p in stats["pipelines"]:
        r = p["rates"]
        rate_line = " · ".join(
            f'{w}m: {r.get(w, 0.0):.1f}/min' for w in (1, 10, 30, 60)
        )
        qcolor = "var(--m3-neon-emerald)" if p["queue_len"] == 0 else "var(--m3-neon-cyan)"
        cards.append(
            f'<div class="metric-card">'
            f'<div class="metric-value" style="color:{qcolor};">{p["queue_len"]:,}</div>'
            f'<div class="metric-label">{p["label"]} queue</div>'
            f'<div style="margin-top:0.25rem; font-size:0.75rem; color:hsl(210,15%,70%); '
            f'font-family:\'Fira Code\',monospace;">{rate_line}</div>'
            f'<div style="margin-top:0.35rem; font-weight:600; font-size:0.85rem;">'
            f'drain: {p["eta_human"]}</div>'
            f'</div>'
        )
    return HTMLResponse("".join(cards))


@app.get("/api/graph", response_class=JSONResponse)
async def get_graph(request: Request):
    """Returns JSON nodes and links for the interactive canvas, fully DB selected aware."""
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    nodes = []
    links = []

    if selected_db == "files":
        try:
            with sqlite3.connect(selected_db_path, timeout=5.0) as conn:
                conn.row_factory = sqlite3.Row
                # Draw File Ingest Nodes
                files = conn.execute("SELECT uuid, filename, filetype FROM file_nodes LIMIT 40").fetchall()
                for f in files:
                    nodes.append({
                        "id": f["uuid"],
                        "name": f["filename"],
                        "type": "file"
                    })
                # Draw Chunk Leaf Nodes and link them to Files
                leaves = conn.execute("SELECT uuid, file_node, division_id FROM leaves LIMIT 100").fetchall()
                for l in leaves:
                    nodes.append({
                        "id": l["uuid"],
                        "name": f"Chunk {l['division_id']}",
                        "type": "chunk"
                    })
                    links.append({
                        "source": l["file_node"],
                        "target": l["uuid"],
                        "predicate": "contains"
                    })
        except Exception as e:
            print(f"Failed to query files graph: {e}", flush=True)
        return {"nodes": nodes, "links": links}

    try:
        from m3_sdk import active_database
        with active_database(selected_db_path):
            with _db() as db:
                ent_exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entities'").fetchone()
                if ent_exists:
                    rows = db.execute("SELECT id, canonical_name, entity_type FROM entities LIMIT 150").fetchall()
                    for r in rows:
                        nodes.append({
                            "id": r["id"],
                            "name": r["canonical_name"],
                            "type": r["entity_type"]
                        })

                rel_exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entity_relationships'").fetchone()
                if rel_exists:
                    rows = db.execute("SELECT from_entity, to_entity, predicate FROM entity_relationships LIMIT 250").fetchall()
                    for r in rows:
                        links.append({
                            "source": r["from_entity"],
                            "target": r["to_entity"],
                            "predicate": r["predicate"]
                        })
    except Exception as e:
        print(f"Failed to load graph database items: {e}", flush=True)

    return {"nodes": nodes, "links": links}


@app.get("/api/search", response_class=HTMLResponse)
async def search_memories(request: Request, q: str = ""):
    """Uses core memory_search_scored_impl or files_search based on selected core."""
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    if selected_db == "files":
        if not q.strip():
            return '<p style="color: hsl(210, 15%, 65%); text-align: center; padding: 2rem 0;">Type in search bar to scan files and chunk indexes.</p>'
        try:
            from files_memory.search import files_search
            hits = files_search(q, limit=15, db_path=selected_db_path)
            if not hits:
                return f'<p style="color: hsl(210, 15%, 65%); text-align: center; padding: 2rem 0;">No matching indexed file chunks found for "{q}".</p>'

            cards = []
            for hit in hits:
                cards.append(f"""
                <div class="memory-card">
                    <div class="memory-header">
                        <div>
                            <span class="m3-badge badge-fact" style="background: hsla(120, 100%, 45%, 0.1); color: var(--m3-neon-emerald); border: 1px solid rgba(16, 185, 129, 0.25);">FILE CHUNK</span>
                            <span style="font-family: 'Outfit', sans-serif; font-weight: 500; font-size: 0.95rem; margin-left: 0.5rem; color:#fff;">{hit.filename}</span>
                        </div>
                        <span class="memory-id">{hit.leaf_uuid[:8]}</span>
                    </div>
                    <div class="memory-content" style="margin-bottom: 0.75rem;">{hit.text}</div>
                    <div style="font-size: 0.75rem; color: hsl(210, 10%, 65%); display: flex; gap: 1rem; border-top: 1px solid var(--m3-border-glass); padding-top: 0.5rem;">
                        <span>Path: <code style="font-family: 'Fira Code', monospace; color: var(--m3-neon-cyan);">{hit.path}</code></span>
                        <span>Corpus: <code style="font-family: 'Fira Code', monospace; color: var(--m3-neon-purple);">{hit.corpus_id or 'default'}</code></span>
                        <span>Score: <strong style="color: var(--m3-neon-amber);">{hit.score:.4f}</strong></span>
                    </div>
                </div>
                """)
            return "\n".join(cards)
        except Exception as e:
            return f'<p style="color: var(--m3-neon-amber); text-align: center; padding: 2rem 0;">Error scanning files index: {str(e)}</p>'

    if not q.strip():
        return '<p style="color: hsl(210, 15%, 65%); text-align: center; padding: 2rem 0;">Type in search bar to explore FTS5 & Vector similarity explain graphs.</p>'

    try:
        from m3_sdk import active_database
        with active_database(selected_db_path):
            results = await memory_search_scored_impl(
                query=q,
                explain=True,
                extra_columns=["metadata_json", "conversation_id", "valid_from", "valid_to", "user_id"]
            )

            if not results:
                return f'<p style="color: hsl(210, 15%, 65%); text-align: center; padding: 2rem 0;">No matching indexed memories found for "{q}".</p>'

            cards = []
            for score, item in results:
                badge_class = "badge-note"
                if item.get("type") in ("fact", "local_device", "project", "reference"):
                    badge_class = "badge-fact"
                elif item.get("type") in ("contradiction", "decision", "task", "to_do"):
                    badge_class = "badge-warn"
                elif item.get("type") in ("knowledge", "network_config", "infrastructure", "chat_log"):
                    badge_class = "badge-sys"

                explain_html = ""
                exp = item.get("_explanation")
                if exp:
                    explain_html = f"""
                    <div class="explain-box">
                        <div class="explain-title">Explain Engine (Paired FTS5 + Vector Cosine)</div>
                        <div class="explain-grid">
                            <div class="explain-metric"><span>Vector Score:</span> {exp.get('vector', 0.0):.4f}</div>
                            <div class="explain-metric"><span>FTS BM25:</span> {exp.get('bm25', 0.0):.4f}</div>
                            <div class="explain-metric"><span>Raw Hybrid:</span> {exp.get('raw_hybrid', 0.0):.4f}</div>
                            <div class="explain-metric"><span>Title Boost:</span> {exp.get('title_boost', 0.0):.4f}</div>
                            <div class="explain-metric"><span>Role Boost:</span> {exp.get('role_boost', 0.0):.4f}</div>
                            <div class="explain-metric"><span>MMR Penalty:</span> -{exp.get('mmr_penalty', 0.0):.4f}</div>
                        </div>
                    </div>
                    """

                cards.append(f"""
                <div class="memory-card">
                    <div class="memory-header">
                        <div>
                            <span class="m3-badge {badge_class}">{item.get('type', 'note')}</span>
                            <span style="font-family: 'Outfit', sans-serif; font-weight: 500; font-size: 0.95rem; margin-left: 0.5rem; color:#fff;">{item.get('title') or 'Untitled Memory'}</span>
                        </div>
                        <span class="memory-id">{item.get('id')[:8]}</span>
                    </div>
                    <div class="memory-content">{item.get('content') or ''}</div>
                    {explain_html}
                </div>
                """)
            return "\n".join(cards)

    except Exception as e:
        return f'<p style="color: var(--m3-neon-amber); text-align: center; padding: 2rem 0;">Error scanning indices: {str(e)}</p>'


@app.get("/api/kb", response_class=HTMLResponse)
async def get_kb_cards(request: Request, q: str = "", type: str = "", limit: int = 50):
    """Replicates cli_kb_browse.py rank search or dynamic file logs into high-fidelity web cards."""
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    if selected_db == "files":
        try:
            query = "SELECT uuid, filename, path, filetype, size_bytes, corpus_id, created_at FROM file_nodes"
            params = []
            if q.strip():
                query += " WHERE filename LIKE ? OR path LIKE ?"
                params += [f"%{q}%", f"%{q}%"]
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(str(int(limit)))

            with sqlite3.connect(selected_db_path, timeout=5.0) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query, params).fetchall()

            total = len(rows)
            if total == 0:
                return '<p style="text-align: center; color: hsl(210, 15%, 65%); padding: 3rem 0;">No matching ingested files found.</p>'

            cards = []
            for idx, row in enumerate(rows, 1):
                size_kb = round(row["size_bytes"] / 1024, 1)
                cards.append(f"""
                <div class="memory-card">
                    <div class="memory-header">
                        <div>
                            <span style="font-family: 'Outfit', sans-serif; font-weight: 600; font-size: 0.85rem; color: var(--m3-neon-emerald);">#{idx:03d}/{total}</span>
                            <span class="m3-badge badge-sys" style="margin-left: 0.5rem;">{row['filetype'] or 'file'}</span>
                            <span style="font-family: 'Fira Code', monospace; font-size: 0.75rem; color: hsl(210, 15%, 50%); margin-left: 0.75rem;">size: {size_kb} KB &middot; corpus: {row['corpus_id'] or 'default'}</span>
                        </div>
                    </div>
                    <h3 style="font-family: 'Outfit', sans-serif; font-size: 1.15rem; font-weight: 600; color: #fff; margin-bottom: 0.5rem;">{row['filename']}</h3>
                    <div style="font-family: 'Fira Code', monospace; font-size: 0.7rem; color: hsl(210, 15%, 55%); margin-bottom: 0.75rem;">
                        uuid: {row['uuid']} &middot; created: {row['created_at']}
                    </div>
                    <div class="memory-content" style="white-space: pre-wrap; font-family: 'Fira Code', monospace; font-size: 0.8rem; color: var(--m3-neon-cyan);">path: {row['path']}</div>
                </div>
                """)
            return "\n".join(cards)
        except Exception as e:
            return f'<p style="color: var(--m3-neon-amber); text-align: center; padding: 2rem 0;">Error scanning files DB: {str(e)}</p>'

    try:
        query = """
            SELECT id, type, title, content, metadata_json, importance,
                   origin_device, change_agent, created_at, updated_at
            FROM memory_items
            WHERE is_deleted = 0
        """
        params = []

        if type:
            query += " AND type = ?"
            params.append(type)

        if q.strip():
            query += " AND (LOWER(title) LIKE LOWER(?) OR LOWER(content) LIKE LOWER(?))"
            params += [f"%{q}%", f"%{q}%"]

        query += " ORDER BY importance DESC, updated_at DESC"
        query += f" LIMIT {int(limit)}"

        from m3_sdk import active_database
        with active_database(selected_db_path):
            with _db() as db:
                rows = db.execute(query, params).fetchall()

        total = len(rows)
        if total == 0:
            return '<p style="text-align: center; color: hsl(210, 15%, 65%); padding: 3rem 0;">No matching entries found.</p>'

        cards = []
        for idx, row in enumerate(rows, 1):
            importance = row["importance"] or 0.0
            bar_color = "var(--m3-neon-purple)"
            if importance >= 0.9:
                bar_color = "var(--m3-neon-emerald)"
            elif importance >= 0.7:
                bar_color = "var(--m3-neon-amber)"

            badge_class = "badge-note"
            if row["type"] in ("fact", "local_device", "project", "reference"):
                badge_class = "badge-fact"
            elif row["type"] in ("decision", "task", "to_do"):
                badge_class = "badge-warn"
            elif row["type"] in ("knowledge", "network_config", "infrastructure", "chat_log"):
                badge_class = "badge-sys"

            tags, extras = parse_metadata(row["metadata_json"])
            tag_html = ""
            if tags:
                tag_html = '<div style="display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.75rem;">' + \
                           "".join([f'<span class="m3-tag">{t}</span>' for t in tags]) + '</div>'

            extras_html = ""
            if extras:
                extras_list = [f'<span style="margin-right: 0.75rem; color: hsl(210, 10%, 65%);"><strong style="color: hsl(210, 10%, 80%);">{k}:</strong> {v}</span>' for k, v in extras.items()]
                extras_html = '<div style="font-family: \'Fira Code\', monospace; font-size: 0.75rem; margin-top: 0.5rem; display: flex; flex-wrap: wrap;">' + \
                              "".join(extras_list) + '</div>'

            cards.append(f"""
            <div class="memory-card">
                <div class="memory-header">
                    <div>
                        <span style="font-family: 'Outfit', sans-serif; font-weight: 600; font-size: 0.85rem; color: var(--m3-neon-cyan);">#{idx:03d}/{total}</span>
                        <span class="m3-badge {badge_class}" style="margin-left: 0.5rem;">{row['type']}</span>
                        <span style="font-family: 'Fira Code', monospace; font-size: 0.75rem; color: hsl(210, 15%, 50%); margin-left: 0.75rem;">{row['origin_device'] or '?'} &middot; {row['change_agent'] or '?'}</span>
                    </div>
                    <div class="m3-progress-container" title="Importance score: {importance:.2f}">
                        <div class="m3-progress-bar">
                            <div class="m3-progress-fill" style="width: {importance * 100}%; background-color: {bar_color}; box-shadow: 0 0 8px {bar_color};"></div>
                        </div>
                        <span>{importance:.2f}</span>
                    </div>
                </div>
                <h3 style="font-family: 'Outfit', sans-serif; font-size: 1.15rem; font-weight: 600; color: #fff; margin-bottom: 0.5rem;">{row['title'] or 'Untitled Entry'}</h3>
                <div style="font-family: 'Fira Code', monospace; font-size: 0.7rem; color: hsl(210, 15%, 55%); margin-bottom: 0.75rem;">
                    id: {row['id']} &middot; created: {(row['created_at'] or '')[:19]} &middot; updated: {(row['updated_at'] or '')[:19]}
                </div>
                <div class="memory-content" style="white-space: pre-wrap;">{row['content'] or ''}</div>
                {tag_html}
                {extras_html}
            </div>
            """)
        return "\n".join(cards)

    except Exception as e:
        return f'<p style="color: var(--m3-neon-amber); text-align: center; padding: 2rem 0;">Error scanning DB: {str(e)}</p>'


@app.get("/api/history", response_class=HTMLResponse)
async def get_history_feed(request: Request):
    """Queries the memory_history table for the conflict log."""
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    logs = []
    try:
        from m3_sdk import active_database
        with active_database(selected_db_path):
            with _db() as db:
                hist_exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_history'").fetchone()
                if hist_exists:
                    rows = db.execute(
                        "SELECT event, memory_id, prev_value, new_value, created_at FROM memory_history ORDER BY created_at DESC LIMIT 5"
                    ).fetchall()
                    for r in rows:
                        action = r["event"].upper()
                        ts = r["created_at"].replace("T", " ")[:16]
                        color = "var(--m3-neon-purple)"
                        border = "1px solid var(--m3-border-glass)"

                        if action in ("SUPERSEDE", "CONTRADICTION"):
                            color = "var(--m3-neon-amber)"
                            border = "1px solid rgba(245, 158, 11, 0.25)"
                        elif action == "DELETE":
                            color = "hsl(15, 100%, 50%)"
                            border = "1px solid rgba(239, 68, 68, 0.25)"

                        logs.append(f"""
                        <div class="conflict-item" style="border-left-color: {color}; border-color: {border}; background: hsla(222, 22%, 5%, 0.45);">
                            <div style="display: flex; justify-content: space-between; font-weight: 600; font-family: 'Outfit', sans-serif; font-size: 0.8rem; color: {color}; margin-bottom: 0.25rem;">
                                <span>ACTION: {action}</span>
                                <span style="font-family: 'Fira Code', monospace; color: hsl(210, 15%, 50%); font-weight: 400;">{ts}</span>
                            </div>
                            <div style="color: hsl(210, 15%, 85%); font-size: 0.82rem; margin-top: 0.4rem;">
                                <strong>ID:</strong> {r['memory_id'][:8]}<br>
                                <strong>Details:</strong> {r['new_value'] or r['prev_value'] or 'System record updated.'}
                            </div>
                        </div>
                        """)
    except Exception as e:
        print(f"Failed to query history log: {e}", flush=True)

    if not logs:
        return '<p style="color: hsl(210, 15%, 55%); text-align: center; font-size: 0.85rem; padding: 1rem 0;">No history logs captured yet.</p>'

    return "\n".join(logs)


# --- Timeline Diff and Audit Helpers ---
def make_html_diff(prev_val: str, new_val: str) -> str:
    if not prev_val:
        prev_val = ""
    if not new_val:
        new_val = ""
    prev_val = prev_val.strip()
    new_val = new_val.strip()
    if not prev_val and not new_val:
        return '<span style="color: hsl(210, 10%, 50%); font-style: italic;">(Empty)</span>'
    if not prev_val:
        return f'<span style="background: rgba(16, 185, 129, 0.2); color: var(--m3-neon-emerald); padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(16, 185, 129, 0.3); font-weight: 500;">{new_val}</span>'
    if not new_val:
        return f'<span style="background: rgba(239, 68, 68, 0.2); color: #ff6b6b; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(239, 68, 68, 0.3); text-decoration: line-through;">{prev_val}</span>'

    import difflib
    matcher = difflib.SequenceMatcher(None, prev_val.split(), new_val.split())
    result = []
    for opcode, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if opcode == 'equal':
            result.append(" ".join(prev_val.split()[a_start:a_end]))
        elif opcode == 'insert':
            inserted = " ".join(new_val.split()[b_start:b_end])
            result.append(f'<span style="background: rgba(16, 185, 129, 0.25); color: #88ff88; border-bottom: 2px solid var(--m3-neon-emerald); padding: 0 4px; border-radius: 2px; font-weight: 600;">{inserted}</span>')
        elif opcode == 'delete':
            deleted = " ".join(prev_val.split()[a_start:a_end])
            result.append(f'<span style="background: rgba(239, 68, 68, 0.25); color: #ff6b6b; text-decoration: line-through; border-bottom: 2px solid #ef4444; padding: 0 4px; border-radius: 2px;">{deleted}</span>')
        elif opcode == 'replace':
            deleted = " ".join(prev_val.split()[a_start:a_end])
            inserted = " ".join(new_val.split()[b_start:b_end])
            result.append(f'<span style="background: rgba(239, 68, 68, 0.25); color: #ff6b6b; text-decoration: line-through; padding: 0 4px; border-radius: 2px;">{deleted}</span>'
                          f' <span style="background: rgba(16, 185, 129, 0.25); color: #88ff88; padding: 0 4px; border-radius: 2px; font-weight: 600;">{inserted}</span>')
    return " ".join(result)


def render_audit_card(memory_id: str, db: Any) -> str:
    # 1. Fetch current memory state from memory_items if it exists
    row = db.execute(
        "SELECT title, content, type, user_id, importance, is_deleted, updated_at "
        "FROM memory_items WHERE id = ?", (memory_id,)
    ).fetchone()

    current_title = "None (Deleted)"
    current_content = ""
    current_type = "unknown"
    is_deleted = True
    user_id = ""
    importance = 0.0

    if row:
        current_title = row["title"] or "(Untitled)"
        current_content = row["content"] or ""
        current_type = row["type"] or "note"
        is_deleted = bool(row["is_deleted"])
        user_id = row["user_id"] or ""
        importance = row["importance"] or 0.0
        row["updated_at"] or ""

    # 2. Fetch history records sorted by created_at DESC
    hist_rows = db.execute(
        "SELECT event, field, prev_value, new_value, actor_id, created_at "
        "FROM memory_history WHERE memory_id = ? "
        "ORDER BY created_at DESC", (memory_id,)
    ).fetchall()

    # Check if there's any active conflict/contradiction
    has_contradiction = any(r["event"].upper() == "CONTRADICTION" for r in hist_rows)

    status_label = "Active"
    status_style = "background: hsla(145, 100%, 45%, 0.1); color: var(--m3-neon-emerald); border: 1px solid rgba(16, 185, 129, 0.25);"

    if is_deleted:
        status_label = "Deleted"
        status_style = "background: hsla(15, 100%, 50%, 0.1); color: hsl(15, 100%, 55%); border: 1px solid rgba(239, 68, 68, 0.25);"
    elif has_contradiction:
        status_label = "Contradiction State"
        status_style = "background: hsla(38, 100%, 50%, 0.1); color: var(--m3-neon-amber); border: 1px solid rgba(245, 158, 11, 0.25);"

    # 3. Generate timeline HTML
    nodes_html = []
    for r in hist_rows:
        event = r["event"].upper()
        field = r["field"] or "content"
        prev_v = r["prev_value"] or ""
        new_v = r["new_value"] or ""
        actor = r["actor_id"] or "system"
        ts = r["created_at"].replace("T", " ")[:16]

        badge_cls = f"badge-{event.lower()}"
        icon = event[0] if event else "U"

        diff_html = ""
        if event in ("UPDATE", "SUPERSEDE", "CONTRADICTION"):
            diff_block = make_html_diff(prev_v, new_v)
            diff_html = f"""
            <div style="margin-top: 0.5rem;">
                <span style="font-size: 0.75rem; font-weight: 600; color: hsl(210, 10%, 60%);">Diff ({field}):</span>
                <div class="diff-text">{diff_block}</div>
            </div>
            """
        elif event == "CREATE":
            diff_html = f"""
            <div style="margin-top: 0.5rem;">
                <span style="font-size: 0.75rem; font-weight: 600; color: hsl(210, 10%, 60%);">Initial Content:</span>
                <div class="diff-text" style="color: var(--m3-neon-emerald);">{new_v or prev_v}</div>
            </div>
            """
        elif event == "DELETE":
            diff_html = f"""
            <div style="margin-top: 0.5rem;">
                <span style="font-size: 0.75rem; font-weight: 600; color: hsl(210, 10%, 60%);">Deleted Value:</span>
                <div class="diff-text" style="color: #ff6b6b; text-decoration: line-through;">{prev_v}</div>
            </div>
            """
        elif event == "RESOLVE":
            diff_html = f"""
            <div style="margin-top: 0.5rem;">
                <span style="font-size: 0.75rem; font-weight: 600; color: hsl(210, 10%, 60%);">Resolution:</span>
                <div class="diff-text" style="color: var(--m3-neon-purple);">{new_v or 'Conflict resolved, marked active.'}</div>
            </div>
            """

        nodes_html.append(f"""
        <div class="timeline-node">
            <div class="timeline-badge {badge_cls}">{icon}</div>
            <div class="timeline-content-box">
                <div style="display: flex; justify-content: space-between; align-items: center; font-size: 0.8rem;">
                    <strong style="color: var(--m3-neon-cyan);">{event}</strong>
                    <span style="color: hsl(210, 10%, 55%); font-family: 'Fira Code', monospace;">{ts}</span>
                </div>
                <div style="font-size: 0.78rem; color: hsl(210, 10%, 75%); margin-top: 0.25rem;">
                    by <strong style="color: #fff;">{actor}</strong> | field: <code>{field}</code>
                </div>
                {diff_html}
            </div>
        </div>
        """)

    timeline_flow_html = ""
    if nodes_html:
        timeline_flow_html = f"""
        <div class="timeline-flow">
            {"".join(nodes_html)}
        </div>
        """
    else:
        timeline_flow_html = '<p style="color: hsl(210, 10%, 50%); font-style: italic; margin-top: 1rem;">No detailed history recorded.</p>'

    # Actions panel
    actions_html = ""
    if not is_deleted:
        actions_html = f"""
        <button class="m3-btn" style="background: hsla(270, 100%, 65%, 0.15); color: var(--m3-neon-purple); border: 1px solid rgba(168, 85, 247, 0.25); font-size: 0.75rem; padding: 0.35rem 0.75rem;"
                hx-post="/api/audit/resolve/{memory_id}" hx-target="#card-{memory_id}" hx-swap="outerHTML">
            Resolve Conflict
        </button>
        <button class="m3-btn" style="background: transparent; border: 1px solid var(--m3-border-glass); color: hsl(210, 10%, 80%); font-size: 0.75rem; padding: 0.35rem 0.75rem;"
                onclick="document.getElementById('override-form-{memory_id}').style.display='block'">
            Override/Edit
        </button>
        <button class="m3-btn" style="background: hsla(15, 100%, 55%, 0.1); color: hsl(15, 100%, 70%); border: 1px solid rgba(239, 68, 68, 0.25); font-size: 0.75rem; padding: 0.35rem 0.75rem;"
                hx-post="/api/audit/soft-delete/{memory_id}" hx-target="#card-{memory_id}" hx-swap="outerHTML"
                hx-confirm="Are you sure you want to soft-delete this memory?">
            Soft Delete
        </button>
        <button class="m3-btn" style="background: hsla(15, 100%, 55%, 0.25); color: hsl(15, 100%, 80%); border: 1px solid rgba(239, 68, 68, 0.4); font-size: 0.75rem; padding: 0.35rem 0.75rem;"
                hx-post="/api/audit/hard-delete/{memory_id}" hx-target="#card-{memory_id}" hx-swap="outerHTML"
                hx-confirm="CRITICAL: Are you sure you want to HARD-delete this memory and purge its vectors?">
            Hard Delete
        </button>
        """
    else:
        # Deleted but can resolve or hard delete
        actions_html = f"""
        <button class="m3-btn" style="background: hsla(145, 100%, 45%, 0.15); color: var(--m3-neon-emerald); border: 1px solid rgba(16, 185, 129, 0.25); font-size: 0.75rem; padding: 0.35rem 0.75rem;"
                hx-post="/api/audit/resolve/{memory_id}" hx-target="#card-{memory_id}" hx-swap="outerHTML">
            Restore Memory
        </button>
        <button class="m3-btn" style="background: hsla(15, 100%, 55%, 0.25); color: hsl(15, 100%, 80%); border: 1px solid rgba(239, 68, 68, 0.4); font-size: 0.75rem; padding: 0.35rem 0.75rem;"
                hx-post="/api/audit/hard-delete/{memory_id}" hx-target="#card-{memory_id}" hx-swap="outerHTML"
                hx-confirm="CRITICAL: Are you sure you want to HARD-delete this memory and purge its vectors?">
            Hard Delete (Purge)
        </button>
        """

    return f"""
    <div class="timeline-group-card" id="card-{memory_id}">
        <div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 1rem; border-bottom: 1px solid var(--m3-border-glass); padding-bottom: 1rem;">
            <div>
                <span class="m3-badge" style="{status_style} font-size: 0.72rem; font-weight: 600; text-transform: uppercase; margin-bottom: 0.5rem; display: inline-block;">
                    {status_label}
                </span>
                <h3 style="font-family: 'Outfit', sans-serif; font-size: 1.15rem; font-weight: 600; color: #fff;">{current_title}</h3>
                <div style="font-family: 'Fira Code', monospace; font-size: 0.75rem; color: hsl(210, 10%, 65%); margin-top: 0.35rem; display: flex; flex-wrap: wrap; gap: 0.75rem;">
                    <span><strong>ID:</strong> {memory_id}</span>
                    <span><strong>Type:</strong> <span class="m3-badge badge-sys" style="font-size: 0.65rem; padding: 1px 4px;">{current_type}</span></span>
                    {f'<span><strong>User ID:</strong> {user_id}</span>' if user_id else ''}
                    <span><strong>Importance:</strong> {importance:.2f}</span>
                </div>
            </div>
            <div style="display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center;">
                {actions_html}
            </div>
        </div>

        <!-- Timeline Flow Section -->
        {timeline_flow_html}

        <!-- Inline Edit Override Form -->
        <div id="override-form-{memory_id}" style="display: none; margin-top: 1.5rem; padding: 1.25rem; background: hsla(222, 22%, 4%, 0.6); border: 1px dashed var(--m3-border-glass); border-radius: 8px;">
            <form hx-post="/api/audit/override/{memory_id}" hx-target="#card-{memory_id}" hx-swap="outerHTML">
                <div style="margin-bottom: 0.75rem;">
                    <label style="display: block; font-size: 0.8rem; margin-bottom: 0.25rem; font-weight: 600; color: var(--m3-neon-cyan);">Override Title</label>
                    <input type="text" name="title" class="m3-input" value="{current_title.replace('"', '&quot;')}" style="width: 100%;" required>
                </div>
                <div style="margin-bottom: 0.75rem;">
                    <label style="display: block; font-size: 0.8rem; margin-bottom: 0.25rem; font-weight: 600; color: var(--m3-neon-cyan);">Override Content</label>
                    <textarea name="content" class="m3-input" rows="4" style="width: 100%; font-family: 'Fira Code', monospace; font-size: 0.8rem; resize: vertical;" required>{current_content}</textarea>
                </div>
                <div style="display: flex; gap: 0.5rem; justify-content: flex-end;">
                    <button type="button" class="m3-btn" style="background: transparent; border: 1px solid var(--m3-border-glass); padding: 0.35rem 0.75rem; font-size: 0.75rem; color: #fff;" onclick="document.getElementById('override-form-{memory_id}').style.display='none'">Cancel</button>
                    <button type="submit" class="m3-btn" style="background: var(--m3-neon-cyan); color: #000; font-weight: 600; padding: 0.35rem 0.75rem; font-size: 0.75rem;">Apply Override</button>
                </div>
            </form>
        </div>
    </div>
    """


@app.get("/api/audit/timeline", response_class=HTMLResponse)
async def get_audit_timeline(request: Request, q: str = "", limit: int = 25):
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    try:
        from m3_sdk import active_database
        with active_database(selected_db_path):
            with _db() as db:
                hist_exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_history'").fetchone()
                if not hist_exists:
                    return '<p style="color: hsl(210, 15%, 55%); text-align: center; font-size: 0.85rem; padding: 2rem 0;">History table memory_history does not exist in this database.</p>'

                # Fetch distinct memory IDs that have history, sorted by their latest activity
                sql_ids = """
                    SELECT memory_id, MAX(created_at) as last_act
                    FROM memory_history
                    GROUP BY memory_id
                    ORDER BY last_act DESC
                """
                all_mems = db.execute(sql_ids).fetchall()

                matched_ids = []
                for m in all_mems:
                    mem_id = m["memory_id"]

                    # Look up item details to support keyword/title/content filtering
                    item = db.execute(
                        "SELECT title, content, type FROM memory_items WHERE id = ?", (mem_id,)
                    ).fetchone()

                    # If query is specified, check matches
                    if q:
                        q_lower = q.lower()
                        match = False
                        if q_lower in mem_id.lower():
                            match = True
                        if item:
                            if item["title"] and q_lower in item["title"].lower():
                                match = True
                            if item["content"] and q_lower in item["content"].lower():
                                match = True
                            if item["type"] and q_lower in item["type"].lower():
                                match = True
                        if not match:
                            continue

                    matched_ids.append(mem_id)
                    if len(matched_ids) >= limit:
                        break

                if not matched_ids:
                    return f'<p style="color: hsl(210, 15%, 55%); text-align: center; font-size: 0.85rem; padding: 2rem 0;">No change history timelines matched "{q}".</p>'

                cards_html = []
                for mem_id in matched_ids:
                    cards_html.append(render_audit_card(mem_id, db))

                return f"""
                <div class="timeline-container">
                    {"".join(cards_html)}
                </div>
                """
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f'<p style="color: var(--m3-neon-amber); text-align: center; font-size: 0.85rem; padding: 2rem 0;">Failed to scan timelines: {str(e)}</p>'


@app.post("/api/audit/override/{memory_id}", response_class=HTMLResponse)
async def audit_override(memory_id: str, request: Request, title: str = Form(...), content: str = Form(...)):
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    # Import and run memory_update_impl
    from memory_core import memory_update_impl
    await memory_update_impl(id=memory_id, title=title, content=content, reembed=True)

    from m3_sdk import active_database
    with active_database(selected_db_path):
        with _db() as db:
            return render_audit_card(memory_id, db)


@app.post("/api/audit/resolve/{memory_id}", response_class=HTMLResponse)
async def audit_resolve(memory_id: str, request: Request):
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    from datetime import datetime, timezone

    from m3_sdk import active_database
    from memory.db import _record_history
    with active_database(selected_db_path):
        with _db() as db:
            # 1. Update is_deleted = 0
            db.execute("UPDATE memory_items SET is_deleted = 0, updated_at = ? WHERE id = ?",
                       (datetime.now(timezone.utc).isoformat(), memory_id))
            # 2. Record "resolve" event in history
            row = db.execute("SELECT content FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
            content = row["content"] if row else "Restored and active."
            _record_history(memory_id, "resolve", "Conflict state or soft-deleted", content, "is_deleted", actor_id="dashboard", db=db)

            return render_audit_card(memory_id, db)


@app.post("/api/audit/soft-delete/{memory_id}", response_class=HTMLResponse)
async def audit_soft_delete(memory_id: str, request: Request):
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    from memory_core import memory_delete_impl
    memory_delete_impl(memory_id, hard=False)

    from m3_sdk import active_database
    with active_database(selected_db_path):
        with _db() as db:
            return render_audit_card(memory_id, db)


@app.post("/api/audit/hard-delete/{memory_id}", response_class=HTMLResponse)
async def audit_hard_delete(memory_id: str, request: Request):
    selected_db = request.cookies.get("selected_db", "main")
    get_active_db_path(request)
    set_active_db_env(selected_db)

    from memory_core import memory_delete_impl
    memory_delete_impl(memory_id, hard=True)

    return f"""
    <div class="timeline-group-card" id="card-{memory_id}" style="border-color: rgba(239, 68, 68, 0.4); background: hsla(15, 100%, 50%, 0.05); text-align: center; padding: 2rem;">
        <span style="color: hsl(15, 100%, 65%); font-size: 1.5rem; display: block; margin-bottom: 0.5rem;">💀 Memory Purged</span>
        <p style="font-size: 0.85rem; color: hsl(210, 10%, 75%);">
            Memory ID <code style="color: #fff;">{memory_id}</code> and all associated vector embeddings have been completely purged from the core layers (GDPR Article 17 hard-delete).
        </p>
    </div>
    """


@app.get("/api/gdpr/export")
async def export_gdpr_data(user_id: str = "default"):
    """Invokes core gdpr_export_impl as Article 20 JSON download."""
    try:
        raw_json = gdpr_export_impl(user_id)
        data = json.loads(raw_json)

        def iter_json():
            yield json.dumps(data, indent=2)

        headers = {
            "Content-Disposition": f'attachment; filename="m3_gdpr_export_{user_id}.json"'
        }
        return StreamingResponse(iter_json(), media_type="application/json", headers=headers)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@app.post("/api/gdpr/forget")
async def forget_gdpr_data(user_id: str = Form(...)):
    """Invokes core gdpr_forget_impl under Article 17."""
    try:
        msg = gdpr_forget_impl(user_id)
        return HTMLResponse(content=f"GDPR Purge Successful: {msg}", status_code=200)
    except Exception as e:
        return HTMLResponse(content=f"Purge Failed: {str(e)}", status_code=500)


active_task: dict[str, Any] = {
    "process": None,
    "action": None,
    "log_path": None,
    "log_file_handle": None,
    "expected_wait": ""
}

@app.post("/api/maintenance/{action}", response_class=HTMLResponse)
@app.post("/api/maintenance/trigger/{action}", response_class=HTMLResponse)
async def trigger_maintenance_task(action: str):
    """Asynchronously triggers maintenance scripts in the background."""
    import subprocess

    # Check if a task is already running
    proc = active_task["process"]
    if proc is not None and proc.poll() is None:
        return HTMLResponse(
            content=f"[Error] Another task ({active_task['action']}) is already running. Please wait for it to complete.",
            status_code=400
        )

    main_db = os.path.abspath(resolve_db_path(None))

    # Resolve Chatlog DB path
    try:
        from chatlog_config import resolve_config
        config = resolve_config()
        chatlog_db = os.path.abspath(config.db_path)
    except Exception:
        chatlog_db = main_db

    cmd_map = {
        "decay_dry": [sys.executable, os.path.join(os.path.dirname(__file__), "chatlog_decay.py"), "--db", chatlog_db],
        "decay_apply": [sys.executable, os.path.join(os.path.dirname(__file__), "chatlog_decay.py"), "--db", chatlog_db, "--apply"],
        "embed_sweep": [sys.executable, os.path.join(os.path.dirname(__file__), "chatlog_embed_sweeper.py"), "--database", main_db, "--drain-spill"],
        "backfill_titles": [sys.executable, os.path.join(os.path.dirname(__file__), "m3_chatlog_backfill_title.py"), "--yes"],
        "backfill_embeds": [sys.executable, os.path.join(os.path.dirname(__file__), "m3_chatlog_backfill_embed.py"), "--yes"],
        "files_health": [sys.executable, "-m", "files_memory.tools", "health", "--rebuild"]
    }

    wait_map = {
        "decay_dry": "5-15 seconds (dry-run)",
        "decay_apply": "5-15 seconds",
        "embed_sweep": "10-60 seconds (sweeping and compacting queue)",
        "backfill_titles": "30-180 seconds (generating titles via LLM/fallback)",
        "backfill_embeds": "1-5 minutes (generating embedding vectors for queue)",
        "files_health": "10-45 seconds (probing and rebuilding chunks)"
    }

    if action not in cmd_map:
        raise HTTPException(status_code=400, detail="Invalid action")

    cmd = cmd_map[action]
    expected_wait = wait_map.get(action, "1-3 minutes")

    log_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "maintenance_run.log"))

    # Close any existing open handles
    if active_task["log_file_handle"]:
        try:
            active_task["log_file_handle"].close()
        except Exception:
            pass
        active_task["log_file_handle"] = None

    # Truncate and prep new log file
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"[System] Starting {action}...\n")
            f.write(f"[System] Expected wait time: {expected_wait}\n")
            f.write(f"[System] Command: {' '.join(cmd)}\n\n")
    except Exception as e:
        return HTMLResponse(content=f"[Error] Failed to initialize log: {str(e)}", status_code=500)

    # Open log file in append mode for child process redirection
    log_file_handle = open(log_path, "a", encoding="utf-8", errors="replace")

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    bin_dir = os.path.dirname(os.path.abspath(__file__))
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] = bin_dir + os.pathsep + env["PYTHONPATH"]
    else:
        env["PYTHONPATH"] = bin_dir

    try:
        # Spawn child process in the background
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=log_file_handle,
            stderr=subprocess.STDOUT, # redirect stderr to stdout
            shell=False
        )

        active_task["process"] = proc
        active_task["action"] = action
        active_task["log_path"] = log_path
        active_task["log_file_handle"] = log_file_handle
        active_task["expected_wait"] = expected_wait

        return HTMLResponse(
            content=f"[System] Triggered background task '{action}'.\nExpected wait: {expected_wait}.\nRunning in background...",
            status_code=200
        )
    except Exception as e:
        try:
            log_file_handle.close()
        except Exception:
            pass
        return HTMLResponse(content=f"[Error] Failed to spawn process: {str(e)}", status_code=500)


@app.get("/api/maintenance/status")
async def get_maintenance_status():
    """Polls the status of the background maintenance task and reads live logs."""
    proc = active_task["process"]
    log_path = active_task["log_path"]

    status = "idle"
    exit_code = None

    if proc is not None:
        exit_code = proc.poll()
        if exit_code is not None:
            status = "finished"
            # Safe close handle
            if active_task["log_file_handle"]:
                try:
                    active_task["log_file_handle"].close()
                except Exception:
                    pass
                active_task["log_file_handle"] = None
        else:
            status = "running"

    # Read the log file
    logs = ""
    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                logs = f.read()
        except Exception as e:
            logs = f"Error reading live logs: {str(e)}"

    return JSONResponse(
        content={
            "status": status,
            "action": active_task["action"],
            "expected_wait": active_task["expected_wait"],
            "exit_code": exit_code,
            "logs": logs
        }
    )


# --- Execution Hook ---
if __name__ == "__main__":
    port_override = int(os.environ.get("M3_DASHBOARD_PORT", PORT))
    host_override = os.environ.get("M3_DASHBOARD_HOST", HOST)

    uvicorn.run("dashboard_server:app", host=host_override, port=port_override, reload=False)
