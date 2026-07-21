"""HTML/CSS template constants for the m3 dashboard.

Extracted verbatim from bin/dashboard_server.py (behavior-preserving).
HEADER_HTML uses str.format() placeholders ({explorer_active}, ...);
STYLE_CSS / *_HTML use {{ MARKER }} tokens replaced via str.replace() by
the route handlers. Do not add logic here.
"""

HEADER_HTML = """
    <header>
        <div class="logo-group">
            <div class="m3-status-dot"></div>
            <div class="logo-text">M3 COGNITIVE</div>
        </div>
        <div style="display: flex; gap: 0.5rem; align-items: center;">
            <a href="/" class="nav-link {explorer_active}">Graph Explorer</a>
            <a href="/browse" class="nav-link {browse_active}">KB Browser</a>
            <a href="/audit" class="nav-link {audit_active}">Conflict & Audit Log</a>
            <a href="/wiki" class="nav-link {wiki_active}">Wiki</a>
            <a href="/health" class="nav-link {health_active}">System Health</a>
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
            <div class="metrics-grid" hx-get="/api/stats" hx-trigger="load, every 8s, refreshStats">
                <!-- Swapped in dynamically by HTMX -->
            </div>

            <!-- Pipeline / Governor telemetry lives on the System Health tab
                 (governor load + per-queue drain), not here — the Graph Explorer
                 is for exploring the knowledge graph, not process monitoring. -->

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
                    <div style="display: flex; align-items: center; gap: 0.5rem; margin-left: auto;">
                        <button type="button" onclick="openGraphWindow()" title="Open the interactive graph in its own resizable browser window"
                                style="background: var(--m3-bg-surface); border: 1px solid var(--m3-border-glass); color: var(--m3-neon-cyan); border-radius: 4px; padding: 3px 10px; font-size: 0.75rem; cursor: pointer;">
                            ⇱ Open in window
                        </button>
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
                    <input id="memQ" type="text" name="q" class="m3-input" placeholder="e.g.  UAC +window   -&quot;malformed string&quot;"
                           hx-get="/api/search" hx-target="#searchResults" hx-trigger="keyup changed delay:350ms, search"
                           hx-include="#igcase" autocomplete="off">
                </div>
                <div style="display:flex; align-items:center; gap:.75rem; margin:.4rem 0 .2rem; font-size:.78rem; color:hsl(210,15%,70%);">
                    <label style="display:flex; align-items:center; gap:.35rem; cursor:pointer; user-select:none;">
                        <input id="igcase" type="checkbox" name="ignore_case" value="1" checked
                               hx-get="/api/search" hx-target="#searchResults" hx-trigger="change" hx-include="#memQ">
                        ignore case
                    </label>
                    <span onclick="var b=document.getElementById('qhelp'); b.style.display = b.style.display==='block'?'none':'block';"
                          style="cursor:pointer; color:var(--m3-neon-cyan);">&#9432; search syntax</span>
                </div>
                <div id="qhelp" style="display:none; font-size:.75rem; color:hsl(210,15%,72%); background:hsla(222,22%,6%,.6);
                     border:1px solid var(--m3-border-glass); border-radius:8px; padding:.6rem .8rem; margin-bottom:.6rem; line-height:1.55;">
                    <strong style="color:#fff;">Search syntax</strong> — words are filters, all must match (AND):
                    <ul style="margin:.35rem 0 0 1.1rem; padding:0;">
                        <li><code style="color:var(--m3-neon-cyan);">UAC window</code> — memories with <em>both</em> UAC and window (same as <code>+UAC +window</code>).</li>
                        <li><code style="color:var(--m3-neon-cyan);">-window</code> — must <em>not</em> contain window.</li>
                        <li><code style="color:var(--m3-neon-cyan);">"malformed string"</code> — the exact phrase (not the words separately).</li>
                        <li><code style="color:var(--m3-neon-cyan);">UAC -"malformed string"</code> — has UAC, but <em>not</em> that phrase.</li>
                        <li><em>ignore case</em> on = <code>UAC</code> matches <code>uac</code>; uncheck for exact case.</li>
                    </ul>
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
                        <button class="m3-btn tooltip-container" style="font-size: 0.92rem; padding: 0.6rem 0.35rem;" onclick="runMaintenance('decay_dry')">
                            Decay Dry-Run
                            <span class="m3-tooltip">Preview memory decay and expiration scores. Safe dry-run, <strong style="color: var(--m3-neon-cyan);">no database edits.</strong></span>
                        </button>
                        <button class="m3-btn tooltip-container" style="font-size: 0.92rem; padding: 0.6rem 0.35rem;" onclick="runMaintenance('decay_apply')">
                            Decay Apply
                            <span class="m3-tooltip">Calculate and commit memory decay scores, prune expired items, and enforce retention limits. <strong style="color: var(--m3-neon-amber);">Modifies DB.</strong></span>
                        </button>
                    </div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem;">
                        <button class="m3-btn tooltip-container" style="font-size: 0.92rem; padding: 0.6rem 0.35rem;" onclick="runMaintenance('embed_sweep')">
                            Embed Sweeper
                            <span class="m3-tooltip">Sweep and process pending entity extraction queue tasks, draining and compacting spill jobs.</span>
                        </button>
                        <button class="m3-btn tooltip-container" style="font-size: 0.92rem; padding: 0.6rem 0.35rem;" onclick="runMaintenance('files_health')">
                            Files Rebuild
                            <span class="m3-tooltip">Scan Files database integrity, chunk document segments, and force rebuilding of index indices.</span>
                        </button>
                    </div>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem;">
                        <button class="m3-btn tooltip-container" style="font-size: 0.92rem; padding: 0.6rem 0.35rem;" onclick="runMaintenance('backfill_titles')">
                            Backfill Titles
                            <span class="m3-tooltip">Derive titles for unnamed or generic entries automatically. <strong style="color: hsl(15, 100%, 55%); font-weight: 700;">Automatically confirms ('--yes') and applies changes.</strong></span>
                        </button>
                        <button class="m3-btn tooltip-container" style="font-size: 0.92rem; padding: 0.6rem 0.35rem;" onclick="runMaintenance('backfill_embeds')">
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
                    <pre id="consoleOutput" style="background: hsla(222, 22%, 5%, 0.8); border: 1px solid var(--m3-border-glass); border-radius: 6px; padding: 0.75rem; font-family: 'Fira Code', Consolas, Monaco, 'Andale Mono', 'Ubuntu Mono', monospace; font-size: 0.82rem; line-height: 1.45; color: hsl(210, 15%, 90%); max-height: 200px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; margin: 0;"></pre>
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

        // Open the interactive graph in its own dedicated browser window. The
        // standalone /graph page renders the same canvas full-window (same data
        // source /api/graph). Sized to a comfortable default; resizable.
        function openGraphWindow() {
            const w = Math.min(1400, Math.round(screen.availWidth * 0.8));
            const h = Math.min(900, Math.round(screen.availHeight * 0.85));
            const left = Math.round((screen.availWidth - w) / 2);
            const top = Math.round((screen.availHeight - h) / 2);
            window.open("/graph", "m3GraphWindow",
                `width=${w},height=${h},left=${left},top=${top},resizable=yes,scrollbars=no`);
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
                    if (data.exit_code === 0) {
                        try {
                            htmx.trigger(".metrics-grid", "refreshStats");
                        } catch(err) {
                            console.error("HTMX trigger failed", err);
                        }
                    }
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
                    <option value="note">Note</option>
                    <option value="summary">Summary</option>
                    <option value="fact">Fact</option>
                    <option value="decision">Decision</option>
                    <option value="task">Task</option>
                    <option value="plan">Plan</option>
                    <option value="preference">Preference</option>
                    <option value="to_do">To-Do</option>
                    <option value="knowledge">Knowledge</option>
                    <option value="project">Project</option>
                    <option value="local_device">Local Device</option>
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

# --- Audit & Timeline (View 3) Layout Template ---
AUDIT_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>M3 Conflict & Audit Log</title>
    <!-- Modern Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Inter:wght@300;400;500;600&family=Outfit:wght@500;600;700&display=swap" rel="stylesheet">

    <!-- HTMX -->
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>

    <style>
        {{ STYLE_CSS }}

        .audit-container {
            max-width: 1200px;
            width: 100%;
            margin: 2rem auto;
            padding: 0 1.5rem;
            flex-grow: 1;
        }

        .timeline-container {
            display: flex;
            flex-direction: column;
            gap: 2.5rem;
            margin-top: 2rem;
        }

        .timeline-group-card {
            background: var(--m3-bg-card-glass);
            border: 1px solid var(--m3-border-glass);
            border-radius: 12px;
            padding: 1.5rem;
            box-shadow: var(--m3-shadow-card);
            position: relative;
            overflow: hidden;
            transition: var(--m3-transition-smooth);
        }

        .timeline-group-card:hover {
            border-color: hsla(180, 100%, 50%, 0.25);
            box-shadow: var(--m3-shadow-glow);
        }

        /* Vertical line for the timeline */
        .timeline-flow {
            position: relative;
            padding-left: 2.5rem;
            margin-top: 1.5rem;
        }

        .timeline-flow::before {
            content: '';
            position: absolute;
            left: 11px;
            top: 5px;
            bottom: 5px;
            width: 2px;
            background: hsla(217, 19%, 27%, 0.5);
        }

        .timeline-node {
            position: relative;
            margin-bottom: 1.5rem;
        }

        .timeline-node:last-child {
            margin-bottom: 0;
        }

        .timeline-badge {
            position: absolute;
            left: -2.5rem;
            top: 2px;
            width: 24px;
            height: 24px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.7rem;
            font-weight: 700;
            color: #000;
            z-index: 2;
            box-shadow: 0 0 8px rgba(0,0,0,0.5);
        }

        .badge-create { background: var(--m3-neon-emerald); box-shadow: 0 0 10px rgba(16, 185, 129, 0.4); }
        .badge-update { background: var(--m3-neon-cyan); box-shadow: 0 0 10px rgba(6, 182, 212, 0.4); }
        .badge-supersede { background: var(--m3-neon-amber); box-shadow: 0 0 10px rgba(245, 158, 11, 0.4); }
        .badge-contradiction { background: var(--m3-neon-amber); box-shadow: 0 0 10px rgba(245, 158, 11, 0.4); }
        .badge-delete { background: hsl(15, 100%, 55%); box-shadow: 0 0 10px rgba(239, 68, 68, 0.4); }
        .badge-resolve { background: var(--m3-neon-purple); box-shadow: 0 0 10px rgba(168, 85, 247, 0.4); }

        .timeline-content-box {
            background: hsla(222, 22%, 5%, 0.4);
            border: 1px solid var(--m3-border-glass);
            border-radius: 8px;
            padding: 0.75rem 1rem;
        }

        .diff-text {
            font-family: 'Fira Code', monospace;
            font-size: 0.8rem;
            white-space: pre-wrap;
            line-height: 1.45;
            color: hsl(210, 15%, 85%);
            background: hsla(222, 22%, 3%, 0.6);
            padding: 0.5rem 0.75rem;
            border-radius: 6px;
            margin-top: 0.5rem;
            border: 1px solid rgba(255,255,255,0.03);
        }

        .filter-panel {
            display: grid;
            grid-template-columns: 2fr 1fr;
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

    <div class="audit-container">
        <!-- Title & Desc -->
        <div class="m3-card" style="margin-bottom: 2rem;">
            <div class="m3-card-title" style="font-size: 1.4rem; color: var(--m3-neon-cyan);">Conflict & Audit Log</div>
            <p style="font-size: 0.88rem; color: hsl(210, 15%, 75%); line-height: 1.5; margin-top: 0.5rem;">
                Explore raw bitemporal change timelines. This panel detects, groups, and lets you resolve contradictions, supersession history, and change logs. It shows interactive word-level <strong>Diff Blocks</strong> comparing previous vs. new values.
            </p>
        </div>

        <!-- Filter Panel -->
        <div class="m3-card" style="margin-bottom: 2rem;">
            <div class="m3-card-title">Filter Conflict Timelines</div>
            <div class="filter-panel">
                <input type="text" name="q" class="m3-input" placeholder="Search memory ID, title or content keywords..."
                       hx-get="/api/audit/timeline" hx-target="#auditTimeline" hx-trigger="keyup changed delay:300ms, filter" hx-include="[name='limit']">

                <select name="limit" class="m3-select" hx-get="/api/audit/timeline" hx-target="#auditTimeline" hx-trigger="change" hx-include="[name='q']">
                    <option value="10">Latest 10 Timelines</option>
                    <option value="25" selected>Latest 25 Timelines</option>
                    <option value="50">Latest 50 Timelines</option>
                    <option value="100">Latest 100 Timelines</option>
                </select>
            </div>
        </div>

        <!-- Timeline Results -->
        <div id="auditTimeline" hx-get="/api/audit/timeline" hx-trigger="load">
            <p style="text-align: center; color: hsl(210, 15%, 65%); padding: 3rem 0;">Scanning memory history database...</p>
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


# Wiki page shell. {{ BODY }} is either an iframe embedding the generated
# self-contained wiki.html, or OS-specific "how to generate it" instructions.
# Uses a column flex so the iframe fills the space below the header.
_WIKI_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>M3 Wiki</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;500&family=Inter:wght@300;400;500;600&family=Outfit:wght@500;600;700&display=swap" rel="stylesheet">
    <style>
        {{ STYLE_CSS }}
        html, body { height: 100%; }
        body { display: flex; flex-direction: column; margin: 0; }
        .wiki-shell { flex: 1; display: flex; flex-direction: column; min-height: 0; }
        pre code { font-family: 'Fira Code', monospace; }
    </style>
</head>
<body>
    {{ HEADER }}
    <div class="wiki-shell">
        {{ BODY }}
    </div>
</body>
</html>
"""
