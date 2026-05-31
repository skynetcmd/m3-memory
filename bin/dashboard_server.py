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
        
        {db_selector_html}
        
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
            grid-template-columns: repeat(6, 1fr);
            gap: 1rem;
            margin-bottom: 2rem;
        }

        @media (max-width: 1200px) {
            .metrics-grid {
                grid-template-columns: repeat(3, 1fr);
            }
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

        /* --- Premium Custom HTML Tooltips --- */
        .tooltip-container {
            position: relative;
        }

        .tooltip-container .m3-tooltip {
            position: absolute;
            bottom: 125%; /* Positioned above the button, safe from large cursors */
            left: 50%;
            transform: translateX(-50%) scale(0.95);
            background: hsla(224, 25%, 5%, 0.98);
            backdrop-filter: blur(8px);
            -webkit-backdrop-filter: blur(8px);
            border: 1px solid var(--m3-border-glass);
            border-radius: 6px;
            color: hsl(210, 15%, 90%);
            padding: 0.5rem 0.75rem;
            font-size: 0.72rem;
            font-family: 'Inter', sans-serif;
            font-weight: normal;
            line-height: 1.45;
            width: 240px; /* Constrain width to force elegant wrapping */
            white-space: normal;
            word-wrap: break-word;
            box-shadow: var(--m3-shadow-card);
            opacity: 0;
            visibility: hidden;
            pointer-events: none;
            transition: opacity 0.15s ease, transform 0.15s ease, visibility 0.15s ease;
            z-index: 1000;
            text-align: center;
        }

        .tooltip-container:hover .m3-tooltip {
            opacity: 1;
            visibility: visible;
            transform: translateX(-50%) scale(1);
        }

        /* --- DB Selector Menu --- */
        .db-selector-container {
            position: relative;
            display: inline-block;
        }

        .db-selector-btn {
            font-family: 'Outfit', sans-serif;
            font-size: 0.85rem;
            font-weight: 600;
            color: hsl(210, 15%, 85%);
            background: hsla(222, 22%, 5%, 0.6);
            border: 1px solid var(--m3-border-glass);
            border-radius: 8px;
            padding: 0.5rem 1rem;
            display: flex;
            align-items: center;
            gap: 0.6rem;
            cursor: pointer;
            transition: var(--m3-transition-smooth);
        }

        .db-selector-btn:hover {
            color: #fff;
            border-color: var(--m3-neon-cyan);
            box-shadow: 0 0 10px hsla(180, 100%, 50%, 0.15);
        }

        .db-menu {
            display: none;
            position: absolute;
            top: calc(100% + 0.5rem);
            right: 0;
            width: 320px;
            background: hsla(224, 25%, 5%, 0.98);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid var(--m3-border-glass);
            border-radius: 10px;
            box-shadow: var(--m3-shadow-card);
            z-index: 1000;
            padding: 0.5rem;
            flex-direction: column;
            gap: 0.25rem;
            animation: fadeInMenu 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }

        @keyframes fadeInMenu {
            from { opacity: 0; transform: translateY(-5px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .db-menu.show {
            display: flex;
        }

        .db-menu-header {
            font-family: 'Outfit', sans-serif;
            font-size: 0.75rem;
            font-weight: 700;
            color: hsl(210, 10%, 55%);
            text-transform: uppercase;
            letter-spacing: 0.08em;
            padding: 0.5rem;
            border-bottom: 1px solid var(--m3-border-glass);
            margin-bottom: 0.25rem;
        }

        .db-menu-item {
            padding: 0.75rem;
            border-radius: 6px;
            border-left: 3px solid transparent;
            cursor: pointer;
            transition: var(--m3-transition-smooth);
            background: transparent;
            text-align: left;
        }

        .db-menu-item:hover {
            background: hsla(222, 22%, 15%, 0.5);
            border-left-color: hsl(210, 10%, 45%);
        }

        .db-menu-item.active {
            background: hsla(180, 100%, 50%, 0.05);
            border-left-color: var(--m3-neon-cyan);
            box-shadow: inset 0 0 10px hsla(180, 100%, 50%, 0.02);
        }
        
        .db-menu-item.active[onclick*="chatlog"] {
            background: hsla(300, 100%, 65%, 0.05);
            border-left-color: hsl(300, 100%, 65%);
        }

        .db-menu-item.active[onclick*="files"] {
            background: hsla(145, 100%, 45%, 0.05);
            border-left-color: var(--m3-neon-emerald);
        }

        .db-item-title-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.15rem;
        }

        .db-item-title {
            font-family: 'Outfit', sans-serif;
            font-weight: 600;
            font-size: 0.9rem;
            color: #fff;
        }

        .db-item-size {
            font-family: 'Fira Code', monospace;
            font-size: 0.75rem;
            color: hsl(210, 15%, 65%);
        }

        .db-item-meta {
            font-family: 'Fira Code', monospace;
            font-size: 0.7rem;
            color: var(--m3-neon-cyan);
            margin-bottom: 0.25rem;
        }
        
        .db-menu-item[onclick*="chatlog"] .db-item-meta {
            color: hsl(300, 100%, 65%);
        }
        
        .db-menu-item[onclick*="files"] .db-item-meta {
            color: var(--m3-neon-emerald);
        }

        .db-item-desc {
            font-size: 0.72rem;
            color: hsl(210, 10%, 70%);
            line-height: 1.35;
        }

        /* --- Dynamic Metrics Grid Highlights --- */
        .metric-card {
            transition: var(--m3-transition-smooth);
        }
        .metric-card.highlight-main {
            border-color: var(--m3-neon-cyan);
            box-shadow: 0 0 15px hsla(180, 100%, 50%, 0.15);
            background: hsla(180, 100%, 50%, 0.03);
        }
        .metric-card.highlight-chatlog {
            border-color: hsl(300, 100%, 65%);
            box-shadow: 0 0 15px hsla(300, 100%, 65%, 0.15);
            background: hsla(300, 100%, 65%, 0.03);
        }
        .metric-card.highlight-files {
            border-color: var(--m3-neon-emerald);
            box-shadow: 0 0 15px hsla(145, 100%, 45%, 0.15);
            background: hsla(145, 100%, 45%, 0.03);
        }

        /* --- Alert Banners --- */
        .m3-alert-banner {
            grid-column: 1 / -1;
            display: flex;
            align-items: center;
            gap: 0.75rem;
            padding: 0.75rem 1rem;
            border-radius: 8px;
            font-size: 0.82rem;
            line-height: 1.45;
            border: 1px solid var(--m3-border-glass);
            margin-bottom: 0.5rem;
            text-align: left;
        }

        .m3-alert-banner strong {
            font-family: 'Outfit', sans-serif;
            font-weight: 600;
        }

        .m3-alert-banner.banner-main {
            background: hsla(180, 100%, 50%, 0.03);
            border-color: hsla(180, 100%, 50%, 0.15);
            color: hsl(180, 15%, 85%);
        }
        
        .m3-alert-banner.banner-main svg {
            color: var(--m3-neon-cyan);
        }

        .m3-alert-banner.banner-chatlog {
            background: hsla(300, 100%, 65%, 0.03);
            border-color: hsla(300, 100%, 65%, 0.15);
            color: hsl(300, 15%, 85%);
        }
        
        .m3-alert-banner.banner-chatlog svg {
            color: hsl(300, 100%, 65%);
        }

        .m3-alert-banner.banner-files {
            background: hsla(145, 100%, 45%, 0.03);
            border-color: hsla(145, 100%, 45%, 0.15);
            color: hsl(145, 15%, 85%);
        }
        
        .m3-alert-banner.banner-files svg {
            color: var(--m3-neon-emerald);
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
                        <button class="m3-btn tooltip-container" style="font-size: 0.8rem; padding: 0.5rem 0.25rem;" onclick="runMaintenance('decay_dry')">
                            Decay Dry-Run
                            <span class="m3-tooltip">Preview memory decay and expiration scores. Safe dry-run, <strong style="color: var(--m3-neon-cyan);">no database edits.</strong></span>
                        </button>
                        <button class="m3-btn tooltip-container" style="font-size: 0.8rem; padding: 0.5rem 0.25rem;" onclick="runMaintenance('decay_apply')">
                            Decay Apply
                            <span class="m3-tooltip">Calculate and commit memory decay scores, prune expired items, and enforce retention limits. <strong style="color: var(--m3-neon-amber);">Modifies DB.</strong></span>
                        </button>
                    </div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem;">
                        <button class="m3-btn tooltip-container" style="font-size: 0.8rem; padding: 0.5rem 0.25rem;" onclick="runMaintenance('embed_sweep')">
                            Embed Sweeper
                            <span class="m3-tooltip">Sweep and process pending entity extraction queue tasks, draining and compacting spill jobs.</span>
                        </button>
                        <button class="m3-btn tooltip-container" style="font-size: 0.8rem; padding: 0.5rem 0.25rem;" onclick="runMaintenance('files_health')">
                            Files Rebuild
                            <span class="m3-tooltip">Scan Files database integrity, chunk document segments, and force rebuilding of index indices.</span>
                        </button>
                    </div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem;">
                        <button class="m3-btn tooltip-container" style="font-size: 0.8rem; padding: 0.5rem 0.25rem;" onclick="runMaintenance('backfill_titles')">
                            Backfill Titles
                            <span class="m3-tooltip">Derive titles for unnamed or generic entries automatically. <strong style="color: hsl(15, 100%, 55%); font-weight: 700;">Automatically confirms ('--yes') and applies changes.</strong></span>
                        </button>
                        <button class="m3-btn tooltip-container" style="font-size: 0.8rem; padding: 0.5rem 0.25rem;" onclick="runMaintenance('backfill_embeds')">
                            Backfill Embeds
                            <span class="m3-tooltip">Generate missing vector embeddings for database facts and log records automatically. <strong style="color: hsl(15, 100%, 55%); font-weight: 700;">Automatically confirms ('--yes') and applies changes.</strong></span>
                        </button>
                    </div>
                </div>
                <div id="maintenanceConsole" style="margin-top: 1rem; display: none;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.25rem;">
                        <span style="font-size: 0.75rem; color: var(--m3-neon-cyan); font-family: 'Outfit', sans-serif;">Console Log</span>
                        <button class="m3-btn" style="padding: 2px 6px; font-size: 0.65rem;" onclick="clearConsole()">Clear</button>
                    </div>
                    <pre id="consoleOutput" style="background: hsla(222, 22%, 5%, 0.8); border: 1px solid var(--m3-border-glass); border-radius: 6px; padding: 0.75rem; font-family: 'Fira Code', Consolas, Monaco, 'Andale Mono', 'Ubuntu Mono', monospace; font-size: 0.72rem; line-height: 1.45; color: hsl(210, 15%, 90%); max-height: 200px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; margin: 0;"></pre>
                </div>
            </div>

            <!-- Conceptual Guide Card -->
            <div class="m3-card" style="margin-bottom: 2rem; border-color: hsla(270, 100%, 65%, 0.15);">
                <div class="m3-card-title" style="color: var(--m3-neon-purple); font-size: 1.1rem;">
                    <span>💡 Memory Browser vs. KB Browser</span>
                </div>
                <div style="font-size: 0.8rem; line-height: 1.45; color: hsl(210, 10%, 75%); display: flex; flex-direction: column; gap: 0.75rem;">
                    <p>
                        The <strong>Memory Browser</strong> is an <em>observability and explanation engine</em>. It queries raw cognitive layers (FTS5 full text search & vector cosine spaces) to show you the exact math behind memory retrieval.
                    </p>
                    <div style="background: hsla(222, 22%, 5%, 0.5); padding: 0.5rem 0.75rem; border-radius: 6px; border: 1px solid var(--m3-border-glass); font-family: 'Fira Code', monospace; font-size: 0.7rem; color: var(--m3-neon-cyan);">
                        Vector Scores + BM25 Hybrid Fusion + MMR Penalty
                    </div>
                    <p>
                        The <a href="/browse" style="color: var(--m3-neon-cyan); text-decoration: none; font-weight: 600;">KB Browser &rarr;</a> is a <em>curated semantic workspace</em> designed for catalog browsing and card curation, displaying rank-prioritized entities with tag attributes.
                    </p>
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

        let maintenancePollTimer = null;

        async function runMaintenance(action) {
            const consoleDiv = document.getElementById("maintenanceConsole");
            const outputPre = document.getElementById("consoleOutput");
            consoleDiv.style.display = "block";
            outputPre.style.color = "var(--m3-neon-cyan)";
            outputPre.innerText = `[System] Triggering ${action}...\n`;
            outputPre.scrollTop = outputPre.scrollHeight;
            
            if (maintenancePollTimer) {
                clearInterval(maintenancePollTimer);
                maintenancePollTimer = null;
            }
            
            try {
                const res = await fetch(`/api/maintenance/trigger/${action}`, { method: "POST" });
                const text = await res.text();
                
                if (!res.ok) {
                    outputPre.style.color = "var(--m3-neon-amber)";
                    outputPre.innerText += text;
                    outputPre.scrollTop = outputPre.scrollHeight;
                    return;
                }
                
                outputPre.style.color = "#fff";
                outputPre.innerText = text + "\\n";
                outputPre.scrollTop = outputPre.scrollHeight;
                
                // Start polling logs
                maintenancePollTimer = setInterval(pollMaintenanceStatus, 1500);
            } catch(e) {
                outputPre.style.color = "var(--m3-neon-amber)";
                outputPre.innerText += `[Error] Failed to trigger task: ${e.message}\n`;
                outputPre.scrollTop = outputPre.scrollHeight;
            }
        }

        async function pollMaintenanceStatus() {
            const outputPre = document.getElementById("consoleOutput");
            try {
                const res = await fetch("/api/maintenance/status");
                if (!res.ok) return;
                const data = await res.json();
                
                outputPre.innerText = data.logs;
                
                if (data.status === "finished") {
                    clearInterval(maintenancePollTimer);
                    maintenancePollTimer = null;
                    const color = data.exit_code === 0 ? "var(--m3-neon-emerald)" : "var(--m3-neon-amber)";
                    outputPre.innerHTML += `\n<span style="color: ${color}; font-weight: 600;">[System] Task finished with exit code ${data.exit_code}.</span>\n`;
                }
                outputPre.scrollTop = outputPre.scrollHeight;
            } catch(e) {
                console.error("Failed to poll status", e);
            }
        }

        function clearConsole() {
            if (maintenancePollTimer) {
                clearInterval(maintenancePollTimer);
                maintenancePollTimer = null;
            }
            document.getElementById("consoleOutput").innerText = "";
            document.getElementById("maintenanceConsole").style.display = "none";
        }

        // Database Selector Javascript
        function toggleDbMenu(event) {
            event.stopPropagation();
            const menu = document.getElementById("dbMenu");
            menu.classList.toggle("show");
        }

        function selectDatabase(dbName) {
            document.cookie = "selected_db=" + dbName + "; path=/; max-age=31536000; SameSite=Lax";
            window.location.reload();
        }

        // Close dropdown on click outside
        window.addEventListener("click", function(event) {
            const menu = document.getElementById("dbMenu");
            if (menu && menu.classList.contains("show")) {
                menu.classList.remove("show");
            }
        });

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

        <!-- Conceptual Guide Card -->
        <div class="m3-card" style="margin-top: 3rem; margin-bottom: 2rem; border-color: hsla(270, 100%, 65%, 0.15); max-width: 800px; margin-left: auto; margin-right: auto;">
            <div class="m3-card-title" style="color: var(--m3-neon-purple); font-size: 1.1rem;">
                <span>💡 KB Browser vs. Memory Browser</span>
            </div>
            <div style="font-size: 0.85rem; line-height: 1.5; color: hsl(210, 10%, 75%); display: flex; flex-direction: column; gap: 0.75rem;">
                <p>
                    The <strong>KB Browser</strong> is a <em>curated semantic workspace</em> designed for catalog browsing, card curation, and category reviews. It visualizes rank-prioritized decisions, facts, references, and configurations.
                </p>
                <p>
                    The <a href="/" style="color: var(--m3-neon-cyan); text-decoration: none; font-weight: 600;">Memory Browser & Explain Engine &rarr;</a> is an <em>observability portal</em>. It exposes the raw vector similarity math, FTS5 BM25 matches, and MMR diversity rerank penalties, allowing engineers to trace exactly how the system scores and retrieves memories.
                </p>
            </div>
        </div>
    </div>

    <!-- Database Selector Script -->
    <script>
        function toggleDbMenu(event) {
            event.stopPropagation();
            const menu = document.getElementById("dbMenu");
            menu.classList.toggle("show");
        }

        function selectDatabase(dbName) {
            document.cookie = "selected_db=" + dbName + "; path=/; max-age=31536000; SameSite=Lax";
            window.location.reload();
        }

        // Close dropdown on click outside
        window.addEventListener("click", function(event) {
            const menu = document.getElementById("dbMenu");
            if (menu && menu.classList.contains("show")) {
                menu.classList.remove("show");
            }
        });
    </script>
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
# --- Helpers & DB Selectors ---
_DB_PATHS = None

def get_db_paths() -> dict[str, str]:
    global _DB_PATHS
    if _DB_PATHS is None:
        from m3_sdk import resolve_db_path
        from chatlog_config import DEFAULT_DB_PATH
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
    header = HEADER_HTML.format(explorer_active="active", browse_active="", db_selector_html=db_selector_html)
    content = INDEX_HTML.replace("{{ STYLE_CSS }}", STYLE_CSS).replace("{{ HEADER }}", header).replace("{{ db_path }}", selected_db_path)
    return HTMLResponse(content=content, status_code=200, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/browse", response_class=HTMLResponse)
async def get_browse(request: Request):
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
    set_active_db_env(selected_db)
    
    db_selector_html = build_db_selector_html(selected_db)
    header = HEADER_HTML.format(explorer_active="", browse_active="active", db_selector_html=db_selector_html)
    content = BROWSE_HTML.replace("{{ STYLE_CSS }}", STYLE_CSS).replace("{{ HEADER }}", header).replace("{{ db_path }}", selected_db_path)
    return HTMLResponse(content=content, status_code=200, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.get("/api/stats", response_class=HTMLResponse)
async def get_stats(request: Request):
    """Returns dynamic HTML stats counters cards across Main, Chatlog and Files DBs."""
    selected_db = request.cookies.get("selected_db", "main")
    selected_db_path = get_active_db_path(request)
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
    
    from m3_sdk import resolve_db_path
    from chatlog_config import DEFAULT_DB_PATH
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
                if item.get("type") == "fact":
                    badge_class = "badge-fact"
                elif item.get("type") == "contradiction":
                    badge_class = "badge-warn"
                elif item.get("type") == "chat_log":
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
            params.append(int(limit))
            
            with sqlite3.connect(selected_db_path, timeout=5.0) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query, params).fetchall()
                
            total = len(rows)
            if total == 0:
                return f'<p style="text-align: center; color: hsl(210, 15%, 65%); padding: 3rem 0;">No matching ingested files found.</p>'
                
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
            elif row["type"] == "chat_log":
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


active_task = {
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
