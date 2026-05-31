#!/usr/bin/env python3
"""
M3 Cognitive Dashboard Server.
FastAPI + HTMX lightweight local administration and visualization portal.
Listens on port 8088 by default.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import uvicorn

# Ensure bin/ is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from m3_sdk import resolve_db_path
from memory import config as _config
from memory.db import _db
from memory.search import memory_search_scored_impl
from memory_maintenance import gdpr_export_impl, gdpr_forget_impl

PORT = 8088
HOST = "127.0.0.1"

# --- HTML template embedded as raw string for ultimate single-file portability ---
INDEX_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>M3 Cognitive Dashboard</title>
    <!-- Modern Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Inter:wght@300;400;500;600&family=Outfit:wght@500;600;700&display=swap" rel="stylesheet">
    
    <!-- HTMX -->
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    
    <style>
        :root {
            /* --- Theme Foundations --- */
            --m3-bg-deep: hsl(224, 25%, 6%);
            --m3-bg-surface: hsl(222, 22%, 10%);
            --m3-bg-card-glass: hsla(222, 22%, 12%, 0.75);
            
            /* --- Core Neon Accents --- */
            --m3-neon-cyan: hsl(180, 100%, 50%);
            --m3-neon-purple: hsl(270, 100%, 65%);
            --m3-neon-amber: hsl(38, 100%, 50%);
            --m3-neon-emerald: hsl(145, 100%, 45%);
            
            /* --- Borders & Shadows --- */
            --m3-border-glow: hsla(180, 100%, 50%, 0.2);
            --m3-border-glass: hsla(217, 19%, 27%, 0.25);
            --m3-shadow-glow: 0 0 25px hsla(180, 100%, 50%, 0.15);
            --m3-shadow-card: 0 8px 32px 0 rgba(0, 0, 0, 0.4);
            
            /* --- Transitions --- */
            --m3-transition-smooth: all 0.3s cubic-bezier(0.25, 0.8, 0.25, 1);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Inter', system-ui, -apple-system, sans-serif;
            color: hsl(210, 40%, 98%);
            background-color: var(--m3-bg-deep);
            -webkit-font-smoothing: antialiased;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }

        /* --- Header Navigation --- */
        header {
            background: rgba(10, 12, 18, 0.8);
            backdrop-filter: blur(12px);
            border-bottom: 1px solid var(--m3-border-glass);
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            position: sticky;
            top: 0;
            z-index: 100;
        }

        .logo-group {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .logo-text {
            font-family: 'Outfit', sans-serif;
            font-size: 1.5rem;
            font-weight: 700;
            background: linear-gradient(135deg, var(--m3-neon-cyan) 0%, var(--m3-neon-purple) 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .db-status {
            font-family: 'Fira Code', monospace;
            font-size: 0.8rem;
            color: hsl(210, 15%, 65%);
            background: hsla(222, 22%, 5%, 0.6);
            padding: 0.4rem 0.8rem;
            border-radius: 6px;
            border: 1px solid var(--m3-border-glass);
            max-width: 400px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .status-dot-container {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-size: 0.85rem;
        }

        .m3-status-dot {
            width: 10px;
            height: 10px;
            background-color: var(--m3-neon-cyan);
            border-radius: 50%;
            box-shadow: 0 0 10px var(--m3-neon-cyan);
            animation: pulse-glow 2s infinite;
        }

        @keyframes pulse-glow {
            0% { box-shadow: 0 0 0 0 rgba(0, 255, 255, 0.4); }
            70% { box-shadow: 0 0 0 8px rgba(0, 255, 255, 0); }
            100% { box-shadow: 0 0 0 0 rgba(0, 255, 255, 0); }
        }

        /* --- Dashboard Grid Layout --- */
        .container {
            max-width: 1400px;
            width: 100%;
            margin: 2rem auto;
            padding: 0 1.5rem;
            flex-grow: 1;
            display: grid;
            grid-template-columns: 1fr 380px;
            gap: 2rem;
        }

        @media (max-width: 1024px) {
            .container {
                grid-template-columns: 1fr;
            }
        }

        /* --- General Component Card styling --- */
        .m3-card {
            background: var(--m3-bg-card-glass);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid var(--m3-border-glass);
            border-radius: 12px;
            padding: 1.5rem;
            box-shadow: var(--m3-shadow-card);
            transition: var(--m3-transition-smooth);
            margin-bottom: 2rem;
        }

        .m3-card:hover {
            border-color: var(--m3-border-glow);
            box-shadow: var(--m3-shadow-glow), var(--m3-shadow-card);
        }

        .m3-card-title {
            font-family: 'Outfit', sans-serif;
            font-size: 1.25rem;
            font-weight: 600;
            margin-bottom: 1.25rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        /* --- Metrics Grid --- */
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 1.25rem;
            margin-bottom: 2rem;
        }

        @media (max-width: 768px) {
            .metrics-grid {
                grid-template-columns: repeat(2, 1fr);
            }
        }

        .metric-card {
            background: hsla(222, 22%, 10%, 0.5);
            border: 1px solid var(--m3-border-glass);
            border-radius: 10px;
            padding: 1.25rem;
            text-align: center;
        }

        .metric-value {
            font-family: 'Outfit', sans-serif;
            font-size: 2rem;
            font-weight: 700;
            color: #fff;
            margin-bottom: 0.25rem;
        }

        .metric-label {
            font-size: 0.8rem;
            color: hsl(210, 15%, 65%);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        /* --- Interactive Search bar --- */
        .search-group {
            display: flex;
            gap: 0.75rem;
            margin-bottom: 1.5rem;
        }

        .m3-input {
            flex-grow: 1;
            background: hsla(222, 22%, 5%, 0.6);
            border: 1px solid var(--m3-border-glass);
            color: #fff;
            border-radius: 8px;
            padding: 0.75rem 1rem;
            font-family: inherit;
            font-size: 0.95rem;
            transition: var(--m3-transition-smooth);
        }

        .m3-input:focus {
            outline: none;
            border-color: var(--m3-neon-cyan);
            box-shadow: 0 0 12px hsla(180, 100%, 50%, 0.15);
        }

        .m3-btn {
            background: var(--m3-grad-primary);
            border: none;
            color: #fff;
            font-family: 'Outfit', sans-serif;
            font-weight: 600;
            font-size: 0.95rem;
            border-radius: 8px;
            padding: 0.75rem 1.5rem;
            cursor: pointer;
            transition: var(--m3-transition-smooth);
        }

        .m3-btn:hover {
            opacity: 0.9;
            box-shadow: 0 0 15px hsla(270, 100%, 65%, 0.25);
        }

        .m3-btn-danger {
            background: linear-gradient(135deg, var(--m3-neon-amber) 0%, hsl(15, 100%, 50%) 100%);
        }

        /* --- Search Result cards --- */
        .memory-card {
            background: hsla(222, 22%, 8%, 0.4);
            border: 1px solid var(--m3-border-glass);
            border-radius: 8px;
            padding: 1.25rem;
            margin-bottom: 1rem;
            transition: var(--m3-transition-smooth);
        }

        .memory-card:hover {
            border-color: rgba(255, 255, 255, 0.15);
        }

        .memory-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.75rem;
        }

        .m3-badge {
            font-family: 'Outfit', sans-serif;
            font-size: 0.7rem;
            font-weight: 600;
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
            text-transform: uppercase;
        }

        .badge-note { background: hsla(270, 100%, 65%, 0.15); color: var(--m3-neon-purple); border: 1px solid rgba(168, 85, 247, 0.3); }
        .badge-fact { background: hsla(180, 100%, 50%, 0.1); color: var(--m3-neon-cyan); border: 1px solid rgba(6, 182, 212, 0.3); }
        .badge-warn { background: hsla(38, 100%, 50%, 0.1); color: var(--m3-neon-amber); border: 1px solid rgba(245, 158, 11, 0.3); }
        .badge-sys { background: hsla(145, 100%, 45%, 0.1); color: var(--m3-neon-emerald); border: 1px solid rgba(16, 185, 129, 0.3); }

        .memory-id {
            font-family: 'Fira Code', monospace;
            font-size: 0.75rem;
            color: hsl(210, 15%, 55%);
        }

        .memory-content {
            font-size: 0.92rem;
            line-height: 1.5;
            color: hsl(210, 15%, 85%);
        }

        /* --- Explain mode box --- */
        .explain-box {
            margin-top: 1rem;
            background: rgba(10, 12, 18, 0.6);
            border: 1px solid var(--m3-border-glass);
            border-radius: 6px;
            padding: 0.75rem 1rem;
        }

        .explain-title {
            font-family: 'Outfit', sans-serif;
            font-size: 0.8rem;
            font-weight: 600;
            color: var(--m3-neon-cyan);
            margin-bottom: 0.5rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .explain-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 0.75rem;
            font-family: 'Fira Code', monospace;
            font-size: 0.75rem;
        }

        .explain-metric span {
            color: hsl(210, 15%, 60%);
        }

        /* --- Knowledge Graph panel --- */
        .graph-panel {
            width: 100%;
            height: 380px;
            background: hsla(222, 22%, 5%, 0.6);
            border: 1px solid var(--m3-border-glass);
            border-radius: 8px;
            position: relative;
            overflow: hidden;
        }

        canvas {
            display: block;
            width: 100%;
            height: 100%;
            cursor: grab;
        }

        canvas:active {
            cursor: grabbing;
        }

        /* --- Contradiction Alert card --- */
        .conflict-item {
            background: hsla(38, 100%, 50%, 0.04);
            border: 1px solid rgba(245, 158, 11, 0.2);
            border-left: 4px solid var(--m3-neon-amber);
            border-radius: 6px;
            padding: 1rem;
            margin-bottom: 0.75rem;
            font-size: 0.85rem;
        }

        /* --- GDPR control section --- */
        .gdpr-btn-group {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }

        .gdpr-user-group {
            margin-bottom: 0.5rem;
        }
    </style>
</head>
<body>

    <header>
        <div class="logo-group">
            <div class="m3-status-dot"></div>
            <div class="logo-text">M3 COGNITIVE</div>
        </div>
        <div class="db-status" title="Active SQLite path">
            DB: {{ db_path }}
        </div>
        <div class="status-dot-container">
            <span style="color: hsl(210, 15%, 75%); font-family: 'Outfit', sans-serif; font-weight: 500;">Active Link</span>
            <div class="m3-status-dot" style="background-color: var(--m3-neon-emerald); box-shadow: 0 0 10px var(--m3-neon-emerald);"></div>
        </div>
    </header>

    <div class="container">
        <!-- Main Panel (Left Column) -->
        <main>
            <!-- System Stats Grid -->
            <div class="metrics-grid" hx-get="/api/stats" hx-trigger="load, every 8s">
                <!-- Swapped in dynamically by HTMX -->
            </div>

            <!-- Graph Canvas Explorer -->
            <div class="m3-card">
                <div class="m3-card-title">
                    <span>Interactive Knowledge Graph</span>
                    <button class="m3-btn" style="padding: 0.35rem 0.75rem; font-size: 0.75rem;" onclick="resetSimulation()">Reset Layout</button>
                </div>
                <div class="graph-panel">
                    <canvas id="graphCanvas"></canvas>
                </div>
            </div>

            <!-- Memory Search -->
            <div class="m3-card">
                <div class="m3-card-title">Memory Browser & Explain Engine</div>
                <div class="search-group">
                    <input type="text" name="q" class="m3-input" placeholder="Type query to scan cognitive layer (e.g. UniFi VLAN)..."
                           hx-get="/api/search" hx-target="#searchResults" hx-trigger="keyup changed delay:350ms, search">
                </div>
                <div id="searchResults">
                    <p style="color: hsl(210, 15%, 65%); text-align: center; padding: 2rem 0;">Type in search bar to explore FTS5 & Vector similarity explain graphs.</p>
                </div>
            </div>
        </main>

        <!-- Sidebar Panel (Right Column) -->
        <aside>
            <!-- Contradictions & Audit Feed -->
            <div class="m3-card">
                <div class="m3-card-title">
                    <span>Change & Conflict Log</span>
                </div>
                <div id="historyLog" hx-get="/api/history" hx-trigger="load, every 5s">
                    <!-- Loaded dynamically -->
                </div>
            </div>

            <!-- GDPR Center -->
            <div class="m3-card">
                <div class="m3-card-title">GDPR Compliance center</div>
                <div class="gdpr-btn-group">
                    <div class="gdpr-user-group">
                        <label style="font-size: 0.8rem; color: hsl(210, 15%, 65%); display: block; margin-bottom: 0.25rem;">User ID</label>
                        <input type="text" id="gdprUserId" class="m3-input" style="width: 100%; padding: 0.5rem 0.75rem;" value="default">
                    </div>
                    <button class="m3-btn" style="width: 100%; font-size: 0.85rem;" onclick="exportGDPR()">Export User Data (Art. 20)</button>
                    <button class="m3-btn m3-btn-danger" style="width: 100%; font-size: 0.85rem;" onclick="forgetGDPR()">Purge User Records (Art. 17)</button>
                </div>
                <div id="gdprFeedback" style="margin-top: 0.75rem; font-size: 0.8rem; text-align: center;"></div>
            </div>
        </aside>
    </div>

    <!-- Canvas Simulation JS -->
    <script>
        const canvas = document.getElementById("graphCanvas");
        const ctx = canvas.getContext("2d");
        
        let nodes = [];
        let links = [];
        let selectedNode = null;
        let scale = 1.0;
        let offsetX = 0;
        let offsetY = 0;
        
        // Auto-scale canvas on resize
        function resizeCanvas() {
            canvas.width = canvas.parentElement.clientWidth;
            canvas.height = canvas.parentElement.clientHeight;
        }
        window.addEventListener("resize", resizeCanvas);
        resizeCanvas();

        // Fetch graph and run simulation
        async function loadGraph() {
            try {
                const res = await fetch("/api/graph");
                const data = await res.json();
                
                // Initialize physics state
                nodes = data.nodes.map(n => ({
                    ...n,
                    x: canvas.width / 2 + (Math.random() - 0.5) * 200,
                    y: canvas.height / 2 + (Math.random() - 0.5) * 200,
                    vx: 0,
                    vy: 0
                }));
                
                // Map links to objects
                links = data.links.map(l => ({
                    ...l,
                    source: nodes.find(n => n.id === l.source),
                    target: nodes.find(n => n.id === l.target)
                })).filter(l => l.source && l.target);
                
            } catch(e) {
                console.error("Failed to load graph nodes", e);
            }
        }

        // Color mapping based on design system accents
        function getNodeColor(type) {
            switch(type) {
                case "person": return "hsl(270, 100%, 75%)"; // neon purple
                case "place": return "hsl(180, 100%, 50%)"; // neon cyan
                case "topic": return "hsl(38, 100%, 55%)"; // neon amber
                default: return "hsl(145, 100%, 45%)"; // neon emerald
            }
        }

        // Basic force-directed simulation loop
        function updatePhysics() {
            const kRepel = 0.08;
            const kLink = 0.05;
            const linkDist = 90;
            const friction = 0.85;
            
            // Repulsion between all node pairs
            for(let i=0; i<nodes.length; i++) {
                for(let j=i+1; j<nodes.length; j++) {
                    const n1 = nodes[i];
                    const n2 = nodes[j];
                    const dx = n2.x - n1.x;
                    const dy = n2.y - n1.y;
                    const distSq = dx*dx + dy*dy + 0.1;
                    if(distSq < 40000) {
                        const dist = Math.sqrt(distSq);
                        const force = kRepel * (linkDist - dist) / dist;
                        n1.vx -= force * dx;
                        n1.vy -= force * dy;
                        n2.vx += force * dx;
                        n2.vy += force * dy;
                    }
                }
            }
            
            // Link attraction
            links.forEach(l => {
                const dx = l.target.x - l.source.x;
                const dy = l.target.y - l.source.y;
                const dist = Math.sqrt(dx*dx + dy*dy) || 0.1;
                const force = kLink * (dist - linkDist) / dist;
                const fx = force * dx;
                const fy = force * dy;
                
                l.source.vx += fx;
                l.source.vy += fy;
                l.target.vx -= fx;
                l.target.vy -= fy;
            });
            
            // Apply speed limits, drag, and update positions
            nodes.forEach(n => {
                if(n === selectedNode) return; // skip currently dragged
                n.vx *= friction;
                n.vy *= friction;
                n.x += Math.max(-10, Math.min(10, n.vx));
                n.y += Math.max(-10, Math.min(10, n.vy));
                
                // Contain inside bounds
                n.x = Math.max(20, Math.min(canvas.width - 20, n.x));
                n.y = Math.max(20, Math.min(canvas.height - 20, n.y));
            });
        }

        // Draw loop
        function draw() {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            
            ctx.save();
            ctx.translate(offsetX, offsetY);
            ctx.scale(scale, scale);
            
            // Draw links
            ctx.lineWidth = 1;
            links.forEach(l => {
                ctx.strokeStyle = "rgba(6, 182, 212, 0.25)";
                ctx.beginPath();
                ctx.moveTo(l.source.x, l.source.y);
                ctx.lineTo(l.target.x, l.target.y);
                ctx.stroke();
                
                // Draw relationship predicate tag at midpoint
                const mx = (l.source.x + l.target.x) / 2;
                const my = (l.source.y + l.target.y) / 2;
                ctx.fillStyle = "rgba(210, 210, 220, 0.4)";
                ctx.font = "8px 'Fira Code'";
                ctx.textAlign = "center";
                ctx.fillText(l.predicate, mx, my - 2);
            });
            
            // Draw nodes
            nodes.forEach(n => {
                const color = getNodeColor(n.type);
                ctx.beginPath();
                ctx.arc(n.x, n.y, 8, 0, 2*Math.PI);
                ctx.fillStyle = color;
                ctx.shadowColor = color;
                ctx.shadowBlur = 8;
                ctx.fill();
                ctx.shadowBlur = 0; // reset
                
                // Node label
                ctx.fillStyle = "#fff";
                ctx.font = "10px 'Outfit', sans-serif";
                ctx.textAlign = "center";
                ctx.fillText(n.name, n.x, n.y - 12);
            });
            
            ctx.restore();
            
            updatePhysics();
            requestAnimationFrame(draw);
        }

        // Mouse interaction for drag
        let isDraggingCanvas = false;
        let dragStartMouse = { x: 0, y: 0 };
        let dragStartOffset = { x: 0, y: 0 };

        canvas.addEventListener("mousedown", e => {
            const rect = canvas.getBoundingClientRect();
            const mouseX = (e.clientX - rect.left - offsetX) / scale;
            const mouseY = (e.clientY - rect.top - offsetY) / scale;
            
            // Check if clicked a node
            selectedNode = nodes.find(n => {
                const dx = n.x - mouseX;
                const dy = n.y - mouseY;
                return dx*dx + dy*dy < 144;
            });
            
            if (!selectedNode) {
                isDraggingCanvas = true;
                dragStartMouse = { x: e.clientX, y: e.clientY };
                dragStartOffset = { x: offsetX, y: offsetY };
            }
        });

        canvas.addEventListener("mousemove", e => {
            if (selectedNode) {
                const rect = canvas.getBoundingClientRect();
                selectedNode.x = (e.clientX - rect.left - offsetX) / scale;
                selectedNode.y = (e.clientY - rect.top - offsetY) / scale;
            } else if (isDraggingCanvas) {
                offsetX = dragStartOffset.x + (e.clientX - dragStartMouse.x);
                offsetY = dragStartOffset.y + (e.clientY - dragStartMouse.y);
            }
        });

        canvas.addEventListener("mouseup", () => {
            selectedNode = null;
            isDraggingCanvas = false;
        });

        canvas.addEventListener("mouseleave", () => {
            selectedNode = null;
            isDraggingCanvas = false;
        });

        // Zoom wheel
        canvas.addEventListener("wheel", e => {
            e.preventDefault();
            const zoomFactor = 1.1;
            if(e.deltaY < 0) {
                scale *= zoomFactor;
            } else {
                scale /= zoomFactor;
            }
            scale = Math.max(0.4, Math.min(3.0, scale));
        });

        function resetSimulation() {
            scale = 1.0;
            offsetX = 0;
            offsetY = 0;
            loadGraph();
        }

        // GDPR functions
        function exportGDPR() {
            const uid = document.getElementById("gdprUserId").value;
            const fb = document.getElementById("gdprFeedback");
            fb.style.color = "var(--m3-neon-cyan)";
            fb.innerText = "Exporting data structure...";
            
            window.location.href = `/api/gdpr/export?user_id=${encodeURIComponent(uid)}`;
            setTimeout(() => {
                fb.innerText = "Export triggered successfully.";
            }, 1000);
        }

        async function forgetGDPR() {
            const uid = document.getElementById("gdprUserId").value;
            const fb = document.getElementById("gdprFeedback");
            if (!confirm(`Are you absolutely sure you want to completely purge user data for '${uid}'? This hard-deletes all memories, vectors, and graph edges recursively.`)) {
                return;
            }
            
            fb.style.color = "var(--m3-neon-amber)";
            fb.innerText = "Purging memory layers...";
            try {
                const formData = new FormData();
                formData.append("user_id", uid);
                
                const res = await fetch("/api/gdpr/forget", {
                    method: "POST",
                    body: formData
                });
                const msg = await res.text();
                fb.style.color = "var(--m3-neon-emerald)";
                fb.innerText = msg;
            } catch(e) {
                fb.style.color = "var(--m3-neon-amber)";
                fb.innerText = "Failed to purge user.";
            }
        }

        // Init loads
        loadGraph();
        draw();
    </script>
</body>
</html>
"""

# --- FastAPI Setup ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log startup info
    active = resolve_db_path(None)
    print(f"M3 Cognitive Dashboard serving SQLite database: {active}", flush=True)
    print(f"Server available at http://{HOST}:{PORT}", flush=True)
    yield

app = FastAPI(
    title="M3 Cognitive Dashboard",
    description="Portal to inspect local context indexing.",
    lifespan=lifespan
)

# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def get_index(request: Request):
    active_db = resolve_db_path(None)
    return INDEX_HTML.replace("{{ db_path }}", active_db)


@app.get("/api/stats", response_class=HTMLResponse)
async def get_stats():
    """Returns dynamic HTML stats counters cards."""
    total_mems = 0
    total_ents = 0
    total_rels = 0
    queue_len = 0
    
    try:
        with _db() as db:
            total_mems = db.execute("SELECT COUNT(*) FROM memory_items WHERE COALESCE(is_deleted, 0) = 0").fetchone()[0]
            
            # Check if entities & relationships tables exist before querying
            ent_exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entities'").fetchone()
            if ent_exists:
                total_ents = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
                
            rel_exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entity_relationships'").fetchone()
            if rel_exists:
                total_rels = db.execute("SELECT COUNT(*) FROM entity_relationships").fetchone()[0]
                
            queue_exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='entity_extraction_queue'").fetchone()
            if queue_exists:
                queue_len = db.execute("SELECT COUNT(*) FROM entity_extraction_queue").fetchone()[0]
    except Exception as e:
        print(f"Failed to query stats: {e}", flush=True)

    return f"""
    <div class="metric-card">
        <div class="metric-value">{total_mems}</div>
        <div class="metric-label">Memories</div>
    </div>
    <div class="metric-card" style="border-color: var(--m3-border-glow);">
        <div class="metric-value" style="color: var(--m3-neon-purple);">{total_ents}</div>
        <div class="metric-label">Entities</div>
    </div>
    <div class="metric-card">
        <div class="metric-value" style="color: var(--m3-neon-cyan);">{total_rels}</div>
        <div class="metric-label">Relationships</div>
    </div>
    <div class="metric-card">
        <div class="metric-value" style="color: var(--m3-neon-amber);">{queue_len}</div>
        <div class="metric-label">Queue Pending</div>
    </div>
    """


@app.get("/api/graph", response_class=JSONResponse)
async def get_graph():
    """Returns JSON nodes and links for the interactive canvas."""
    nodes = []
    links = []
    try:
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
async def search_memories(q: str = ""):
    """Uses core memory_search_scored_impl with explain mode enabled."""
    if not q.strip():
        return '<p style="color: hsl(210, 15%, 65%); text-align: center; padding: 2rem 0;">Type in search bar to explore FTS5 & Vector similarity explain graphs.</p>'
        
    try:
        # Search using standard explain=True
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
            if item.get("type") == "fact":
                badge_class = "badge-fact"
            elif item.get("type") == "contradiction":
                badge_class = "badge-warn"
                
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
        return "\\n".join(cards)
        
    except Exception as e:
        return f'<p style="color: var(--m3-neon-amber); text-align: center; padding: 2rem 0;">Error scanning indices: {str(e)}</p>'


@app.get("/api/history", response_class=HTMLResponse)
async def get_history_feed():
    """Queries the memory_history table for the conflict log."""
    logs = []
    try:
        with _db() as db:
            hist_exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_history'").fetchone()
            if hist_exists:
                rows = db.execute(
                    "SELECT action, memory_id, prev_content, new_content, timestamp FROM memory_history ORDER BY id DESC LIMIT 5"
                ).fetchall()
                for r in rows:
                    action = r["action"].upper()
                    ts = r["timestamp"].replace("T", " ")[:16]
                    color = "var(--m3-neon-purple)"
                    border = "1px solid var(--m3-border-glass)"
                    
                    if action == "SUPERSEDE" or action == "CONTRADICTION":
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
                            <strong>Details:</strong> {r['new_content'] or r['prev_content'] or 'System record updated.'}
                        </div>
                    </div>
                    """)
    except Exception as e:
        print(f"Failed to query history log: {e}", flush=True)

    if not logs:
        return '<p style="color: hsl(210, 15%, 55%); text-align: center; font-size: 0.85rem; padding: 1rem 0;">No history logs captured yet.</p>'
        
    return "\\n".join(logs)


@app.get("/api/gdpr/export")
async def export_gdpr_data(user_id: str = "default"):
    """Invokes core gdpr_export_impl as Article 20 JSON download."""
    try:
        raw_json = gdpr_export_impl(user_id)
        # Verify JSON
        data = json.loads(raw_json)
        
        # Return as downloadable StreamingResponse
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


# --- Execution Hook ---
if __name__ == "__main__":
    # Allow overriding port/host via env variables
    port_override = int(os.environ.get("M3_DASHBOARD_PORT", PORT))
    host_override = os.environ.get("M3_DASHBOARD_HOST", HOST)
    
    uvicorn.run("dashboard_server:app", host=host_override, port=port_override, reload=False)
