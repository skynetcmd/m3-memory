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

import html
import json
import os
import sys
from contextlib import asynccontextmanager
from typing import Any


def _h(s: object) -> str:
    """HTML-escape any DB-derived / user-supplied value before it lands in an
    f-string HTML template. quote=True so it is safe in an attribute context too
    (the local panel esc() omitted quotes). None -> '' so 'None' never renders."""
    return html.escape("" if s is None else str(s), quote=True)

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)

# Ensure bin/ is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from m3_sdk import resolve_db_path
from memory.db import _db
from memory.search import memory_search_scored_impl
from memory_maintenance import gdpr_export_impl, gdpr_forget_impl

PORT = 8088
HOST = "127.0.0.1"


# ── Backend-agnostic data access ──────────────────────────────────────────────
# The dashboard must work on EVERY registered storage backend (SQLite today,
# PostgreSQL now, MariaDB later) with no per-engine code here. It NEVER names an
# engine or opens sqlite3 directly; it goes through the storage-backend seam
# (memory.backends: active_backend / dialect), so a future backend that registers
# itself is picked up with zero dashboard changes.
#
#   * db_readonly(db_path) — a read-only-intent connection to a specific store.
#     On file backends it honors db_path (that .db file, read-only); on pooled
#     backends (PostgreSQL) there is one store, so db_path is ignored. Replaces
#     every raw ``sqlite3.connect(path)`` block.
#   * table_exists(conn, table) — backend-blind "does this table exist?".
#     Replaces the SQLite-only ``sqlite_master`` probe (to_regclass on PG, etc.).
#
# NOTE: os.path.exists(db_file) guards are dropped when porting a block — a file
# check is meaningless on a pooled backend; table_exists() + an empty result set
# already express "no such data" backend-blind.
def _active_backend():
    from memory.backends import active_backend
    return active_backend()


def db_readonly(db_path: str):
    """Read-only connection to a specific store, backend-blind (see module note)."""
    return _active_backend().open_readonly(db_path)


def table_exists(conn, table: str) -> bool:
    """True iff ``table`` exists, via the active backend's dialect (no sqlite_master)."""
    try:
        sql, params = _active_backend().dialect().table_exists(table)
        return conn.execute(sql, params).fetchone() is not None
    except Exception:  # noqa: BLE001 — a probe failure means "treat as absent", never crash a stats view
        return False


# --- Advanced search-query grammar (Memory Browser + Knowledge Graph) ---------
# Mirrors the JS parser in the graph window so both behave identically. Grammar:
#   unquoted word / +word  → must-have (AND)      "UAC window" == "+UAC +window"
#   -word                  → must-NOT-have
#   "phrase"               → exact-phrase must-have
#   -"phrase"              → exact-phrase must-NOT-have
# Case-insensitive by default (ignore_case=True). See parse_query_grammar.
import re as _re


def parse_query_grammar(raw: str) -> dict:
    """Split a query into include/exclude terms and phrases.

    Returns {includes, excludes, phrases, exclude_phrases, positive_text}.
    ``positive_text`` is the space-joined include terms + phrases, handed to the
    underlying ranked search so results are still relevance-ordered; the
    include/exclude sets are then applied as hard filters over content+title.
    """
    includes: list[str] = []
    excludes: list[str] = []
    phrases: list[str] = []
    exclude_phrases: list[str] = []
    # Tokens: optional leading -, then either "quoted phrase" or a bare word.
    for m in _re.finditer(r'(-?)"([^"]*)"|(-?)(\S+)', raw or ""):
        neg_q, phrase, neg_w, word = m.groups()
        if phrase is not None and (neg_q or phrase):
            (exclude_phrases if neg_q == "-" else phrases).append(phrase)
        elif word:
            if neg_w == "-":
                excludes.append(word.lstrip("+"))
            else:
                includes.append(word.lstrip("+"))
    positive_text = " ".join(includes + phrases).strip()
    return {
        "includes": includes, "excludes": excludes,
        "phrases": phrases, "exclude_phrases": exclude_phrases,
        "positive_text": positive_text,
    }


def matches_query_grammar(text: str, g: dict, ignore_case: bool = True) -> bool:
    """True iff ``text`` satisfies the parsed grammar ``g`` (from parse_query_grammar).

    All includes AND all phrases must be present; no excludes/exclude_phrases may
    be present. A phrase is matched literally; a word is a plain substring (the
    ranked search already handled tokenization/relevance — this is the hard
    include/exclude gate)."""
    hay = text or ""
    if ignore_case:
        hay = hay.lower()

    def present(term: str) -> bool:
        t = term.lower() if ignore_case else term
        return t in hay

    if any(not present(w) for w in g["includes"]):
        return False
    if any(not present(p) for p in g["phrases"]):
        return False
    if any(present(w) for w in g["excludes"]):
        return False
    if any(present(p) for p in g["exclude_phrases"]):
        return False
    return True


# --- Common HTML Parts (Styling, Header, Nav) ---
# HTML/CSS templates extracted to bin/dashboard/templates.py (behavior-preserving).
from dashboard.templates import (  # noqa: E402
    AUDIT_HTML,
    BROWSE_HTML,
    HEADER_HTML,
    INDEX_HTML,
    STYLE_CSS,
    _WIKI_PAGE_HTML,
)


def _logo_data_uri() -> str:
    """The m3 logo as an inline base64 data-URI so the dashboard renders it with
    no network (fully offline). Falls back to the raw.githubusercontent.com URL if
    the packaged PNG can't be found — the header still works, just needs the net.
    """
    import base64
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "docs", "m3_logo_icon.png"),   # dev tree (bin/ -> ../docs)
        os.path.join(here, "docs", "m3_logo_icon.png"),          # installed (m3_memory/docs)
    ]
    for path in candidates:
        try:
            with open(os.path.abspath(path), "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return f"data:image/png;base64,{b64}"
        except OSError:
            continue
    return ("https://raw.githubusercontent.com/skynetcmd/m3-memory/main/"
            "docs/m3_logo_icon.png")


# Bake the logo src into the header once at import (base64 has no {}/braces, so it
# survives the later str.format() calls that fill the nav-active + db-selector slots).
HEADER_HTML = HEADER_HTML.replace("{{ LOGO_SRC }}", _logo_data_uri())

# Standalone full-window Interactive Knowledge Graph (served at /graph, opened in
# its own browser window). Self-contained: a compact copy of the force-graph
# renderer (fetch /api/graph → physics → draw → drag/zoom), full-viewport, dark.
_GRAPH_WINDOW_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>M3 · Interactive Knowledge Graph</title>
<style>
  html,body{margin:0;height:100%;background:#0a0e17;overflow:hidden;font-family:'Outfit',system-ui,sans-serif}
  #c{display:block;width:100vw;height:100vh;cursor:grab}
  #c:active{cursor:grabbing}
  #bar{position:fixed;top:10px;left:12px;display:flex;gap:.6rem;align-items:center;
       background:rgba(10,14,23,.7);border:1px solid rgba(120,140,160,.25);border-radius:8px;
       padding:.4rem .7rem;color:#cbd5e1;font-size:.78rem;backdrop-filter:blur(4px);z-index:10}
  #bar button{background:#131a26;border:1px solid rgba(120,140,160,.3);color:#22d3ee;border-radius:4px;
       padding:3px 10px;font-size:.75rem;cursor:pointer}
  #bar .dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:3px}
  #status{color:#22d3ee;font-family:'Fira Code',monospace}
  #legend{position:fixed;bottom:10px;left:12px;display:flex;gap:.5rem;color:#94a3b8;font-size:.72rem;
       background:rgba(10,14,23,.6);border:1px solid rgba(120,140,160,.2);border-radius:8px;padding:.3rem .5rem;z-index:10}
  #legend .lg{display:flex;align-items:center;cursor:pointer;padding:.15rem .4rem;border-radius:5px;user-select:none;transition:opacity .15s}
  #legend .lg:hover{background:rgba(120,140,160,.15)}
  #legend .lg.off{opacity:.35;text-decoration:line-through}
  #hint{position:fixed;bottom:10px;right:12px;color:#64748b;font-size:.68rem;z-index:10;
       background:rgba(10,14,23,.6);border:1px solid rgba(120,140,160,.2);border-radius:8px;padding:.3rem .6rem}
  #search{background:#0d1420;border:1px solid rgba(120,140,160,.3);color:#e2e8f0;border-radius:5px;
       padding:3px 8px;font-size:.75rem;width:150px;outline:none}
  #search:focus{border-color:#22d3ee}
  #side{position:fixed;top:0;right:0;width:320px;max-width:80vw;height:100vh;z-index:20;
       background:rgba(9,13,22,.96);border-left:1px solid rgba(120,140,160,.25);backdrop-filter:blur(6px);
       transform:translateX(100%);transition:transform .2s ease;overflow-y:auto;padding:3.2rem 1rem 1rem;box-sizing:border-box}
  #side.open{transform:translateX(0)}
  #side h2{font-size:1.05rem;color:#fff;margin:0 0 .2rem}
  #side .type{font-size:.72rem;color:#22d3ee;font-family:'Fira Code',monospace;margin-bottom:.8rem}
  #side .rel{font-size:.8rem;color:#cbd5e1;font-family:'Fira Code',monospace;margin:.25rem 0;padding:.2rem .4rem;
       border-left:2px solid rgba(120,140,160,.25)}
  #side .rel .p{color:#94a3b8}
  #side .close{position:absolute;top:.7rem;right:.9rem;cursor:pointer;color:#94a3b8;font-size:1.1rem}
  #side .sec{color:#64748b;font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;margin:1rem 0 .3rem}
</style></head>
<body>
<div id="bar">
  <strong style="color:#fff">Knowledge Graph</strong>
  <span style="color:#f59e0b;font-size:.68rem;border:1px solid rgba(245,158,11,.4);border-radius:4px;padding:1px 6px;">ENTITIES &amp; LINKS — not raw memories</span>
  <input id="search" type="text" placeholder='filter entity names…' oninput="onSearch(this.value)" autocomplete="off">
  <label style="display:flex;align-items:center;gap:.25rem;font-size:.7rem;cursor:pointer;user-select:none;" title="Ignore case in the node filter">
    <input id="igcase" type="checkbox" checked onchange="updateEmpty();wake()">Aa</label>
  <span onclick="toggleHelp()" style="cursor:pointer;color:#22d3ee;font-size:.9rem;" title="Filter syntax">&#9432;</span>
  <span id="status">Status: Active</span>
  <button onclick="zoomBy(1.25)" title="Zoom in">+</button>
  <button onclick="zoomBy(0.8)" title="Zoom out">&minus;</button>
  <button onclick="fitToView()" title="Fit all nodes to the window">Fit</button>
  <button onclick="resetView()">Reset</button>
  <button onclick="loadGraph()">Reload</button>
</div>
<div id="banner" style="position:fixed;top:52px;left:50%;transform:translateX(-50%);z-index:12;
     background:rgba(9,13,22,.9);border:1px solid rgba(245,158,11,.35);border-radius:8px;
     padding:.35rem .8rem;color:#cbd5e1;font-size:.72rem;max-width:90vw;text-align:center">
  This is the <strong style="color:#f59e0b">knowledge-graph layer</strong> — extracted
  <em>entities</em> (files, functions, models, hosts…) and how they link, <strong>not</strong> a memory search.
  The filter matches <strong>entity names</strong>. To search memory text, use the
  Memory Browser on the main dashboard. <span onclick="this.parentElement.style.display='none'" style="cursor:pointer;color:#64748b;margin-left:.4rem">dismiss ✕</span>
</div>
<div id="legend"></div>
<div id="empty" style="display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:11;
     color:#94a3b8;font-size:.85rem;text-align:center;max-width:420px;line-height:1.6">
  <div style="font-size:1.4rem;margin-bottom:.4rem">◍</div>
  No <strong>entities</strong> match this filter.<br>
  <span style="font-size:.78rem;color:#64748b">The graph holds extracted entities (not memory text), so a term like a
  memory keyword may simply not be an entity name. Try a file, function, model, or host name — or clear the filter.</span>
</div>
<div id="hint">scroll = zoom · drag = pan · click node = details · click a type to filter</div>
<div id="qhelp" style="display:none;position:fixed;top:52px;left:12px;z-index:15;max-width:360px;
     background:rgba(9,13,22,.96);border:1px solid rgba(120,140,160,.3);border-radius:8px;padding:.7rem .9rem;
     color:#cbd5e1;font-size:.74rem;line-height:1.55">
  <strong style="color:#fff">Filter syntax</strong> — words are filters, all must match (AND):
  <ul style="margin:.35rem 0 0 1.1rem;padding:0">
    <li><code style="color:#22d3ee">UAC window</code> — nodes with both (same as <code>+UAC +window</code>)</li>
    <li><code style="color:#22d3ee">-window</code> — must not contain window</li>
    <li><code style="color:#22d3ee">"malformed string"</code> — the exact phrase</li>
    <li><code style="color:#22d3ee">UAC -"malformed string"</code> — has UAC, not that phrase</li>
    <li><code style="color:#22d3ee">Aa</code> checkbox = ignore case (on) / exact case (off)</li>
  </ul>
</div>
<div id="side"><span class="close" onclick="closeSide()">✕</span><div id="sideBody"></div></div>
<canvas id="c"></canvas>
<script>
const canvas=document.getElementById("c"), ctx=canvas.getContext("2d");
let nodes=[],links=[],selectedNode=null,scale=1,offsetX=0,offsetY=0;
let fpsLimit=30,lastFrame=0,sleeping=false;
// Node-type visibility filter (legend toggles). A type mapped to false is hidden.
const typeVisible={};
let queryG=null;  // parsed grammar of the current filter
function nodeClass(t){return (t==="person"||t==="place"||t==="topic")?t:"other";}
// Parse the search grammar (same rules as the Memory Browser):
//  word/+word = must-have (AND), -word = must-not, "phrase" = exact, -"phrase" = exclude phrase.
function parseGrammar(raw){
  const inc=[],exc=[],ph=[],exph=[];
  const re=/(-?)"([^"]*)"|(-?)(\S+)/g; let m;
  while((m=re.exec(raw||""))!==null){
    if(m[2]!==undefined && (m[1]||m[2])){ (m[1]==="-"?exph:ph).push(m[2]); }
    else if(m[4]){ const w=m[4].replace(/^\+/,""); (m[3]==="-"?exc:inc).push(w); }
  }
  return (inc.length||exc.length||ph.length||exph.length)?{inc,exc,ph,exph}:null;
}
function matchesSearch(n){
  if(!queryG)return true;
  const ic=document.getElementById("igcase").checked;
  let hay=(n.name||""); if(ic)hay=hay.toLowerCase();
  const has=t=>hay.includes(ic?t.toLowerCase():t);
  if(queryG.inc.some(w=>!has(w)))return false;
  if(queryG.ph.some(p=>!has(p)))return false;
  if(queryG.exc.some(w=>has(w)))return false;
  if(queryG.exph.some(p=>has(p)))return false;
  return true;
}
function isVisible(n){return typeVisible[nodeClass(n.type)]!==false && matchesSearch(n);}
// Show the "no entities match" empty-state when a filter hides everything.
function updateEmpty(){
  const anyVisible=nodes.some(isVisible);
  const filtering=!!queryG || Object.values(typeVisible).some(v=>v===false);
  document.getElementById("empty").style.display=(filtering && !anyVisible && nodes.length)?"block":"none";
}
function onSearch(v){queryG=parseGrammar((v||"").trim());updateEmpty();wake();}
function toggleHelp(){const b=document.getElementById("qhelp");b.style.display=b.style.display==="block"?"none":"block";}
function fit(){canvas.width=window.innerWidth;canvas.height=window.innerHeight;}
// Re-fit AND wake on resize: a resizable window means the canvas buffer must
// track the viewport, and the draw loop must re-run even if physics had slept
// (otherwise the resized canvas stays blank until the next interaction).
window.addEventListener("resize",()=>{fit();wake();});fit();
function setStatus(t,c){const s=document.getElementById("status");s.innerText="Status: "+t;s.style.color=c||"#22d3ee";}
// wake() (re)starts the draw loop. `running` guards against stacking multiple
// rAF loops; it flips false when draw() sleeps. Works on FIRST load (sleeping is
// false initially) AND on wake-from-sleep — the old `if(sleeping)` guard started
// the loop ONLY when waking from sleep, so on first load nothing ever drew.
let running=false;
// `alpha` is a simulation "temperature" that DECAYS toward 0 as the layout
// settles — like d3-force's alpha cooling. Motion is scaled by alpha, so nodes
// glide to rest and then hold (a static feel) instead of jittering forever. A
// full wake (load/reload) reheats to 1; a light nudge (drag, filter) only warms
// a little so the graph doesn't explode back into motion on every interaction.
let alpha=1;
function wake(full){alpha=Math.min(1, full?1:Math.max(alpha,0.35));
  sleeping=false;if(!running){running=true;requestAnimationFrame(draw);}}
function color(t){return t==="person"?"hsl(270,100%,75%)":t==="place"?"hsl(180,100%,50%)":t==="topic"?"hsl(38,100%,55%)":"hsl(145,100%,45%)";}
async function loadGraph(){
  try{
    const d=await (await fetch("/api/graph")).json();
    const old=new Map(nodes.map(n=>[n.id,n]));
    nodes=d.nodes.map(n=>{const e=old.get(n.id);return {...n,x:e?e.x:canvas.width/2+(Math.random()-.5)*300,y:e?e.y:canvas.height/2+(Math.random()-.5)*300,vx:0,vy:0};});
    links=d.links.map(l=>({...l,source:nodes.find(n=>n.id===l.source),target:nodes.find(n=>n.id===l.target)})).filter(l=>l.source&&l.target);
    wake(true);  // full reheat: lay out the fresh graph, then settle
  }catch(e){console.error("graph load failed",e);setStatus("Load failed","#f59e0b");}
}
function physics(){
  // Spread scales with node count so 150 nodes don't collapse into a blob.
  // fr=.78 (heavier damping) + motion scaled by `alpha` (temperature) → nodes
  // glide to rest and HOLD, instead of buzzing. CAP=6 keeps steps small/smooth.
  const LD=160, kR=.14, kL=.045, fr=.78, RANGE=520, CAP=6;
  for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){
    const a=nodes[i],b=nodes[j],dx=b.x-a.x,dy=b.y-a.y,d2=dx*dx+dy*dy+.1;
    if(d2<RANGE*RANGE){const d=Math.sqrt(d2),f=kR*(LD-d)/d;a.vx-=f*dx;a.vy-=f*dy;b.vx+=f*dx;b.vy+=f*dy;}
  }
  links.forEach(l=>{const dx=l.target.x-l.source.x,dy=l.target.y-l.source.y,d=Math.sqrt(dx*dx+dy*dy)||.1,f=kL*(d-LD)/d;
    l.source.vx+=f*dx;l.source.vy+=f*dy;l.target.vx-=f*dx;l.target.vy-=f*dy;});
  nodes.forEach(n=>{if(n===selectedNode)return;n.vx*=fr;n.vy*=fr;
    // Displacement scaled by alpha: as the sim cools, steps shrink toward 0.
    n.x+=Math.max(-CAP,Math.min(CAP,n.vx))*alpha;
    n.y+=Math.max(-CAP,Math.min(CAP,n.vy))*alpha;});
}
function draw(ts){
  if(sleeping){running=false;return;}
  if(!lastFrame)lastFrame=ts;
  if(ts-lastFrame<1000/fpsLimit){requestAnimationFrame(draw);return;}
  lastFrame=ts;
  ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.save();ctx.translate(offsetX,offsetY);ctx.scale(scale,scale);
  ctx.lineWidth=1;
  links.forEach(l=>{if(!isVisible(l.source)||!isVisible(l.target))return;
    ctx.strokeStyle="rgba(6,182,212,.25)";ctx.beginPath();ctx.moveTo(l.source.x,l.source.y);ctx.lineTo(l.target.x,l.target.y);ctx.stroke();
    const mx=(l.source.x+l.target.x)/2,my=(l.source.y+l.target.y)/2;ctx.fillStyle="rgba(210,210,220,.4)";ctx.font="8px 'Fira Code'";ctx.textAlign="center";ctx.fillText(l.predicate||"",mx,my-2);});
  nodes.forEach(n=>{if(!isVisible(n))return;const c=color(n.type);ctx.beginPath();ctx.arc(n.x,n.y,8,0,2*Math.PI);ctx.fillStyle=c;ctx.shadowColor=c;ctx.shadowBlur=8;ctx.fill();ctx.shadowBlur=0;
    ctx.fillStyle="#fff";ctx.font="10px 'Outfit',sans-serif";ctx.textAlign="center";ctx.fillText(n.name,n.x,n.y-12);});
  ctx.restore();
  physics();
  // Cool the simulation every frame so it converges to a stable, STATIC layout
  // and then stops (no perpetual jitter). ~0.985/frame ≈ settles in a few
  // seconds; once cold, freeze. A drag/filter/reheat warms it just enough to
  // re-settle. status shows "Settled" (not "Asleep") once at rest.
  alpha*=0.985;
  if(alpha<0.02){alpha=0;sleeping=true;running=false;setStatus("Settled","#10b981");}
  else{setStatus("Settling…");requestAnimationFrame(draw);}
}
let dragCanvas=false,dsm={x:0,y:0},dso={x:0,y:0},downXY=null,downNode=null;
canvas.addEventListener("mousedown",e=>{const r=canvas.getBoundingClientRect(),mx=(e.clientX-r.left-offsetX)/scale,my=(e.clientY-r.top-offsetY)/scale;
  // Hit-test only VISIBLE nodes so a hidden/filtered node can't be grabbed.
  selectedNode=nodes.find(n=>{if(!isVisible(n))return false;const dx=n.x-mx,dy=n.y-my;return dx*dx+dy*dy<144;});
  downNode=selectedNode;downXY={x:e.clientX,y:e.clientY};wake();
  if(!selectedNode){dragCanvas=true;dsm={x:e.clientX,y:e.clientY};dso={x:offsetX,y:offsetY};}});
canvas.addEventListener("mousemove",e=>{const r=canvas.getBoundingClientRect();
  if(selectedNode){selectedNode.x=(e.clientX-r.left-offsetX)/scale;selectedNode.y=(e.clientY-r.top-offsetY)/scale;wake();}
  else if(dragCanvas){offsetX=dso.x+(e.clientX-dsm.x);offsetY=dso.y+(e.clientY-dsm.y);wake();}});
window.addEventListener("mouseup",e=>{
  // A click (node pressed, negligible movement) opens its detail sidebar.
  if(downNode&&downXY){const moved=Math.abs(e.clientX-downXY.x)+Math.abs(e.clientY-downXY.y);
    if(moved<5)openNode(downNode);}
  selectedNode=null;dragCanvas=false;downNode=null;downXY=null;wake();});
// Zoom toward a screen point (keeps that point fixed while scaling).
function zoomAt(sx,sy,factor){
  const ns=Math.max(.15,Math.min(6,scale*factor));
  // world point under (sx,sy) must stay put: offset' = s - ns*((s-offset)/scale)
  offsetX=sx-(sx-offsetX)*(ns/scale);
  offsetY=sy-(sy-offsetY)*(ns/scale);
  scale=ns;wake();
}
canvas.addEventListener("wheel",e=>{e.preventDefault();
  const r=canvas.getBoundingClientRect();
  zoomAt(e.clientX-r.left,e.clientY-r.top, e.deltaY<0?1.12:1/1.12);
},{passive:false});
// Toolbar +/- zoom around the viewport center.
function zoomBy(f){zoomAt(canvas.width/2,canvas.height/2,f);}
function resetView(){scale=1;offsetX=0;offsetY=0;wake();}
// Auto-frame: fit all VISIBLE nodes into the window with padding.
function fitToView(){
  const vis=nodes.filter(isVisible);
  if(!vis.length)return;
  let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
  vis.forEach(n=>{minX=Math.min(minX,n.x);minY=Math.min(minY,n.y);maxX=Math.max(maxX,n.x);maxY=Math.max(maxY,n.y);});
  const w=Math.max(1,maxX-minX),h=Math.max(1,maxY-minY),pad=80;
  const s=Math.min((canvas.width-2*pad)/w,(canvas.height-2*pad)/h);
  scale=Math.max(.15,Math.min(3,s));
  offsetX=canvas.width/2-scale*(minX+maxX)/2;
  offsetY=canvas.height/2-scale*(minY+maxY)/2;
  wake();
}
// Clickable legend: toggles each node type's visibility.
const TYPES=[["person","hsl(270,100%,75%)"],["place","hsl(180,100%,50%)"],
             ["topic","hsl(38,100%,55%)"],["other","hsl(145,100%,45%)"]];
function renderLegend(){
  const el=document.getElementById("legend");
  el.innerHTML="";
  TYPES.forEach(([t,c])=>{
    const on=typeVisible[t]!==false;
    const span=document.createElement("span");
    span.className="lg"+(on?"":" off");
    span.innerHTML='<span class="dot" style="background:'+c+'"></span>'+t;
    span.onclick=()=>{typeVisible[t]=!(typeVisible[t]!==false);renderLegend();updateEmpty();wake();};
    el.appendChild(span);
  });
}
renderLegend();
// Node detail sidebar: fetch the entity + its relationships, render, slide in.
function esc(s){return (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
async function openNode(n){
  const side=document.getElementById("side"), body=document.getElementById("sideBody");
  body.innerHTML='<h2>'+esc(n.name)+'</h2><div class="type">'+esc(n.type)+'</div><div class="sec">loading…</div>';
  side.classList.add("open");
  try{
    const d=await (await fetch("/api/graph/node/"+encodeURIComponent(n.id))).json();
    let html='<h2>'+esc(d.name||n.name)+'</h2><div class="type">'+esc(d.type||n.type)+'</div>';
    html+='<div class="sec">id</div><div class="rel" style="border:none;color:#64748b;font-size:.7rem">'+esc(n.id)+'</div>';
    const nb=d.neighbors||[];
    html+='<div class="sec">relationships ('+nb.length+')</div>';
    if(nb.length){nb.forEach(r=>{html+='<div class="rel">'+esc(r.dir)+' <span class="p">'+esc(r.predicate)+'</span> '+esc(r.name)+'</div>';});}
    else{html+='<div class="rel" style="border:none;color:#64748b">no connections</div>';}
    body.innerHTML=html;
  }catch(e){body.innerHTML+='<div class="sec" style="color:#f59e0b">could not load details</div>';}
}
function closeSide(){document.getElementById("side").classList.remove("open");}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeSide();});
// Fit once the first layout has had a moment to spread.
loadGraph().then(()=>{setTimeout(fitToView,1200);updateEmpty();});
</script>
</body></html>
"""


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
    header = HEADER_HTML.format(explorer_active="active", browse_active="", audit_active="", health_active="", wiki_active="", db_selector_html=db_selector_html)
    content = INDEX_HTML.replace("{{ STYLE_CSS }}", STYLE_CSS).replace("{{ HEADER }}", header).replace("{{ db_path }}", selected_db_path)
    return HTMLResponse(content=content, status_code=200, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/browse", response_class=HTMLResponse)
async def get_browse(request: Request):
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    db_selector_html = build_db_selector_html(selected_db)
    header = HEADER_HTML.format(explorer_active="", browse_active="active", audit_active="", health_active="", wiki_active="", db_selector_html=db_selector_html)
    content = BROWSE_HTML.replace("{{ STYLE_CSS }}", STYLE_CSS).replace("{{ HEADER }}", header).replace("{{ db_path }}", selected_db_path)
    return HTMLResponse(content=content, status_code=200, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/audit", response_class=HTMLResponse)
async def get_audit(request: Request):
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    db_selector_html = build_db_selector_html(selected_db)
    header = HEADER_HTML.format(explorer_active="", browse_active="", audit_active="active", health_active="", wiki_active="", db_selector_html=db_selector_html)
    content = AUDIT_HTML.replace("{{ STYLE_CSS }}", STYLE_CSS).replace("{{ HEADER }}", header).replace("{{ db_path }}", selected_db_path)
    return HTMLResponse(content=content, status_code=200, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


def _wiki_html_path() -> str:
    """Absolute path to the generated self-contained wiki viewer, if any.

    Default vault location is <engine_root>/wiki/wiki.html (see bin/gen_wiki.py).
    """
    try:
        from m3_core.paths import get_m3_engine_root
        engine_root = get_m3_engine_root()
    except Exception:
        engine_root = os.path.join(os.path.expanduser("~"), ".m3", "engine")
    return os.path.join(engine_root, "wiki", "wiki.html")


@app.get("/wiki", response_class=HTMLResponse)
async def get_wiki(request: Request):
    """Show the generated wiki if present, else how to generate it.

    The wiki is a self-contained HTML vault produced by `m3 wiki generate --html`.
    When it exists we embed it in an iframe (served from /wiki/raw) inside the
    dashboard chrome; when it doesn't, we render OS-specific instructions.
    """
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)
    db_selector_html = build_db_selector_html(selected_db)
    header = HEADER_HTML.format(explorer_active="", browse_active="", audit_active="",
                                health_active="", wiki_active="active",
                                db_selector_html=db_selector_html)

    if os.path.isfile(_wiki_html_path()):
        body = (
            '<div style="flex:1; display:flex; flex-direction:column; min-height:0;">'
            '<div style="padding:0.4rem 1rem; display:flex; gap:1rem; align-items:center;">'
            '<span style="color:hsl(210,15%,60%); font-size:0.85rem;">'
            'Generated from your core memories · refresh with '
            '<code>m3 wiki generate --html</code></span>'
            '<a href="/wiki/raw" target="_blank" class="nav-link" '
            'style="margin-left:auto;">Open full screen ↗</a></div>'
            '<iframe src="/wiki/raw" title="m3 wiki" '
            'style="flex:1; width:100%; border:0; background:transparent;"></iframe>'
            '</div>'
        )
    else:
        body = _wiki_install_html()

    content = _WIKI_PAGE_HTML.replace("{{ STYLE_CSS }}", STYLE_CSS) \
                             .replace("{{ HEADER }}", header) \
                             .replace("{{ BODY }}", body)
    return HTMLResponse(content=content, status_code=200,
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/wiki/raw", response_class=HTMLResponse)
async def get_wiki_raw(request: Request):
    """Serve the raw self-contained wiki.html (for the iframe / full-screen)."""
    path = _wiki_html_path()
    if not os.path.isfile(path):
        return HTMLResponse(content="wiki not generated — run `m3 wiki generate --html`",
                            status_code=404)
    return FileResponse(path, media_type="text/html",
                        headers={"Cache-Control": "no-store"})


def _wiki_install_html() -> str:
    """Instructions shown when no wiki has been generated yet (OS variants)."""
    return """
    <div style="max-width:760px; margin:2rem auto; padding:0 1.5rem;
                font-family:'Outfit',sans-serif; color:hsl(210,15%,80%);">
      <h1 style="color:hsl(210,20%,92%);">No wiki generated yet</h1>
      <p>The <strong>m3 wiki</strong> compiles your canonical memories (pinned,
         high-confidence, beliefs, procedures) and indexed files into a browsable,
         interlinked knowledge base. Generate it once and it appears here.</p>

      <h3 style="color:hsl(180,100%,70%); margin-top:1.6rem;">Generate it</h3>
      <p>Run this in a terminal on the machine hosting m3 — the same command on
         every OS:</p>
      <pre style="background:hsla(222,22%,12%,0.9); border:1px solid hsla(210,15%,30%,0.4);
                  border-radius:8px; padding:0.9rem 1rem; overflow-x:auto;"><code>m3 wiki generate --html</code></pre>
      <p style="color:hsl(210,15%,60%); font-size:0.9rem;">
         Then reload this page. Re-run any time to refresh.</p>

      <h3 style="color:hsl(180,100%,70%); margin-top:1.6rem;">Opening a terminal</h3>
      <ul style="line-height:1.9;">
        <li><strong>Windows</strong> — press <code>Win</code>, type
            <em>PowerShell</em>, Enter. If <code>m3</code> isn't found, run
            <code>py -m m3_memory.cli wiki generate --html</code>.</li>
        <li><strong>macOS</strong> — open <em>Terminal</em>
            (<code>Cmd+Space</code> → "Terminal"). If <code>m3</code> isn't found,
            run <code>pipx ensurepath</code> and reopen the terminal.</li>
        <li><strong>Linux</strong> — open your terminal emulator. If <code>m3</code>
            isn't on <code>PATH</code>, run <code>pipx ensurepath</code> (or use
            <code>python3 -m m3_memory.cli wiki generate --html</code>).</li>
      </ul>

      <h3 style="color:hsl(180,100%,70%); margin-top:1.6rem;">Options</h3>
      <ul style="line-height:1.9;">
        <li><code>--importance-threshold 0.8</code> — a tighter, higher-signal vault.</li>
        <li><code>--synthesize</code> — add an LLM prose summary to each topic
            (needs a local chat model).</li>
        <li><code>--exclude "REGEX"</code> — drop private/sensitive memories.</li>
      </ul>
      <p style="margin-top:1.4rem;">Full guide:
         <a href="https://github.com/skynetcmd/m3-memory/blob/main/docs/WIKI.md"
            target="_blank" style="color:hsl(180,100%,65%);">docs/WIKI.md</a>.</p>
    </div>
    """


@app.get("/graph", response_class=HTMLResponse)
async def get_graph_window(request: Request):
    """Standalone full-window Interactive Knowledge Graph.

    Opened via the 'Open in window' button on the Graph Explorer. Renders the
    same canvas force-graph, full-viewport, from the same /api/graph data — so it
    lives in its own resizable browser window with no surrounding dashboard chrome.
    Self-contained (its own compact renderer) so it doesn't couple to the busy
    index page's script.
    """
    selected_db = request.cookies.get("selected_db", "main")
    set_active_db_env(selected_db)
    return HTMLResponse(content=_GRAPH_WINDOW_HTML, status_code=200,
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/api/graph/node/{node_id}", response_class=JSONResponse)
async def get_graph_node(node_id: str, request: Request):
    """Detail for one graph node (entity): its fields + connected relationships.

    Powers the standalone graph window's sidebar. Backend-agnostic (dialect
    placeholder + _db). Returns {found, id, name, type, mentions, neighbors:[...]}.
    Best-effort — missing tables/rows yield a minimal payload, never an error.
    """
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)
    out: dict = {"found": False, "id": node_id, "name": "", "type": "", "neighbors": []}
    try:
        ph = _active_backend().dialect().placeholder
        from m3_sdk import active_database
        with active_database(selected_db_path):
            with _db() as db:
                if table_exists(db, "entities"):
                    row = db.execute(
                        f"SELECT canonical_name, entity_type FROM entities WHERE id = {ph()}",
                        (node_id,),
                    ).fetchone()
                    if row:
                        out["found"] = True
                        out["name"] = row["canonical_name"]
                        out["type"] = row["entity_type"]
                if table_exists(db, "entity_relationships"):
                    # Outgoing + incoming edges, with the other end's name.
                    rels = db.execute(
                        f"""
                        SELECT er.predicate, er.from_entity, er.to_entity,
                               ef.canonical_name AS from_name, et.canonical_name AS to_name
                        FROM entity_relationships er
                        LEFT JOIN entities ef ON ef.id = er.from_entity
                        LEFT JOIN entities et ON et.id = er.to_entity
                        WHERE er.from_entity = {ph()} OR er.to_entity = {ph()}
                        LIMIT 50
                        """,
                        (node_id, node_id),
                    ).fetchall()
                    for r in rels:
                        if r["from_entity"] == node_id:
                            out["neighbors"].append(
                                {"dir": "→", "predicate": r["predicate"], "name": r["to_name"] or r["to_entity"]})
                        else:
                            out["neighbors"].append(
                                {"dir": "←", "predicate": r["predicate"], "name": r["from_name"] or r["from_entity"]})
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    # Same no-store rationale as /api/graph: the payload varies by selected_db but
    # the URL is fixed, so without this the browser could serve a stale node detail
    # after a DB switch.
    return JSONResponse(content=out, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache", "Expires": "0"})


@app.get("/health", response_class=HTMLResponse)
async def get_health(request: Request):
    """System Health page — backend identity, stores, CDW sync, pipeline, verdict.

    Renders a self-contained page (same header/CSS as the other tabs) whose body
    is the health panel. Kept dependency-light: the panel HTML is built inline
    from the structured collect_health() snapshot so a new backend needs no change.
    """
    selected_db = request.cookies.get("selected_db", "main")
    set_active_db_env(selected_db)
    db_selector_html = build_db_selector_html(selected_db)
    header = HEADER_HTML.format(explorer_active="", browse_active="", audit_active="", health_active="active", wiki_active="", db_selector_html=db_selector_html)
    # Render the page shell INSTANTLY with a skeleton, then fetch the real panel
    # from /api/health on load. collect_health() pings the inference endpoint
    # (~2-3s) + pipeline stats (~0.7s), so building the panel inline blocked the
    # whole page for ~4s. The skeleton makes the page appear immediately and each
    # section shows "gathering data…" until the fetch fills it in.
    skeleton = _health_skeleton_html()
    content = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>M3 System Health</title>"
        f"<style>{STYLE_CSS}</style></head><body>"
        f"{header}"
        "<div style='max-width: 1100px; margin: 0 auto; padding: 1.5rem;'>"
        "<div class='page-hero'>"
        "<div class='hero-icon'>🩺</div>"
        "<div class='hero-text'>"
        "<div class='hero-title'>System Health</div>"
        "<div class='hero-sub'>Backend identity, store integrity, inference status, and governor load — the same signals as <code>m3 doctor</code>.</div>"
        "</div></div>"
        f"<div id='healthPanel'>{skeleton}</div>"
        "</div>"
        # Fetch the real panel immediately, then re-fetch every 5s (governor
        # load — CPU/RAM/GPU — is volatile). The INITIAL fetch always runs so the
        # skeleton fills in even on a background tab; only the recurring poll skips
        # while hidden (don't waste cycles polling an unfocused tab).
        "<script>"
        "(function(){"
        " async function fetchPanel(){"
        "  try{var r=await fetch('/api/health',{cache:'no-store'});"
        "   if(r.ok)document.getElementById('healthPanel').innerHTML=await r.text();}catch(e){}"
        " }"
        " fetchPanel();"                   # initial fill — regardless of visibility
        " setInterval(function(){ if(!document.hidden) fetchPanel(); },5000);"
        "})();"
        "</script>"
        "</body></html>"
    )
    return HTMLResponse(content=content, status_code=200, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


def _health_skeleton_html() -> str:
    """The instant placeholder shown while collect_health() gathers (~3s).

    Mirrors the real panel's card structure so the page doesn't reflow much, and
    labels each slow section 'gathering data…' with a shimmer, so the user sees a
    live, loading page instead of a blank ~4s wait.
    """
    def card(title: str, note: str) -> str:
        return (
            '<div class="m3-card" style="margin-bottom:1.5rem;">'
            f'<div class="m3-card-title">{title}'
            '<span class="gathering">gathering data'
            '<span class="gathering-dots"></span></span></div>'
            '<div class="skeleton-lines">'
            '<div class="skel-line"></div>'
            '<div class="skel-line" style="width:82%;"></div>'
            '<div class="skel-line" style="width:64%;"></div>'
            '</div>'
            f'<div style="color:hsl(210,12%,52%);font-size:.8rem;margin-top:.4rem;">{note}</div>'
            '</div>'
        )
    return (
        '<style>'
        '.gathering{float:right;font-size:.8rem;font-weight:500;color:var(--m3-neon-cyan);'
        'font-family:"Fira Code",monospace;opacity:.9;}'
        '.gathering-dots::after{content:"";animation:gather-dots 1.4s steps(4,end) infinite;}'
        '@keyframes gather-dots{0%{content:"";}25%{content:".";}50%{content:"..";}75%{content:"...";}}'
        '.skeleton-lines{display:flex;flex-direction:column;gap:.6rem;margin:.4rem 0;}'
        '.skel-line{height:12px;border-radius:6px;'
        'background:linear-gradient(90deg,hsla(210,20%,30%,.25) 25%,hsla(180,100%,50%,.15) 50%,hsla(210,20%,30%,.25) 75%);'
        'background-size:200% 100%;animation:skel-shimmer 1.6s ease-in-out infinite;}'
        '@keyframes skel-shimmer{0%{background-position:200% 0;}100%{background-position:-200% 0;}}'
        '@media (prefers-reduced-motion: reduce){'
        '.skel-line{animation:none;}.gathering-dots::after{animation:none;content:"…";}}'
        '</style>'
        '<div class="m3-card" style="margin-bottom:1.5rem;border-left:3px solid var(--m3-neon-cyan);">'
        '<div style="font-size:1.1rem;font-weight:600;color:var(--m3-neon-cyan);">'
        '● Gathering system health'
        '<span class="gathering-dots"></span></div>'
        '<div style="color:hsl(210,15%,70%);margin-top:.35rem;">'
        'Probing the inference endpoint, stores, and pipeline — this takes a few '
        'seconds. Sections fill in below as data arrives.</div></div>'
        + card("System load (Governor)", "CPU / RAM / GPU pacing")
        + card("Storage backend", "core · chatlog · files stores")
        + card("Inference backend (LLM/SLM)", "endpoint + model probe (slowest)")
        + card("Cognitive pipeline", "queue depths + drain telemetry")
    )


@app.get("/api/health", response_class=HTMLResponse)
async def get_health_partial(request: Request):
    """HTMX partial: just the health panel (for in-place refresh)."""
    return HTMLResponse(content=_render_health_panel(), status_code=200,
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


def _render_health_panel() -> str:
    """Build the digestible System Health HTML from collect_health().

    Presentation only — all data comes from dashboard.health.collect_health(),
    the single source shared with the doctor's stats. Color-coded status pills,
    store cards, CDW watermarks, and the overall verdict; never raises.
    """
    try:
        from dashboard.health import collect_health
        h = collect_health()
    except Exception as e:  # noqa: BLE001 — the panel must render even if a probe dies
        return (f'<div class="memory-card"><h3 style="color: var(--m3-neon-amber);">'
                f'Health data unavailable</h3><p>{e}</p></div>')

    def esc(s: object) -> str:
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def ts(s: object) -> str:
        """Escape a dual-time string and render the trailing '(…Z)' Zulu portion
        in muted light-grey so the local time reads primary and the UTC is a quiet
        secondary."""
        raw = esc(s)
        i = raw.rfind(" (")
        if i != -1 and raw.endswith(")"):
            local, zulu = raw[:i], raw[i + 1:]
            return f'{local} <span style="color:hsl(210,12%,55%);">{zulu}</span>'
        return raw

    # Report time (current time of this snapshot) — noted up top, dual local/Zulu.
    generated = ts(h.get("generated_at", "—"))

    # Overall status pill. Use the USER-FACING label/tone (never the scary word
    # "DEGRADED" for a mere throttle/perf state), WITH the specific reasons.
    v = h.get("verdict", {})
    label = v.get("label", (v.get("verdict", "unknown") or "unknown").upper())
    tone = v.get("tone", "warn")
    color = {"ok": "var(--m3-neon-emerald)", "warn": "var(--m3-neon-amber)",
             "bad": "#ff5c6c"}.get(tone, "hsl(210,15%,55%)")
    icon = {"ok": "●", "warn": "▲", "bad": "✕"}.get(tone, "?")
    reasons = v.get("reasons") or []
    reasons_html = ""
    if tone != "ok" and reasons:
        items = "".join(f'<li style="margin:0.15rem 0;">{esc(r)}</li>' for r in reasons)
        reasons_html = (
            f'<div style="margin-top:0.6rem;color:hsl(210,15%,75%);font-size:0.85rem;">'
            f'<strong style="color:{color};">Why:</strong>'
            f'<ul style="margin:0.3rem 0 0 1.1rem;padding:0;">{items}</ul></div>')
    elif tone == "ok":
        reasons_html = ('<div style="margin-top:0.4rem;color:var(--m3-neon-emerald);'
                        'font-size:0.85rem;">All subsystems nominal.</div>')

    parts = [
        f'<div style="color:hsl(210,15%,55%);font-size:0.75rem;margin-bottom:0.9rem;">'
        f'report time: {generated}</div>',
        f'<div class="memory-card" style="border-left:4px solid {color};">'
        f'<span style="color:{color};font-weight:700;font-size:1.1rem;">{icon} {esc(label)}</span>'
        f'<span style="margin-left:0.75rem;color:hsl(210,15%,70%);">{esc(v.get("headline",""))}</span>'
        f'{reasons_html}</div>',
    ]

    # System load (Governor) — placed RIGHT AFTER the verdict, before the backend
    # section: it's the load context that explains a THROTTLED/HALTED verdict.
    pl = h.get("pipeline", {})
    gov = pl.get("governor")
    if gov and gov.get("available"):
        init = gov.get("initial_threshold", 80)
        limit = gov.get("limit_threshold", 90)
        mode = str(gov.get("mode", "?")).upper()
        # Pacing-mode label keeps the governor-state color (grey non-blocking /
        # amber throttling / red halting) so the overall status is still obvious.
        mode_color = {"THROTTLED": "var(--m3-neon-amber)",
                      "HALTED": "#ff5c6c"}.get(mode, "hsl(210,15%,60%)")

        def _meter(label, val):
            """Each meter is colored INDEPENDENTLY by its OWN value vs the
            governor thresholds: green while free (< throttle), amber while in the
            throttle band, red at/above the halt band. So you see at a glance
            WHICH resource is free / throttling / halting."""
            try:
                v = float(val or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v >= limit:
                c = "#ff5c6c"                 # halting
            elif v >= init:
                c = "var(--m3-neon-amber)"    # throttling
            else:
                c = "var(--m3-neon-emerald)"  # free
            return (
                f'<div style="margin:0.3rem 0;font-family:\'Fira Code\',monospace;font-size:0.8rem;">'
                f'<div style="display:flex;justify-content:space-between;">'
                f'<span style="color:hsl(210,15%,70%);">{label}</span>'
                f'<span style="color:{c};">{v:.0f}%</span></div>'
                f'<div style="height:5px;background:hsla(222,22%,12%,0.9);border-radius:3px;overflow:hidden;margin-top:2px;">'
                f'<div style="height:100%;width:{min(v,100):.0f}%;background:{c};"></div></div></div>')

        parts.append(
            '<div class="memory-card"><h3 style="color:var(--m3-neon-cyan);margin-bottom:0.5rem;">'
            'System load (Governor)</h3>'
            f'<div style="margin-bottom:0.4rem;font-size:0.82rem;">pacing mode: '
            f'<strong style="color:{mode_color};">{esc(mode)}</strong> '
            f'<span style="color:hsl(210,15%,50%);">(throttle ≥ {init}%, halt ≥ {limit}%)</span> · '
            f'thermal: {esc(gov.get("thermal","?"))}</div>'
            + _meter("CPU", gov.get("cpu")) + _meter("RAM", gov.get("ram")) + _meter("GPU", gov.get("gpu"))
            + '<div style="color:hsl(210,15%,50%);font-size:0.72rem;margin-top:0.4rem;">'
            'The governor paces background work by host load so it never competes with you.</div></div>')

    # Backend + stores. Tall-man / correct casing for the engine name.
    b = h.get("backend", {})
    try:
        from dashboard.health import _backend_display
        backend_name = _backend_display(b.get("backend", "?"))
    except Exception:  # noqa: BLE001
        backend_name = str(b.get("backend", "?"))
    parts.append(f'<div class="memory-card"><h3 style="color:var(--m3-neon-cyan);margin-bottom:0.5rem;">'
                 f'Database backend: {esc(backend_name)}</h3>')
    if b.get("note"):
        parts.append(f'<p style="color:var(--m3-neon-amber);">{esc(b["note"])}</p>')
    for st in b.get("stores", []):
        present = st.get("present")
        pill = ('<span style="color:var(--m3-neon-emerald);">present</span>' if present
                else '<span style="color:hsl(210,15%,55%);">absent</span>')
        rows = st.get("rows")
        rows_txt = f"{rows:,} rows" if isinstance(rows, int) else "—"
        shared = " (shared with core)" if st.get("shared") else ""
        parts.append(
            f'<div style="margin:0.4rem 0;font-family:\'Fira Code\',monospace;font-size:0.82rem;">'
            f'<strong style="color:#fff;">{esc(st.get("label"))}</strong> · {pill}{shared}<br>'
            f'<span style="color:hsl(210,15%,60%);">{esc(st.get("path"))}</span><br>'
            f'<span style="color:var(--m3-neon-cyan);">{rows_txt}</span> · '
            f'last updated: {ts(st.get("last_updated","—"))}</div>')
    parts.append("</div>")

    # Inference backend (LLM/SLM) — the cognitive loop / entity extraction /
    # enrichment all call an LLM selected by m3's failover chain. We verify the
    # SAME way m3 does: walk llm_failover.LLM_ENDPOINTS in order and confirm the
    # landing endpoint can actually TAKE AN INFERENCE REQUEST (cached real
    # completion, not just a /v1/models listing). Report the whole chain + which
    # hops failed, so a stall or an active failover has a named cause instead of a
    # silent "HEALTHY". Provider-agnostic, no hardcoded port.
    inf = h.get("inference") or {}
    inf_status = inf.get("status", "none_configured")
    inf_map = {
        "ok":              ("var(--m3-neon-emerald)", "●", "SERVING INFERENCE"),
        "failover_active": ("var(--m3-neon-amber)",   "▲", "FAILOVER ACTIVE"),
        "no_model":        ("#ff5c6c",                "✕", "NO MODEL LOADED"),
        "auth_failed":     ("#ff5c6c",                "✕", "AUTH REJECTED"),
        "down":            ("#ff5c6c",                "✕", "DOWN"),
        "unknown":         ("var(--m3-neon-amber)",   "▲", "REACHABLE (UNVERIFIED)"),
        "none_configured": ("var(--m3-neon-amber)",   "▲", "NOT CONFIGURED"),
    }
    inf_color, inf_icon, inf_label = inf_map.get(inf_status, ("hsl(210,15%,55%)", "?", inf_status.upper()))
    # Cloud landed-endpoint reads "REACHABLE" (a ping), not "SERVING INFERENCE".
    _landed_cloud = any(hp.get("cloud") and hp.get("ok") for hp in (inf.get("chain") or []))
    if inf_status == "ok" and _landed_cloud:
        inf_label = "REACHABLE"
    cached_note = (' <span style="color:hsl(210,15%,45%);font-size:0.72rem;">(cached)</span>'
                   if inf.get("cached") else "")
    parts.append(
        '<div class="memory-card"><h3 style="color:var(--m3-neon-cyan);margin-bottom:0.5rem;">'
        'Inference backend (LLM/SLM)</h3>'
        f'<div style="font-size:0.9rem;margin-bottom:0.35rem;">'
        f'<span style="color:{inf_color};font-weight:700;">{inf_icon} {esc(inf_label)}</span>'
        + (f' <span style="color:hsl(210,15%,70%);">· {esc(inf.get("backend"))}</span>'
           if inf.get("backend") else "")
        + cached_note
        + '</div>')
    if inf.get("expected_url"):
        model_txt = ""
        if inf.get("model_id"):
            model_txt = (f' · model: <span style="color:var(--m3-neon-cyan);">'
                         f'{esc(inf["model_id"])}</span>')
        verb = "serving at" if inf_status in ("ok", "failover_active") else "expected at"
        parts.append(
            f'<div style="font-family:\'Fira Code\',monospace;font-size:0.8rem;color:hsl(210,15%,60%);">'
            f'{verb} <span style="color:#fff;">{esc(inf.get("expected_url"))}</span>{model_txt}</div>')
    # Failover chain — every hop in order, with serve/fail + the real reason. Shown
    # whenever there's more than one endpoint OR failover is active, so the operator
    # sees exactly which preferred backend failed and why.
    chain = inf.get("chain") or []
    if len(chain) > 1 or inf_status == "failover_active":
        parts.append('<div style="margin-top:0.4rem;font-size:0.73rem;color:hsl(210,15%,50%);">'
                     'failover chain (tried in order):</div>')
        for idx, hop in enumerate(chain):
            is_cloud = hop.get("cloud")
            if hop.get("ok"):
                # Cloud is verified by a ping (reachable + key accepted), not a
                # local 'serving inference' state — word it honestly per kind.
                ok_txt = "✓ reachable (ping OK)" if is_cloud else "✓ serving inference"
                hc, htxt = "var(--m3-neon-emerald)", ok_txt + (
                    f" ({esc(hop.get('model_id'))})" if hop.get("model_id") else "")
            else:
                hstat = hop.get("status", "down")
                hc = "#ff5c6c" if hstat in ("down", "no_model", "auth_failed") else "var(--m3-neon-amber)"
                # For cloud, "no_model" is impossible; auth_failed reads as a key issue.
                htxt = f"✕ {esc(hstat)}" + (f" — {esc(hop.get('detail'))}" if hop.get("detail") else "")
            arrow = "" if idx == 0 else '<span style="color:hsl(210,15%,40%);">↳ </span>'
            parts.append(
                f'<div style="font-family:\'Fira Code\',monospace;font-size:0.76rem;margin:0.15rem 0 0.15rem {0 if idx==0 else 0.8}rem;">'
                f'{arrow}<span style="color:hsl(210,15%,65%);">{esc(hop.get("backend"))} {esc(hop.get("url"))}</span> · '
                f'<span style="color:{hc};">{htxt}</span></div>')
    if inf.get("remedy") and inf_status not in ("ok",):
        parts.append(
            f'<div style="margin-top:0.45rem;color:hsl(35,90%,72%);font-size:0.8rem;">'
            f'{esc(inf.get("remedy"))}</div>')
    parts.append("</div>")

    # CDW sync — TABLE format (direction | last sync), easier to scan.
    cdw = h.get("cdw")
    if cdw:
        parts.append('<div class="memory-card"><h3 style="color:var(--m3-neon-cyan);margin-bottom:0.5rem;">'
                     'Data warehouse (CDW) sync</h3>'
                     f'<div style="font-family:\'Fira Code\',monospace;font-size:0.78rem;color:hsl(210,15%,60%);margin-bottom:0.25rem;">'
                     f'{esc(cdw.get("dsn"))}</div>'
                     f'<div style="font-size:0.72rem;color:hsl(210,15%,50%);margin-bottom:0.5rem;">'
                     f'as of report time: {generated}</div>')
        wm = cdw.get("watermarks") or []
        if wm:
            th = ('padding:0.3rem 0.6rem;text-align:left;border-bottom:1px solid '
                  'var(--m3-border-glass);color:hsl(210,15%,60%);font-weight:600;')
            td = ("padding:0.28rem 0.6rem;border-bottom:1px solid rgba(120,140,160,0.12);"
                  "font-family:'Fira Code',monospace;font-size:0.8rem;")
            body = "".join(
                f'<tr><td style="{td}color:#fff;">{esc(w.get("direction"))}</td>'
                f'<td style="{td}color:var(--m3-neon-cyan);">{ts(w.get("last_sync"))}</td></tr>'
                for w in wm)
            parts.append(
                '<table style="width:100%;border-collapse:collapse;font-size:0.8rem;">'
                f'<thead><tr><th style="{th}">Direction</th>'
                f'<th style="{th}">Last sync (local <span style="color:hsl(210,12%,55%);">/ UTC</span>)</th></tr></thead>'
                f'<tbody>{body}</tbody></table>')
        else:
            parts.append('<div style="color:hsl(210,15%,55%);">no sync recorded yet</div>')
        parts.append("</div>")

    # (System load / Governor card is rendered above, right after the verdict.)

    # Processing pipeline — queue depth + plain-language status (is 'pending' ok?).
    pipes = pl.get("pipelines") or []
    if pipes:
        parts.append('<div class="memory-card"><h3 style="color:var(--m3-neon-cyan);margin-bottom:0.5rem;">'
                     'Processing pipeline</h3>'
                     '<div style="color:hsl(210,15%,55%);font-size:0.75rem;margin-bottom:0.5rem;">'
                     'A queue at 0 is <em>drained</em> (normal). Items queued while the worker is '
                     'producing is normal under load; a backlog with an idle worker wants attention.</div>')
        tone_color = {"ok": "var(--m3-neon-emerald)", "warn": "var(--m3-neon-amber)"}
        for p in pipes:
            label = esc(p.get("label", "queue"))
            qlen = p.get("queue_len", 0)
            status = esc(p.get("status", ""))
            c = tone_color.get(p.get("tone"), "hsl(210,15%,70%)")
            eta = esc(p.get("eta_human", ""))
            eta_txt = f' · drain ETA: {eta}' if eta and qlen else ""
            parts.append(
                f'<div style="font-family:\'Fira Code\',monospace;font-size:0.82rem;margin:0.25rem 0;">'
                f'<strong style="color:#fff;">{label}</strong>: '
                f'<span style="color:var(--m3-neon-cyan);">{esc(qlen)} queued</span> · '
                f'<span style="color:{c};">{status}</span>{eta_txt}</div>')
        parts.append("</div>")

    parts.append('<div style="text-align:center;color:hsl(210,15%,45%);font-size:0.72rem;margin-top:1rem;">'
                 f'report time: {generated} · '
                 '<a href="/api/health" hx-get="/api/health" hx-target="closest div" '
                 'style="color:var(--m3-neon-cyan);">refresh</a></div>')
    return "\n".join(parts)


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

    # Query Main DB (backend-blind; no file-existence guard — see module note)
    try:
        with db_readonly(main_db) as conn:
            total_mems = conn.execute("SELECT COUNT(*) FROM memory_items WHERE COALESCE(is_deleted, 0) = 0 AND type != 'chat_log'").fetchone()[0]

            if table_exists(conn, "entities"):
                total_ents = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]

            if table_exists(conn, "entity_relationships"):
                total_rels = conn.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0]

            # Entity-extraction backlog = memories still needing entities (the
            # worker is SCAN-driven, not queue-driven; the entity_extraction_queue
            # table is a done-marker log, not a work queue). Use the shared
            # queue_stats helper so this card and System Health agree.
            try:
                from dashboard.queue_stats import _entity_backlog_count
                queue_len = _entity_backlog_count(conn)
            except Exception:  # noqa: BLE001
                queue_len = 0
    except Exception as e:
        print(f"Failed to query Main DB stats: {e}", flush=True)

    # Query Chatlog DB (may be the same store as main, or a separate one)
    try:
        chatlog_target = main_db if chatlog_db == main_db else chatlog_db
        with db_readonly(chatlog_target) as conn:
            chatlog_turns = conn.execute("SELECT COUNT(*) FROM memory_items WHERE type='chat_log'").fetchone()[0]
            chatlog_sessions = conn.execute("SELECT COUNT(DISTINCT COALESCE(NULLIF(conversation_id, ''), 'legacy')) FROM memory_items WHERE type='chat_log'").fetchone()[0]
    except Exception as e:
        print(f"Failed to query Chatlog DB stats: {e}", flush=True)

    # Query Files DB (backend-blind)
    try:
        with db_readonly(files_db) as conn:
            if table_exists(conn, "leaves"):
                file_chunks = conn.execute("SELECT COUNT(*) FROM leaves").fetchone()[0]
                # Count deduplicated non-blank lines in active leaves
                dedup_leaves = conn.execute("SELECT text FROM leaves WHERE superseded_by IS NULL GROUP BY text_sha256").fetchall()
                file_lines = sum(sum(1 for line in (leaf[0] or "").splitlines() if line.strip()) for leaf in dedup_leaves)
            if table_exists(conn, "file_nodes"):
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
        <div class="metric-label">Structured/Core Memories</div>
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
    <div class="metric-card {highlight_main}" style="{style_main} cursor: pointer;"
         onclick="window.location.href='/health'" title="View queue drain + system load on System Health">
        <div class="metric-value" style="color: var(--m3-neon-amber);">{queue_len}</div>
        <div class="metric-label">Queue Pending&nbsp;<span style="font-size:0.7rem;color:var(--m3-neon-cyan);">→ Health</span></div>
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
    from dashboard.queue_stats import collect_governor, collect_pipeline_stats
    from m3_sdk import resolve_db_path

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

    # The graph JSON VARIES by the selected_db cookie, but the URL ("/api/graph")
    # is constant. Without an explicit no-store, the browser caches the first
    # response and serves it again after a DB switch + reload — so switching the
    # DB selector appeared to "do nothing" to the graph even though the backend
    # returned different data. The /graph WINDOW handler already sets no-store for
    # this reason; the data endpoint that actually differs was the one missing it.
    _no_cache = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                 "Pragma": "no-cache", "Expires": "0"}

    nodes = []
    links = []

    if selected_db == "files":
        try:
            with db_readonly(selected_db_path) as conn:
                # Draw File Ingest Nodes (index by position — backend-blind, no row_factory)
                files = conn.execute("SELECT uuid, filename, filetype FROM file_nodes LIMIT 40").fetchall()
                for f in files:
                    nodes.append({
                        "id": f[0],
                        "name": f[1],
                        "type": "file"
                    })
                # Draw Chunk Leaf Nodes and link them to Files
                leaves = conn.execute("SELECT uuid, file_node, division_id FROM leaves LIMIT 100").fetchall()
                for l in leaves:
                    nodes.append({
                        "id": l[0],
                        "name": f"Chunk {l[2]}",
                        "type": "chunk"
                    })
                    links.append({
                        "source": l[1],
                        "target": l[0],
                        "predicate": "contains"
                    })
        except Exception as e:
            print(f"Failed to query files graph: {e}", flush=True)
        return JSONResponse({"nodes": nodes, "links": links}, headers=_no_cache)

    try:
        from m3_sdk import active_database
        with active_database(selected_db_path):
            with _db() as db:
                if table_exists(db, "entities"):
                    rows = db.execute("SELECT id, canonical_name, entity_type FROM entities LIMIT 150").fetchall()
                    for r in rows:
                        nodes.append({
                            "id": r["id"],
                            "name": r["canonical_name"],
                            "type": r["entity_type"]
                        })

                if table_exists(db, "entity_relationships"):
                    rows = db.execute("SELECT from_entity, to_entity, predicate FROM entity_relationships LIMIT 250").fetchall()
                    for r in rows:
                        links.append({
                            "source": r["from_entity"],
                            "target": r["to_entity"],
                            "predicate": r["predicate"]
                        })
    except Exception as e:
        print(f"Failed to load graph database items: {e}", flush=True)

    return JSONResponse({"nodes": nodes, "links": links}, headers=_no_cache)


@app.get("/api/search", response_class=HTMLResponse)
async def search_memories(request: Request, q: str = "", ignore_case: int = 1):
    """Search with the advanced grammar (+/-/"phrase"), Memory Browser or files.

    The ranked search runs on the POSITIVE terms/phrases (so results stay
    relevance-ordered); the full include/exclude grammar is then applied as a
    hard filter. ``ignore_case`` (default 1) toggles case-insensitivity.
    """
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)
    ic = bool(ignore_case)
    grammar = parse_query_grammar(q)
    # The text handed to the ranked search: positive terms/phrases (falls back to
    # the raw query if the grammar produced no positives, e.g. a pure exclusion).
    search_text = grammar["positive_text"] or q

    if selected_db == "files":
        if not q.strip():
            return '<p style="color: hsl(210, 15%, 65%); text-align: center; padding: 2rem 0;">Type in search bar to scan files and chunk indexes.</p>'
        try:
            from files_memory.search import files_search
            hits = files_search(search_text, limit=40, db_path=selected_db_path)
            # Apply the include/exclude grammar over chunk text + filename.
            hits = [h for h in hits
                    if matches_query_grammar(f"{getattr(h,'text','')} {getattr(h,'filename','')}", grammar, ic)][:15]
            if not hits:
                return f'<p style="color: hsl(210, 15%, 65%); text-align: center; padding: 2rem 0;">No matching indexed file chunks found for "{_h(q)}".</p>'

            cards = []
            for hit in hits:
                cards.append(f"""
                <div class="memory-card">
                    <div class="memory-header">
                        <div>
                            <span class="m3-badge badge-fact" style="background: hsla(120, 100%, 45%, 0.1); color: var(--m3-neon-emerald); border: 1px solid rgba(16, 185, 129, 0.25);">FILE CHUNK</span>
                            <span style="font-family: 'Outfit', sans-serif; font-weight: 500; font-size: 0.95rem; margin-left: 0.5rem; color:#fff;">{_h(hit.filename)}</span>
                        </div>
                        <span class="memory-id">{_h(hit.leaf_uuid[:8])}</span>
                    </div>
                    <div class="memory-content" style="margin-bottom: 0.75rem;">{_h(hit.text)}</div>
                    <div style="font-size: 0.75rem; color: hsl(210, 10%, 65%); display: flex; gap: 1rem; border-top: 1px solid var(--m3-border-glass); padding-top: 0.5rem;">
                        <span>Path: <code style="font-family: 'Fira Code', monospace; color: var(--m3-neon-cyan);">{_h(hit.path)}</code></span>
                        <span>Corpus: <code style="font-family: 'Fira Code', monospace; color: var(--m3-neon-purple);">{_h(hit.corpus_id or 'default')}</code></span>
                        <span>Score: <strong style="color: var(--m3-neon-amber);">{hit.score:.4f}</strong></span>
                    </div>
                </div>
                """)
            return "\n".join(cards)
        except Exception as e:
            return f'<p style="color: var(--m3-neon-amber); text-align: center; padding: 2rem 0;">Error scanning files index: {_h(str(e))}</p>'

    if not q.strip():
        return '<p style="color: hsl(210, 15%, 65%); text-align: center; padding: 2rem 0;">Type in search bar to explore FTS5 & Vector similarity explain graphs.</p>'

    try:
        from m3_sdk import active_database
        with active_database(selected_db_path):
            results = await memory_search_scored_impl(
                query=search_text,
                k=40,
                explain=True,
                extra_columns=["metadata_json", "conversation_id", "valid_from", "valid_to", "user_id"]
            )
            # Apply the include/exclude grammar over title + content as a hard gate.
            results = [(s, it) for (s, it) in results
                       if matches_query_grammar(f"{it.get('title','')} {it.get('content','')}", grammar, ic)][:15]

            if not results:
                return f'<p style="color: hsl(210, 15%, 65%); text-align: center; padding: 2rem 0;">No matching indexed memories found for "{_h(q)}".</p>'

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
                            <span class="m3-badge {badge_class}">{_h(item.get('type', 'note'))}</span>
                            <span style="font-family: 'Outfit', sans-serif; font-weight: 500; font-size: 0.95rem; margin-left: 0.5rem; color:#fff;">{_h(item.get('title') or 'Untitled Memory')}</span>
                        </div>
                        <span class="memory-id">{_h(item.get('id')[:8])}</span>
                    </div>
                    <div class="memory-content">{_h(item.get('content') or '')}</div>
                    {explain_html}
                </div>
                """)
            return "\n".join(cards)

    except Exception as e:
        return f'<p style="color: var(--m3-neon-amber); text-align: center; padding: 2rem 0;">Error scanning indices: {_h(str(e))}</p>'


@app.get("/api/kb", response_class=HTMLResponse)
async def get_kb_cards(request: Request, q: str = "", type: str = "", limit: int = 50):
    """Replicates cli_kb_browse.py rank search or dynamic file logs into high-fidelity web cards."""
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)

    if selected_db == "files":
        try:
            # Positional columns (backend-blind: open_readonly yields tuple rows,
            # no row_factory) — order matches the SELECT below.
            # 0=uuid 1=filename 2=path 3=filetype 4=size_bytes 5=corpus_id 6=created_at
            ph = _active_backend().dialect().placeholder
            query = "SELECT uuid, filename, path, filetype, size_bytes, corpus_id, created_at FROM file_nodes"
            params = []
            if q.strip():
                query += f" WHERE filename LIKE {ph()} OR path LIKE {ph()}"
                params += [f"%{q}%", f"%{q}%"]
            query += f" ORDER BY created_at DESC LIMIT {ph()}"
            params.append(str(int(limit)))

            with db_readonly(selected_db_path) as conn:
                rows = conn.execute(query, params).fetchall()

            total = len(rows)
            if total == 0:
                return '<p style="text-align: center; color: hsl(210, 15%, 65%); padding: 3rem 0;">No matching ingested files found.</p>'

            cards = []
            for idx, row in enumerate(rows, 1):
                size_kb = round((row[4] or 0) / 1024, 1)
                cards.append(f"""
                <div class="memory-card">
                    <div class="memory-header">
                        <div>
                            <span style="font-family: 'Outfit', sans-serif; font-weight: 600; font-size: 0.85rem; color: var(--m3-neon-emerald);">#{idx:03d}/{total}</span>
                            <span class="m3-badge badge-sys" style="margin-left: 0.5rem;">{_h(row[3] or 'file')}</span>
                            <span style="font-family: 'Fira Code', monospace; font-size: 0.75rem; color: hsl(210, 15%, 50%); margin-left: 0.75rem;">size: {size_kb} KB &middot; corpus: {_h(row[5] or 'default')}</span>
                        </div>
                    </div>
                    <h3 style="font-family: 'Outfit', sans-serif; font-size: 1.15rem; font-weight: 600; color: #fff; margin-bottom: 0.5rem;">{_h(row[1])}</h3>
                    <div style="font-family: 'Fira Code', monospace; font-size: 0.7rem; color: hsl(210, 15%, 55%); margin-bottom: 0.75rem;">
                        uuid: {_h(row[0])} &middot; created: {_h(row[6])}
                    </div>
                    <div class="memory-content" style="white-space: pre-wrap; font-family: 'Fira Code', monospace; font-size: 0.8rem; color: var(--m3-neon-cyan);">path: {_h(row[2])}</div>
                </div>
                """)
            return "\n".join(cards)
        except Exception as e:
            return f'<p style="color: var(--m3-neon-amber); text-align: center; padding: 2rem 0;">Error scanning files DB: {_h(str(e))}</p>'

    try:
        query = """
            SELECT id, type, title, content, metadata_json, importance,
                   origin_device, change_agent, created_at, updated_at
            FROM memory_items
            WHERE is_deleted = 0
        """
        params = []
        ph = _active_backend().dialect().placeholder  # ?/%s, backend-agnostic

        if type:
            query += f" AND type = {ph()}"
            params.append(type)

        if q.strip():
            query += f" AND (LOWER(title) LIKE LOWER({ph()}) OR LOWER(content) LIKE LOWER({ph()}))"
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
                           "".join([f'<span class="m3-tag">{_h(t)}</span>' for t in tags]) + '</div>'

            extras_html = ""
            if extras:
                extras_list = [f'<span style="margin-right: 0.75rem; color: hsl(210, 10%, 65%);"><strong style="color: hsl(210, 10%, 80%);">{_h(k)}:</strong> {_h(v)}</span>' for k, v in extras.items()]
                extras_html = '<div style="font-family: \'Fira Code\', monospace; font-size: 0.75rem; margin-top: 0.5rem; display: flex; flex-wrap: wrap;">' + \
                              "".join(extras_list) + '</div>'

            cards.append(f"""
            <div class="memory-card">
                <div class="memory-header">
                    <div>
                        <span style="font-family: 'Outfit', sans-serif; font-weight: 600; font-size: 0.85rem; color: var(--m3-neon-cyan);">#{idx:03d}/{total}</span>
                        <span class="m3-badge {badge_class}" style="margin-left: 0.5rem;">{_h(row['type'])}</span>
                        <span style="font-family: 'Fira Code', monospace; font-size: 0.75rem; color: hsl(210, 15%, 50%); margin-left: 0.75rem;">{_h(row['origin_device'] or '?')} &middot; {_h(row['change_agent'] or '?')}</span>
                    </div>
                    <div class="m3-progress-container" title="Importance score: {importance:.2f}">
                        <div class="m3-progress-bar">
                            <div class="m3-progress-fill" style="width: {importance * 100}%; background-color: {bar_color}; box-shadow: 0 0 8px {bar_color};"></div>
                        </div>
                        <span>{importance:.2f}</span>
                    </div>
                </div>
                <h3 style="font-family: 'Outfit', sans-serif; font-size: 1.15rem; font-weight: 600; color: #fff; margin-bottom: 0.5rem;">{_h(row['title'] or 'Untitled Entry')}</h3>
                <div style="font-family: 'Fira Code', monospace; font-size: 0.7rem; color: hsl(210, 15%, 55%); margin-bottom: 0.75rem;">
                    id: {_h(row['id'])} &middot; created: {_h((row['created_at'] or '')[:19])} &middot; updated: {_h((row['updated_at'] or '')[:19])}
                </div>
                <div class="memory-content" style="white-space: pre-wrap;">{_h(row['content'] or '')}</div>
                {tag_html}
                {extras_html}
            </div>
            """)
        return "\n".join(cards)

    except Exception as e:
        return f'<p style="color: var(--m3-neon-amber); text-align: center; padding: 2rem 0;">Error scanning DB: {_h(str(e))}</p>'


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
                if table_exists(db, "memory_history"):
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
                                <span>ACTION: {_h(action)}</span>
                                <span style="font-family: 'Fira Code', monospace; color: hsl(210, 15%, 50%); font-weight: 400;">{_h(ts)}</span>
                            </div>
                            <div style="color: hsl(210, 15%, 85%); font-size: 0.82rem; margin-top: 0.4rem;">
                                <strong>ID:</strong> {_h(r['memory_id'][:8])}<br>
                                <strong>Details:</strong> {_h(r['new_value'] or r['prev_value'] or 'System record updated.')}
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
        return f'<span style="background: rgba(16, 185, 129, 0.2); color: var(--m3-neon-emerald); padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(16, 185, 129, 0.3); font-weight: 500;">{_h(new_val)}</span>'
    if not new_val:
        return f'<span style="background: rgba(239, 68, 68, 0.2); color: #ff6b6b; padding: 2px 6px; border-radius: 4px; border: 1px solid rgba(239, 68, 68, 0.3); text-decoration: line-through;">{_h(prev_val)}</span>'

    import difflib
    matcher = difflib.SequenceMatcher(None, prev_val.split(), new_val.split())
    result = []
    for opcode, a_start, a_end, b_start, b_end in matcher.get_opcodes():
        if opcode == 'equal':
            result.append(_h(" ".join(prev_val.split()[a_start:a_end])))
        elif opcode == 'insert':
            inserted = _h(" ".join(new_val.split()[b_start:b_end]))
            result.append(f'<span style="background: rgba(16, 185, 129, 0.25); color: #88ff88; border-bottom: 2px solid var(--m3-neon-emerald); padding: 0 4px; border-radius: 2px; font-weight: 600;">{inserted}</span>')
        elif opcode == 'delete':
            deleted = _h(" ".join(prev_val.split()[a_start:a_end]))
            result.append(f'<span style="background: rgba(239, 68, 68, 0.25); color: #ff6b6b; text-decoration: line-through; border-bottom: 2px solid #ef4444; padding: 0 4px; border-radius: 2px;">{deleted}</span>')
        elif opcode == 'replace':
            deleted = _h(" ".join(prev_val.split()[a_start:a_end]))
            inserted = _h(" ".join(new_val.split()[b_start:b_end]))
            result.append(f'<span style="background: rgba(239, 68, 68, 0.25); color: #ff6b6b; text-decoration: line-through; padding: 0 4px; border-radius: 2px;">{deleted}</span>'
                          f' <span style="background: rgba(16, 185, 129, 0.25); color: #88ff88; padding: 0 4px; border-radius: 2px; font-weight: 600;">{inserted}</span>')
    return " ".join(result)


def render_audit_card(memory_id: str, db: Any) -> str:
    # Backend-agnostic: dialect placeholder (?/%s) + POSITIONAL row indexing
    # (psycopg rows aren't dict-like; SQLite Row supports positional too).
    ph = _active_backend().dialect().placeholder
    # 1. Fetch current memory state from memory_items if it exists.
    #    cols: 0=title 1=content 2=type 3=user_id 4=importance 5=is_deleted 6=updated_at
    row = db.execute(
        "SELECT title, content, type, user_id, importance, is_deleted, updated_at "
        f"FROM memory_items WHERE id = {ph()}", (memory_id,)
    ).fetchone()

    current_title = "None (Deleted)"
    current_content = ""
    current_type = "unknown"
    is_deleted = True
    user_id = ""
    importance = 0.0

    if row:
        # Name access works on both backends: SQLite Row + PG _DualRow (compat).
        current_title = row["title"] or "(Untitled)"
        current_content = row["content"] or ""
        current_type = row["type"] or "note"
        is_deleted = bool(row["is_deleted"])
        user_id = row["user_id"] or ""
        importance = row["importance"] or 0.0

    # 2. Fetch history records sorted by created_at DESC.
    hist_rows = db.execute(
        "SELECT event, field, prev_value, new_value, actor_id, created_at "
        f"FROM memory_history WHERE memory_id = {ph()} "
        "ORDER BY created_at DESC", (memory_id,)
    ).fetchall()

    # Check if there's any active conflict/contradiction
    has_contradiction = any((r["event"] or "").upper() == "CONTRADICTION" for r in hist_rows)

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
        event = (r["event"] or "").upper()
        field = r["field"] or "content"
        prev_v = r["prev_value"] or ""
        new_v = r["new_value"] or ""
        actor = r["actor_id"] or "system"
        ts = (r["created_at"] or "").replace("T", " ")[:16]

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
                <div class="diff-text" style="color: var(--m3-neon-emerald);">{_h(new_v or prev_v)}</div>
            </div>
            """
        elif event == "DELETE":
            diff_html = f"""
            <div style="margin-top: 0.5rem;">
                <span style="font-size: 0.75rem; font-weight: 600; color: hsl(210, 10%, 60%);">Deleted Value:</span>
                <div class="diff-text" style="color: #ff6b6b; text-decoration: line-through;">{_h(prev_v)}</div>
            </div>
            """
        elif event == "RESOLVE":
            diff_html = f"""
            <div style="margin-top: 0.5rem;">
                <span style="font-size: 0.75rem; font-weight: 600; color: hsl(210, 10%, 60%);">Resolution:</span>
                <div class="diff-text" style="color: var(--m3-neon-purple);">{_h(new_v or 'Conflict resolved, marked active.')}</div>
            </div>
            """

        nodes_html.append(f"""
        <div class="timeline-node">
            <div class="timeline-badge {badge_cls}">{icon}</div>
            <div class="timeline-content-box">
                <div style="display: flex; justify-content: space-between; align-items: center; font-size: 0.8rem;">
                    <strong style="color: var(--m3-neon-cyan);">{_h(event)}</strong>
                    <span style="color: hsl(210, 10%, 55%); font-family: 'Fira Code', monospace;">{_h(ts)}</span>
                </div>
                <div style="font-size: 0.78rem; color: hsl(210, 10%, 75%); margin-top: 0.25rem;">
                    by <strong style="color: #fff;">{_h(actor)}</strong> | field: <code>{_h(field)}</code>
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
                if not table_exists(db, "memory_history"):
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
                        f"SELECT title, content, type FROM memory_items WHERE id = {_active_backend().dialect().placeholder()}",
                        (mem_id,),
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
    # Backend-agnostic placeholder: ? on SQLite, %s on PostgreSQL/others. _db()
    # yields the raw driver connection (no auto ?→%s translation), so inline SQL
    # MUST use the active backend's placeholder to work on every backend.
    ph = _active_backend().dialect().placeholder
    with active_database(selected_db_path):
        with _db() as db:
            # 1. Undelete (is_deleted = 0)
            db.execute(f"UPDATE memory_items SET is_deleted = 0, updated_at = {ph()} WHERE id = {ph()}",
                       (datetime.now(timezone.utc).isoformat(), memory_id))
            # 2. Record "resolve" event in history. Name access works on both
            #    backends (SQLite Row + PG _DualRow compat cursor).
            row = db.execute(f"SELECT content FROM memory_items WHERE id = {ph()}", (memory_id,)).fetchone()
            content = (row["content"] if row else None) or "Restored and active."
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
        # Spawn child process in the background. no_window_kwargs() suppresses a
        # console window on Windows (the maintenance task would otherwise flash one).
        from _task_runtime import no_window_kwargs
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=log_file_handle,
            stderr=subprocess.STDOUT, # redirect stderr to stdout
            shell=False,
            **no_window_kwargs(),
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


_HALT_ROLE = "dashboard"


def _ensure_std_streams() -> None:
    """Give the process real stdout/stderr when launched via pythonw.exe.

    Under ``pythonw`` (no console) sys.stdout/sys.stderr are None; a stray write
    from any dependency then raises and can take the process down. Bind the
    missing streams to devnull so such writes are harmless. Idempotent; a no-op
    under normal python.exe. (Mirror of embed_server_inproc._ensure_std_streams.)
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
            except OSError:
                pass


def _resolve_host_port(host: "str | None", port: "int | None") -> "tuple[str, int]":
    resolved_host = host or os.environ.get("M3_DASHBOARD_HOST", HOST)
    resolved_port = int(port or os.environ.get("M3_DASHBOARD_PORT", PORT))
    return resolved_host, resolved_port


def _port_already_serving(host: str, port: int, timeout: float = 1.0) -> bool:
    """True if something already accepts TCP on host:port (a live dashboard).

    Used as a single-instance pre-flight so a self-heal re-fire or an accidental
    double-launch exits cleanly instead of crashing on an in-use port. 0.0.0.0 is
    probed via loopback (you connect TO a concrete address, not the wildcard).
    """
    import socket
    # B104 false positive: "0.0.0.0" here is a string comparison that remaps the
    # wildcard to loopback for an outbound connect probe; this is not a bind-to-all.
    probe = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host  # nosec B104
    try:
        with socket.create_connection((probe, port), timeout=timeout):
            return True
    except OSError:
        return False


def _live_dashboards() -> list:
    """Registered, alive dashboard processes (reaps stale entries as a side effect)."""
    try:
        import m3_halt
        return [p for p in m3_halt.list_live_processes() if p.role == _HALT_ROLE]
    except Exception:  # noqa: BLE001
        return []


# Command-line signature of a running dashboard SERVER. The registry can MISS a
# process (killed externally, crashed mid-register, a stale duplicate that lost
# the bind race) — so stop/status also sweep by what the process RUNS, a
# registry-independent floor mirroring m3_halt._WRITER_CMDLINE_SIGNATURES.
#
# The signature requires BOTH "dashboard_server.py" AND "--foreground": only the
# actual long-lived server (the detached child + the ONSTART task) runs with
# --foreground. The transient LAUNCHER (the `m3 dashboard` CLI process, the
# UTF-8 re-exec parent, a bare `python dashboard_server.py` about to background)
# does NOT carry --foreground, so it is correctly NOT matched — that avoids the
# false-positive where a launcher sees its own re-exec'd child as "already
# running" and refuses to start.
_DASHBOARD_CMDLINE_SIG = "dashboard_server.py"
_DASHBOARD_FOREGROUND_SIG = "--foreground"


def _dashboard_pids_by_cmdline() -> "set[int]":
    """PIDs of live dashboard SERVER processes, found by cmdline — independent of
    the PID registry. Cross-platform, best-effort (empty on error).

    Matches a python interpreter running ``dashboard_server.py --foreground``;
    EXCLUDES this process and non-python matches. Never raises."""
    me = os.getpid()
    pids: set[int] = set()
    try:
        if sys.platform == "win32":
            import subprocess

            from _task_runtime import no_window_kwargs
            # PowerShell CIM: python* whose cmdline has BOTH the script and --foreground.
            ps = (
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -match 'python' -and "
                f"$_.CommandLine -match '{_DASHBOARD_CMDLINE_SIG}' -and "
                "$_.CommandLine -match '--foreground' } | "
                "ForEach-Object { $_.ProcessId }"
            )
            out = subprocess.run(  # noqa: S603
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=15, **no_window_kwargs(),
            ).stdout
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit() and int(line) != me:
                    pids.add(int(line))
        else:
            # POSIX: scan /proc for a python cmdline with both signatures.
            import glob
            for cmdpath in glob.glob("/proc/[0-9]*/cmdline"):
                try:
                    pid = int(cmdpath.split("/")[2])
                    if pid == me:
                        continue
                    with open(cmdpath, "rb") as f:
                        parts = f.read().split(b"\x00")
                    joined = b" ".join(parts).decode("utf-8", "replace")
                    if ("python" in joined and _DASHBOARD_CMDLINE_SIG in joined
                            and _DASHBOARD_FOREGROUND_SIG in joined):
                        pids.add(pid)
                except (OSError, ValueError, IndexError):
                    continue
    except Exception:  # noqa: BLE001 — discovery is best-effort; registry still covers the common case
        pass
    return pids


def _kill_pid(pid: int) -> bool:
    """Terminate a pid cross-platform (taskkill /F on Windows, SIGTERM on POSIX)."""
    import subprocess
    try:
        if sys.platform == "win32":
            from _task_runtime import no_window_kwargs
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],  # noqa: S603,S607
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=False, **no_window_kwargs())
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
        return True
    except Exception:  # noqa: BLE001
        return False


def dashboard_status(host: "str | None" = None, port: "int | None" = None) -> int:
    """Print whether a dashboard is running and where. Returns 0 if running, 1 if not.

    Reports registry-known instances (with URL) AND flags any unregistered
    process found by cmdline (so a zombie the registry missed is still surfaced).
    """
    live = _live_dashboards()
    registered_pids = {p.pid for p in live}
    for p in live:
        h = str(p.extra.get("host") or HOST)
        pt = p.extra.get("port") or PORT
        print(f"dashboard: running (pid {p.pid}) → http://{h}:{pt}")
    # Unregistered survivors (not in the registry) — surface them honestly.
    orphans = _dashboard_pids_by_cmdline() - registered_pids
    for pid in sorted(orphans):
        print(f"dashboard: running but UNREGISTERED (pid {pid}) — "
              "`m3 dashboard --stop` will clean it up.")
    if not live and not orphans:
        print("dashboard: not running  (start with `m3 dashboard`)")
        return 1
    return 0


def dashboard_stop() -> int:
    """Terminate ALL running dashboard(s) and reap their registry entries.

    Uses the UNION of (a) the PID registry and (b) a cmdline sweep, so a process
    the registry missed — killed externally, crashed mid-register, or a stale
    duplicate — is still stopped. Registry-only stop was blind to those.
    """
    live = _live_dashboards()
    reg_pids = {p.pid for p in live}
    cmdline_pids = _dashboard_pids_by_cmdline()
    all_pids = reg_pids | cmdline_pids
    if not all_pids:
        print("dashboard: not running — nothing to stop.")
        return 0

    for pid in sorted(all_pids):
        if _kill_pid(pid):
            tag = "" if pid in reg_pids else " (unregistered)"
            print(f"dashboard: stopped (pid {pid}){tag}.")
        else:
            print(f"dashboard: could not stop pid {pid}.")
    # Reap registry entries for the ones we knew about.
    for p in live:
        try:
            p.path.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def _spawn_detached(host: str, port: int) -> int:
    """Relaunch this server DETACHED + WINDOWLESS, then return to the caller.

    Windows: re-exec under pythonw.exe (GUI subsystem — the OS never allocates a
    console, so there is NO window and NO flash, ever) with CREATE_NO_WINDOW |
    DETACHED_PROCESS so the child outlives this process and the terminal. POSIX:
    start_new_session=True detaches from the controlling terminal. Mirrors the
    m3_cognitive_loop `--background` daemonize. The child runs `--foreground`
    (the actual blocking server) and self-registers in the PID registry.
    """
    import subprocess
    env = dict(os.environ)
    env["M3_DASHBOARD_HOST"] = host
    env["M3_DASHBOARD_PORT"] = str(port)
    script = os.path.abspath(__file__)
    if sys.platform == "win32":
        # pythonw.exe (GUI subsystem) = the OS never allocates a console → no
        # window, no flash. CREATE_NO_WINDOW belts-and-braces the no-console
        # guarantee; DETACHED_PROCESS lets the child outlive this process/terminal.
        pyw = sys.executable.replace("python.exe", "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        subprocess.Popen(  # noqa: S603
            [exe, script, "--foreground"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
    else:
        # POSIX has no console window; start_new_session detaches from the
        # controlling terminal. no_window_kwargs() is an empty dict here (marks
        # the no-window intent for the regression guard; a no-op off Windows).
        from _task_runtime import no_window_kwargs
        subprocess.Popen(  # noqa: S603
            [sys.executable, script, "--foreground"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True, **no_window_kwargs(),
        )
    print(f"m3 dashboard started in background → http://{host}:{port}", flush=True)
    print("  (keeps running after you close this window; stop with `m3 dashboard --stop`)", flush=True)
    return 0


def run_dashboard(host: "str | None" = None, port: "int | None" = None,
                  *, background: bool = True) -> int:
    """Start the dashboard.

    DEFAULT (``background=True``): detach a WINDOWLESS server that SURVIVES the
    terminal closing (no startup window, no flash, no periodic flashes — it runs
    under pythonw/detached), then RETURN to the prompt. Single-instance: if one
    is already running, report its URL instead of spawning a duplicate.

    ``background=False`` (the ``--foreground`` child, or an explicit foreground
    run): run the uvicorn server IN THIS PROCESS until stopped. This is the code
    the detached child and the ONSTART scheduled task both execute.
    """
    resolved_host, resolved_port = _resolve_host_port(host, port)

    if background:
        # INTERACTIVE launch path (`m3 dashboard`): don't spawn a second detached
        # server. These probes are an informational "already up → here's the URL"
        # pre-check — the spawned child's FOREGROUND path holds the real atomic
        # lock (above). Returns 0 here on purpose: for a human/script running
        # `m3 dashboard`, "it's already up" is SUCCESS (they wanted it available),
        # not the "lost a race" failure the supervisor-run foreground path reports.
        live = _live_dashboards()
        if live:
            p = live[0]
            h = str(p.extra.get("host") or resolved_host)
            pt = p.extra.get("port") or resolved_port
            print(f"m3 dashboard already running (pid {p.pid}) → http://{h}:{pt}")
            return 0
        if _dashboard_pids_by_cmdline():
            print(f"m3 dashboard already running → http://{resolved_host}:{resolved_port}")
            return 0
        if _port_already_serving(resolved_host, resolved_port):
            print(f"m3 dashboard already serving → http://{resolved_host}:{resolved_port}")
            return 0
        return _spawn_detached(resolved_host, resolved_port)

    # ── Foreground server body (runs in the detached child / scheduled task) ──
    _ensure_std_streams()  # pythonw has no console → bind devnull so writes are safe

    # Single-instance pre-flight — an OS ADVISORY lock (fcntl/msvcrt), race-free
    # by construction (the old port-probe was check-then-act TOCTOU; two launches
    # both bound and piled up, observed 2026-07-20). The OS releases the lock on
    # death, so there is no stale lock / dead-PID case. acquire_or_exit: a LIVE
    # peer → sys.exit(EXIT_ALREADY_RUNNING=4) (supervisors treat 4 as a clean
    # no-op, no respawn); a config/lock error → a degraded handle so we still run
    # (fail-safe §3). Held for the process lifetime (released via atexit+SIGTERM).
    from m3_sdk import acquire_or_exit
    _lock = acquire_or_exit(
        _HALT_ROLE,
        extra={"host": resolved_host, "port": resolved_port},
        on_already_running=lambda o: print(
            f"dashboard already running (pid {o.pid if o else '?'}) on "
            f"{resolved_host}:{resolved_port}; exiting.", flush=True),
    )
    run_dashboard._instance_lock = _lock  # type: ignore[attr-defined]  # keep alive
    if not _lock.acquired:
        print(f"[warn] dashboard single-instance lock DEGRADED "
              f"({_lock.status.value}) — running without enforcement", flush=True)

    registered = False
    try:
        import m3_halt
        m3_halt.register_process(
            _HALT_ROLE, extra={"host": resolved_host, "port": resolved_port})
        registered = True
    except Exception as e:  # noqa: BLE001 — registration is advisory, not fatal
        print(f"[warn] could not register dashboard in PID registry: {e}", flush=True)

    config = uvicorn.Config(app, host=resolved_host, port=resolved_port,
                            log_level="warning", use_colors=False)

    class _NoSignalServer(uvicorn.Server):
        # Under pythonw there is NO console to deliver SIGINT/SIGTERM, and
        # uvicorn's default handler install can make a no-console server exit
        # early. The process is stopped by taskkill/SIGTERM (dashboard_stop) or
        # the OS, not by Ctrl-C — so don't install console signal handlers.
        def install_signal_handlers(self) -> None:
            return None

    server = _NoSignalServer(config)
    try:
        server.run()  # blocks until the process is terminated
        return 0
    finally:
        if registered:
            try:
                import m3_halt
                m3_halt.deregister(_HALT_ROLE)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass


# --- Execution Hook ---
if __name__ == "__main__":
    import argparse

    _p = argparse.ArgumentParser(description="M3 local web dashboard.")
    _p.add_argument("--host", default=None, help="Bind address (default 127.0.0.1).")
    _p.add_argument("--port", type=int, default=None, help="TCP port (default 8088).")
    _p.add_argument("--foreground", action="store_true",
                    help="Run the server in THIS process (used by the detached "
                         "child and the boot task). Default launches detached.")
    _p.add_argument("--stop", action="store_true", help="Stop a running dashboard.")
    _p.add_argument("--status", action="store_true", help="Report dashboard status.")
    # --log-file is accepted for scheduled-task parity (self-logging); the task
    # runtime consumes it, so we just tolerate it here.
    _p.add_argument("--log-file", default=None, help=argparse.SUPPRESS)
    _a = _p.parse_args()

    if _a.log_file:
        try:
            import _task_runtime
            _task_runtime.setup_task_runtime(log_file=_a.log_file, lock_name=None,
                                             logger_name="dashboard")
        except Exception:  # noqa: BLE001 — logging setup must never block startup
            pass

    if _a.stop:
        sys.exit(dashboard_stop())
    if _a.status:
        sys.exit(dashboard_status(_a.host, _a.port))
    sys.exit(run_dashboard(_a.host, _a.port, background=not _a.foreground))
