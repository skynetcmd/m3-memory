# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Web Dashboard

A built-in, local web control panel for M3 — browse memory, explore the
interactive knowledge graph, audit conflicts, and watch system health and load.
It is **backend-agnostic** (works on SQLite and PostgreSQL, and any future
storage backend) and runs as a **windowless background service** on
`http://127.0.0.1:8088`.

> **Loopback-only, no authentication.** The dashboard binds to `127.0.0.1` and
> is intended for local, single-user use. It can read *and mutate* memory
> (edit, soft/hard-delete, GDPR export/forget), so **do not expose it to a
> network** without putting your own authentication in front of it.

---

## Install

The dashboard's dependencies (FastAPI + uvicorn) ship as an optional extra so a
default install stays lightweight:

```bash
pip install "m3-memory[dashboard]"
```

The interactive setup wizard (`m3 setup`) also offers to install the dashboard
(default **yes**) and to register it to auto-start on boot.

---

## Run

```bash
m3 dashboard              # start it (detached, windowless) and print the URL
m3 dashboard --status     # is it running? show the URL + pid
m3 dashboard --stop       # stop it and free the port
```

`m3 dashboard` launches the server **detached and windowless** — there is no
console window and no flashing, and it **keeps running after you close the
terminal**. The command prints the URL and returns immediately. Open
**<http://127.0.0.1:8088>** in your browser.

Flags:

| Flag | Meaning |
|------|---------|
| `--host HOST` | Bind address (default `127.0.0.1`; leave it loopback unless you add auth). |
| `--port PORT` | TCP port (default `8088`, or `$M3_DASHBOARD_PORT`). |
| `--foreground` | Run the server in the current process (blocks; for debugging). |
| `--stop` | Stop the running dashboard. |
| `--status` | Report whether it's running and its URL. |

Environment variables `M3_DASHBOARD_HOST` and `M3_DASHBOARD_PORT` override the
defaults.

### Auto-start on boot

When you opt into the dashboard during `m3 setup`, it is registered as a
boot-start service (a scheduled task on Windows via `pythonw.exe`, so there is
no console window or flash; a user service on macOS/Linux). To register it
manually:

```bash
python bin/install_schedules.py --add dashboard
```

The service is single-instance (a re-launch when it's already running is a
no-op) and self-heals if it dies.

---

## Pages

The dashboard has four tabs:

### Graph Explorer
The **interactive knowledge graph** — the extracted *entities* (files,
functions, models, hosts, …) and how they link. Pan, zoom, drag nodes, filter
by type, and open any node's detail. Use **⇱ Open in window** to pop the graph
out into its own resizable browser window (with a node-name filter and a detail
sidebar).

> The graph shows the **knowledge-graph layer — entities and links, not raw
> memory text.** Filtering it for a memory keyword that isn't an entity name
> returns nothing; to search memory text, use the Memory Browser.

### KB Browser
A curated, card-based view of memories and entities for browsing and curation.

### Conflict & Audit Log
The memory history timeline: overrides, resolutions, contradictions, and
soft/hard-delete — plus GDPR export/forget. All actions go through M3's
backend-agnostic core, so they behave identically on any storage backend.

### System Health
A digestible status panel:

- **Overall status** — `HEALTHY`, `THROTTLED (RAM/GPU/…)`, `REDUCED
  PERFORMANCE`, or `NEEDS SETUP`, with the specific reasons when it isn't
  healthy. (A load-throttle or slower embedder tier is **not** a data-integrity
  problem — the wording reflects that.)
- **System load (Governor)** — CPU / RAM / GPU meters, each colored green /
  amber / red by its own value against the throttle and halt thresholds, plus
  the current pacing mode and thermal state.
- **Database backend** — SQLite / PostgreSQL / … with per-store paths (core,
  chat, files), row counts, and last-updated time.
- **Data warehouse (CDW) sync** — per-direction last-sync watermarks (if a
  warehouse is configured).
- **Processing pipeline** — enrichment, reflection, and entity-extraction queue
  depths and drain status.

All timestamps are shown in local time with the UTC (Zulu) value in parentheses.
The page auto-refreshes every few seconds so live load telemetry stays current.

---

## Search syntax (Memory Browser & graph filter)

Both the Memory Browser search box and the knowledge-graph node filter use the
same query grammar. Words are **filters** (all must match — AND):

| Query | Matches |
|-------|---------|
| `UAC window` | items with **both** `UAC` **and** `window` (same as `+UAC +window`) |
| `-window` | items that do **not** contain `window` |
| `"malformed string"` | the exact **phrase** (not the two words separately) |
| `UAC -"malformed string"` | has `UAC` but **not** that phrase |

Search is **case-insensitive by default**; uncheck **ignore case** for exact
case. An ⓘ info box in the UI documents this inline.

---

## Backends

The dashboard reads and writes through M3's storage-backend seam, so it works
unchanged on **SQLite** (the default) and **PostgreSQL** (`M3_DB_BACKEND=postgres`).
A future SQL backend (e.g. MariaDB) is picked up automatically — the dashboard
never hardcodes an engine.

---

## Troubleshooting

- **`m3 dashboard` says it needs web dependencies** — run
  `pip install "m3-memory[dashboard]"`.
- **Port already in use** — another instance is running; `m3 dashboard --status`
  shows it, `m3 dashboard --stop` clears it (this also cleans up an orphaned
  instance the process registry missed).
- **`m3 doctor`** reports the dashboard's health, and `m3 doctor --fix` will
  restart a dead/wedged instance on its recorded host/port.
