#!/usr/bin/env python3
"""
M3 Cognitive & Observability Portal.
FastAPI + HTMX unified local control center for Graph Exploration & KB Browsing.
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

# --- Common HTML Parts (Styling, Header, Nav) ---
HEADER_HTML = """
    <header>
        <div class="logo-group">
            <div class="m3-status-dot"></div>
            <div class="logo-text">M3 COGNITIVE</div>
        </div>
        <div style="display: flex; gap: 0.5rem; align-items: center;">
            <a href="/" class="nav-link {explorer_active}">Graph Explorer</a>
            <a href="/browse" class="nav-link {browse_active}">KB Browser</a>
        </div>
        <div class="db-status" title="Active SQLite path">
            DB: {db_path}
        </div>
        <div class="status-dot-container">
            <span style="color: hsl(210, 15%, 75%); font-family: 'Outfit', sans-serif; font-weight: 500;">Active Link</span>
            <div class="m3-status-dot" style="background-color: var(--m3-neon-emerald); box-shadow: 0 0 10px var(--m3-neon-emerald); animation: pulse-glow-emerald 2s infinite;"></div>
        </div>
    </header>
"""

STYLE_CSS = """
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

        .nav-link {
            font-family: 'Outfit', sans-serif;
            color: hsl(210, 15%, 75%);
            text-decoration: none;
            font-weight: 600;
            font-size: 0.95rem;
            padding: 0.5rem 1rem;
            border-radius: 6px;
            transition: var(--m3-transition-smooth);
            border: 1px solid transparent;
        }

        .nav-link:hover {
            color: #fff;
            background: rgba(255, 255, 255, 0.05);
        }

        .nav-link.active {
            color: var(--m3-neon-cyan);
            background: hsla(180, 100%, 50%, 0.08);
            border-color: hsla(180, 100%, 50%, 0.25);
            box-shadow: 0 0 10px hsla(180, 100%, 50%, 0.1);
        }

        .db-status {
            font-family: 'Fira Code', monospace;
            font-size: 0.8rem;
            color: hsl(210, 15%, 65%);
            background: hsla(222, 22%, 5%, 0.6);
            padding: 0.4rem 0.8rem;
            border-radius: 6px;
            border: 1px solid var(--m3-border-glass);
            max-width: 300px;
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

        @keyframes pulse-glow-emerald {
            0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.4); }
            70% { box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
            100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
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

        /* --- Interactive Search & Filters bar --- */
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

        .m3-select {
            background: hsla(222, 22%, 5%, 0.8);
            border: 1px solid var(--m3-border-glass);
            color: #fff;
            border-radius: 8px;
            padding: 0.75rem 1.5rem;
            font-family: inherit;
            font-size: 0.95rem;
            cursor: pointer;
            transition: var(--m3-transition-smooth);
        }

        .m3-select:focus {
            outline: none;
            border-color: var(--m3-neon-cyan);
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

        .badge-note { background: hsla(270, 100%, 65%, 0.12); color: var(--m3-neon-purple); border: 1px solid rgba(168, 85, 247, 0.25); }
        .badge-fact { background: hsla(145, 100%, 45%, 0.1); color: var(--m3-neon-emerald); border: 1px solid rgba(16, 185, 129, 0.25); }
        .badge-warn { background: hsla(38, 100%, 50%, 0.1); color: var(--m3-neon-amber); border: 1px solid rgba(245, 158, 11, 0.25); }
        .badge-sys { background: hsla(180, 100%, 50%, 0.1); color: var(--m3-neon-cyan); border: 1px solid rgba(6, 182, 212, 0.25); }

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

        /* --- KB Browser Progress indicators --- */
        .m3-progress-container {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            background: hsla(222, 22%, 5%, 0.6);
            border: 1px solid var(--m3-border-glass);
            border-radius: 4px;
            padding: 2px 8px;
            font-family: 'Fira Code', monospace;
            font-size: 0.75rem;
            color: #fff;
        }

        .m3-progress-bar {
            width: 60px;
            height: 6px;
            background: rgba(255, 255, 255, 0.1);
            border-radius: 3px;
            overflow: hidden;
        }

        .m3-progress-fill {
            height: 100%;
            border-radius: 3px;
            transition: var(--m3-transition-smooth);
        }

        /* --- Tags capsules --- */
        .m3-tag {
            background: hsla(270, 100%, 65%, 0.08);
            border: 1px solid rgba(168, 85, 247, 0.25);
            color: var(--m3-neon-purple);
            font-size: 0.7rem;
            padding: 0.15rem 0.45rem;
            border-radius: 4px;
            font-family: 'Outfit', sans-serif;
            font-weight: 500;
        }
"""

# --- Explorer (View 1) Layout Template ---
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
        {{ STYLE_CSS }}
    </style>
</head>
<body>

    {{ HEADER }}

    <div class="container">
        <!-- Main Panel (Left Column) -->
        <main>
            <!-- System Stats Grid -->
            <div class="metrics-grid" hx-get="/api/stats" hx-trigger="load, every 8s">
                <!-- Swapped in dynamically by HTMX -->
            </div>

            <!-- Graph Canvas Explorer -->
            <div class="m3-card">
                <div class="m3-card-title" style="margin-bottom: 0.5rem; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem;">
                    <span>Interactive Knowledge Graph</span>
                    <div style="display: flex; gap: 0.5rem; align-items: center;">
                        <button class="m3-btn" style="padding: 0.35rem 0.75rem; font-size: 0.75rem;" onclick="resetSimulation()">Reset Layout</button>
                    </div>
                </div>
                
                <!-- Controls Row -->
                <div style="display: flex; gap: 1.5rem; flex-wrap: wrap; margin-bottom: 1rem; padding: 0.5rem; background: hsla(222, 22%, 5%, 0.4); border: 1px solid var(--m3-border-glass); border-radius: 8px; font-size: 0.8rem; color: hsl(210, 15%, 80%);">
                    <div style="display: flex; align-items: center; gap: 0.5rem;">
                        <label for="reloadSlider">Data Sync:</label>
                        <select id="reloadSlider" onchange="updateReloadInterval()" style="background: var(--m3-bg-surface); border: 1px solid var(--m3-border-glass); color: #fff; border-radius: 4px; padding: 2px 6px; font-size: 0.75rem;">
                            <option value="0">Manual Only</option>
                            <option value="5000">Every 5s</option>
                            <option value="10000" selected>Every 10s</option>
                            <option value="30000">Every 30s</option>
                        </select>
                    </div>
                    <div style="display: flex; align-items: center; gap: 0.5rem;">
                        <label for="fpsSlider">Physics Target:</label>
                        <select id="fpsSlider" onchange="updateFpsLimit()" style="background: var(--m3-bg-surface); border: 1px solid var(--m3-border-glass); color: #fff; border-radius: 4px; padding: 2px 6px; font-size: 0.75rem;">
                            <option value="60">60 FPS (Fluid)</option>
                            <option value="30" selected>30 FPS (Efficient)</option>
                            <option value="10">10 FPS (Low CPU)</option>
                            <option value="0">Freeze Physics</option>
                        </select>
                    </div>
                    <div style="display: flex; align-items: center; gap: 0.5rem;">
                        <span id="physicsStatus" style="font-family: 'Fira Code', monospace; font-size: 0.7rem; color: var(--m3-neon-cyan);">Status: Active</span>
                    </div>
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

            <!-- System Diagnostics & Tasks -->
            <div class="m3-card">
                <div class="m3-card-title">System Diagnostics & Tasks</div>
                <div style="display: flex; flex-direction: column; gap: 0.5rem;">
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem;">
                        <button class="m3-btn" style="font-size: 0.8rem; padding: 0.5rem 0.25rem;" onclick="runMaintenance('decay_dry')">Decay Dry-Run</button>
                        <button class="m3-btn" style="font-size: 0.8rem; padding: 0.5rem 0.25rem;" onclick="runMaintenance('decay_apply')">Decay Apply</button>
                    </div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem;">
                        <button class="m3-btn" style="font-size: 0.8rem; padding: 0.5rem 0.25rem;" onclick="runMaintenance('embed_sweep')">Embed Sweeper</button>
                        <button class="m3-btn" style="font-size: 0.8rem; padding: 0.5rem 0.25rem;" onclick="runMaintenance('files_health')">Files Rebuild</button>
                    </div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem;">
                        <button class="m3-btn" style="font-size: 0.8rem; padding: 0.5rem 0.25rem;" onclick="runMaintenance('backfill_titles')">Backfill Titles</button>
                        <button class="m3-btn" style="font-size: 0.8rem; padding: 0.5rem 0.25rem;" onclick="runMaintenance('backfill_embeds')">Backfill Embeds</button>
                    </div>
                </div>
                <div id="maintenanceConsole" style="margin-top: 1rem; display: none;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.25rem;">
                        <span style="font-size: 0.75rem; color: var(--m3-neon-cyan); font-family: 'Outfit', sans-serif;">Console Log</span>
                        <button class="m3-btn" style="padding: 2px 6px; font-size: 0.65rem;" onclick="clearConsole()">Clear</button>
                    </div>
                    <pre id="consoleOutput" style="background: hsla(222, 22%, 5%, 0.8); border: 1px solid var(--m3-border-glass); border-radius: 6px; padding: 0.5rem; font-family: 'Fira Code', monospace; font-size: 0.7rem; color: #fff; max-height: 180px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; margin: 0;"></pre>
                </div>
            </div>

            <!-- Contradictions & Audit Feed -->
            <div class="m3-card">
                <div class="m3-card-title">
                    <span>Change & Conflict Log</span>
                </div>
                <div id="historyLog" hx-get="/api/history" hx-trigger="load, every 5s">
                    <!-- Loaded dynamically -->
                </div>
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

        let reloadTimer = null;
        let fpsLimit = 30; // default to efficient 30 FPS
        let lastFrameTime = 0;
        let isSleeping = false;
        let activeActivity = 150; // ticks remaining before sleep

        function updateReloadInterval() {
            const val = parseInt(document.getElementById("reloadSlider").value);
            if (reloadTimer) {
                clearInterval(reloadTimer);
                reloadTimer = null;
            }
            if (val > 0) {
                reloadTimer = setInterval(() => {
                    loadGraph(true); // background update
                }, val);
            }
        }

        function updateFpsLimit() {
            fpsLimit = parseInt(document.getElementById("fpsSlider").value);
            if (fpsLimit > 0) {
                wakePhysics();
            } else {
                isSleeping = true;
                document.getElementById("physicsStatus").innerText = "Status: Frozen";
                document.getElementById("physicsStatus").style.color = "var(--m3-neon-amber)";
            }
        }

        function wakePhysics() {
            activeActivity = 150;
            if (isSleeping && fpsLimit > 0) {
                isSleeping = false;
                document.getElementById("physicsStatus").innerText = "Status: Active";
                document.getElementById("physicsStatus").style.color = "var(--m3-neon-cyan)";
                requestAnimationFrame(draw);
            }
        }

        // Fetch graph and run simulation
        async function loadGraph(isBackground = false) {
            try {
                const res = await fetch("/api/graph");
                const data = await res.json();
                
                const oldNodesMap = new Map(nodes.map(n => [n.id, n]));
                
                // Initialize physics state
                nodes = data.nodes.map(n => {
                    const existing = oldNodesMap.get(n.id);
                    if (isBackground && existing) {
                        return { ...n, ...existing };
                    }
                    return {
                        ...n,
                        x: existing ? existing.x : canvas.width / 2 + (Math.random() - 0.5) * 200,
                        y: existing ? existing.y : canvas.height / 2 + (Math.random() - 0.5) * 200,
                        vx: existing ? existing.vx : 0,
                        vy: existing ? existing.vy : 0
                    };
                });
                
                // Map links to objects
                links = data.links.map(l => ({
                    ...l,
                    source: nodes.find(n => n.id === l.source),
                    target: nodes.find(n => n.id === l.target)
                })).filter(l => l.source && l.target);
                
                wakePhysics();
                
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
        function draw(timestamp) {
            if (isSleeping || fpsLimit === 0) return;
            
            // FPS limiting
            if (!lastFrameTime) lastFrameTime = timestamp;
            const elapsed = timestamp - lastFrameTime;
            
            if (elapsed < (1000 / fpsLimit)) {
                requestAnimationFrame(draw);
                return;
            }
            lastFrameTime = timestamp;
            
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
            
            // Auto sleep cooling mechanism
            let totalVelocity = 0;
            nodes.forEach(n => {
                totalVelocity += Math.abs(n.vx) + Math.abs(n.vy);
            });
            
            if (totalVelocity < 0.05 * nodes.length) {
                activeActivity--;
            } else {
                activeActivity = 120;
            }
            
            if (activeActivity <= 0) {
                isSleeping = true;
                document.getElementById("physicsStatus").innerText = "Status: Asleep (0% CPU)";
                document.getElementById("physicsStatus").style.color = "var(--m3-neon-emerald)";
            } else {
                requestAnimationFrame(draw);
            }
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
            
            wakePhysics();
            
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
                wakePhysics();
            } else if (isDraggingCanvas) {
                offsetX = dragStartOffset.x + (e.clientX - dragStartMouse.x);
                offsetY = dragStartOffset.y + (e.clientY - dragStartMouse.y);
                wakePhysics();
            }
        });

        canvas.addEventListener("mouseup", () => {
            selectedNode = null;
            isDraggingCanvas = false;
            wakePhysics();
        });

        canvas.addEventListener("mouseleave", () => {
            selectedNode = null;
            isDraggingCanvas = false;
            wakePhysics();
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
            wakePhysics();
        });

        function resetSimulation() {
            scale = 1.0;
            offsetX = 0;
            offsetY = 0;
            loadGraph();
            wakePhysics();
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

        async function runMaintenance(action) {
            const consoleDiv = document.getElementById("maintenanceConsole");
            const outputPre = document.getElementById("consoleOutput");
            consoleDiv.style.display = "block";
            outputPre.style.color = "var(--m3-neon-cyan)";
            outputPre.innerText = `[System] Triggering ${action}...\n`;
            outputPre.scrollTop = outputPre.scrollHeight;
            
            try {
                const res = await fetch(`/api/maintenance/${action}`, { method: "POST" });
                const text = await res.text();
                outputPre.style.color = res.ok ? "#fff" : "var(--m3-neon-amber)";
                outputPre.innerText += text;
            } catch(e) {
                outputPre.style.color = "var(--m3-neon-amber)";
                outputPre.innerText += `[Error] Failed to connect to server: ${e.message}\n`;
            }
            outputPre.scrollTop = outputPre.scrollHeight;
        }

        function clearConsole() {
            document.getElementById("consoleOutput").innerText = "";
            document.getElementById("maintenanceConsole").style.display = "none";
        }

        // Init loads
        loadGraph();
        updateReloadInterval();
        requestAnimationFrame(draw);
    </script>
</body>
</html>
"""

# --- Browser (View 2) Layout Template ---
BROWSE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>M3 Knowledge Base Browser</title>
    <!-- Modern Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Inter:wght@300;400;500;600&family=Outfit:wght@500;600;700&display=swap" rel="stylesheet">
    
    <!-- HTMX -->
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    
    <style>
        {{ STYLE_CSS }}
        
        .browse-container {
            max-width: 1200px;
            width: 100%;
            margin: 2rem auto;
            padding: 0 1.5rem;
            flex-grow: 1;
        }
        
        .filter-panel {
            display: grid;
            grid-template-columns: 2fr 1fr 120px;
            gap: 1rem;
            margin-bottom: 2rem;
        }

        @media (max-width: 768px) {
            .filter-panel {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>

    {{ HEADER }}

    <div class="browse-container">
        <!-- Filter Bar Panel -->
        <div class="m3-card" style="margin-bottom: 2rem;">
            <div class="m3-card-title">Filter & Search Knowledge base</div>
            <div class="filter-panel">
                <input type="text" name="q" class="m3-input" placeholder="Search keywords in title or content..."
                       hx-get="/api/kb" hx-target="#kbEntries" hx-trigger="keyup changed delay:300ms, filter" hx-include="[name='type'], [name='limit']">
                
                <select name="type" class="m3-select" hx-get="/api/kb" hx-target="#kbEntries" hx-trigger="change" hx-include="[name='q'], [name='limit']">
                    <option value="">-- All Types --</option>
                    <option value="fact">Fact</option>
                    <option value="decision">Decision</option>
                    <option value="knowledge">Knowledge</option>
                    <option value="project">Project</option>
                    <option value="note">Note</option>
                    <option value="network_config">Network Config</option>
                    <option value="infrastructure">Infrastructure</option>
                    <option value="reference">Reference</option>
                </select>
                
                <select name="limit" class="m3-select" hx-get="/api/kb" hx-target="#kbEntries" hx-trigger="change" hx-include="[name='q'], [name='type']">
                    <option value="20">Top 20</option>
                    <option value="50" selected>Top 50</option>
                    <option value="100">Top 100</option>
                    <option value="1000">All</option>
                </select>
            </div>
        </div>

        <!-- Rendered Cards -->
        <div id="kbEntries" hx-get="/api/kb" hx-trigger="load">
            <!-- Loaded dynamically by HTMX -->
            <p style="text-align: center; color: hsl(210, 15%, 65%); padding: 3rem 0;">Scanning knowledge index...</p>
        </div>
    </div>

</body>
</html>
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
    active_db = resolve_db_path(None)
    header = HEADER_HTML.format(explorer_active="active", browse_active="", db_path=active_db)
    return INDEX_HTML.replace("{{ STYLE_CSS }}", STYLE_CSS).replace("{{ HEADER }}", header).replace("{{ db_path }}", active_db)


@app.get("/browse", response_class=HTMLResponse)
async def get_browse(request: Request):
    active_db = resolve_db_path(None)
    header = HEADER_HTML.format(explorer_active="", browse_active="active", db_path=active_db)
    return BROWSE_HTML.replace("{{ STYLE_CSS }}", STYLE_CSS).replace("{{ HEADER }}", header).replace("{{ db_path }}", active_db)


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
        return "\n".join(cards)
        
    except Exception as e:
        return f'<p style="color: var(--m3-neon-amber); text-align: center; padding: 2rem 0;">Error scanning indices: {str(e)}</p>'


@app.get("/api/kb", response_class=HTMLResponse)
async def get_kb_cards(q: str = "", type: str = "", limit: int = 50):
    """Replicates cli_kb_browse.py rank search directly into high-fidelity web cards."""
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
        
        with _db() as db:
            rows = db.execute(query, params).fetchall()
            
        total = len(rows)
        if total == 0:
            return f'<p style="text-align: center; color: hsl(210, 15%, 65%); padding: 3rem 0;">No matching entries found.</p>'
            
        cards = []
        for idx, row in enumerate(rows, 1):
            importance = row["importance"] or 0.0
            bar_color = "var(--m3-neon-purple)"
            if importance >= 0.9:
                bar_color = "var(--m3-neon-emerald)"
            elif importance >= 0.7:
                bar_color = "var(--m3-neon-amber)"
                
            badge_class = "badge-note"
            if row["type"] == "fact":
                badge_class = "badge-fact"
            elif row["type"] == "decision":
                badge_class = "badge-warn"
            elif row["type"] in ("knowledge", "network_config", "infrastructure"):
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
async def get_history_feed():
    """Queries the memory_history table for the conflict log."""
    logs = []
    try:
        with _db() as db:
            hist_exists = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_history'").fetchone()
            if hist_exists:
                # Standardized history query based on migrations: event instead of action, prev_value/new_value instead of prev_content/new_content, created_at instead of timestamp
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


@app.post("/api/maintenance/{action}", response_class=HTMLResponse)
async def run_maintenance_task(action: str):
    """Executes backend maintenance actions similar to TUI options."""
    import subprocess
    
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
        "backfill_titles": [sys.executable, os.path.join(os.path.dirname(__file__), "m3_chatlog_backfill_title.py")],
        "backfill_embeds": [sys.executable, os.path.join(os.path.dirname(__file__), "m3_chatlog_backfill_embed.py")],
        "files_health": [sys.executable, "-m", "files_memory.tools", "health", "--rebuild"]
    }
    
    if action not in cmd_map:
        raise HTTPException(status_code=400, detail="Invalid action")
        
    cmd = cmd_map[action]
    
    # Run command inside a thread to keep FastAPI non-blocking
    def run_cmd():
        env = os.environ.copy()
        # Add bin directory to PYTHONPATH so files_memory is importable
        bin_dir = os.path.dirname(os.path.abspath(__file__))
        if "PYTHONPATH" in env:
            env["PYTHONPATH"] = bin_dir + os.pathsep + env["PYTHONPATH"]
        else:
            env["PYTHONPATH"] = bin_dir
            
        try:
            res = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, shell=False)
            return res.stdout + "\n" + res.stderr + f"\nProcess exited with code {res.returncode}"
        except Exception as e:
            return f"Error executing process: {str(e)}"
            
    output = await asyncio.to_thread(run_cmd)
    return HTMLResponse(content=output, status_code=200)


# --- Execution Hook ---
if __name__ == "__main__":
    port_override = int(os.environ.get("M3_DASHBOARD_PORT", PORT))
    host_override = os.environ.get("M3_DASHBOARD_HOST", HOST)
    
    uvicorn.run("dashboard_server:app", host=host_override, port=port_override, reload=False)
