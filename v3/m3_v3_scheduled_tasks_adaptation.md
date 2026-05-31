# 🚀 M3-v3: Scheduled Tasks Adaptation Plan

This document outlines the strategy to migrate rigid scheduled cron tasks and daemon processes (PostgreSQL Sync, Chat Log Embedding Sweeper, Filesystem Watcher, and Reflector/Observer daemons) under the **M3 Adaptive Background Workload Governor**. 

By transitioning these tasks from static crons to **Cooperative Idle-Monitored Processes**, we prevent database lockouts, network congestion, and GPU memory saturation during active user sessions.

---

## 📊 Summary of Tasks to Migrate

| Target Job / Script | Legacy Schedule | Bottleneck it Causes | Hardened Governor Adaptation |
| :--- | :--- | :--- | :--- |
| **1. PostgreSQL Sync** (`pg_sync.py`) | Hourly Cron (`pg_sync.sh`) | WAL locks on SQLite during bulk uploads; CPU spikes; network saturation. | Runs only in **Idle Mode** (user idle >60s). Syncs in atomic 100-row chunks; halts cleanly at unit boundaries. |
| **2. Chat Log Sweeper** (`chatlog_embed_sweeper.py`) | Periodic Cron | GPU context thrashing and Ollama/LM Studio lockouts during active queries. | Runs only in **Idle Mode** + GPU load is **Nominal**. Embeds in atomic **5-row batches**; pauses instantly on user query. |
| **3. Filesystem Watcher** (`files_watch_once.py`) | Scheduled Run | Heavy disk I/O and CPU spikes while recalculating file hashes. | Integrated into the idle loop. Scans directories incrementally (10 file nodes per unit); checkpoints progress in-DB. |
| **4. Cognitive Reflector** (`run_reflector.py`) | Persistent Daemon | Local LLM saturation from long summary and reflection completions. | Registered as a low-priority task. Worker halts prompt completion if active query context space is requested. |

---

## 🛠️ Detailed Adaptation Designs

### 1. PostgreSQL Synchronization (`pg_sync.py`)
Currently, `pg_sync.sh` triggers `pg_sync.py` blindly every hour. If synchronization occurs during an active development session where the agent is executing parallel searches, the SQLite database WAL lock will cause query latency to balloon.

*   **Adaptation:**
    *   Remove the hourly cron job from `bin/install_schedules.py`.
    *   Register `pg_sync.py` as a Governor-tracked background task.
    *   **Unit Sizing:** Process synchronization in atomic chunks of **100 rows** per database transaction.
    *   **Interrupt Logic:** Before beginning the next 100-row chunk, check `get_cooldown_state()`. If the state transitions to `HALTED` or `TAPERED`, save the current watermarks, commit the transaction, and suspend.

### 2. Chat Log Embedding Sweeper (`chatlog_embed_sweeper.py`)
Embedding generation requires significant local GPU computation. If the embedding sweeper runs blindly in the background while the user is executing an interactive query, the local LLM server (Ollama/LM Studio) will experience severe queue congestion, stalling the user's vector query response.

*   **Adaptation:**
    *   Change from a rigid time-based schedule to an **Idle-Only Trigger**.
    *   **Unit Sizing:** Process embedding backfills in batches of exactly **5 rows** per unit.
    *   **Interrupt Logic:** Instantly pause embedding if `get_cooldown_state() != "CONTINUOUS"` or if the telemetry reports high GPU thermal loads. The worst-case latency to yield GPU capacity back to the user is reduced to **<300ms** (the time to complete 5 embeddings).

### 3. Filesystem Watcher (`files_watch_once.py`)
Checking folder trees for changes during file-staleness checks requires walking directories and computing SHA-256 hashes, creating heavy disk I/O overhead.

*   **Adaptation:**
    *   Convert `files_watch_once` into an incremental directory-walking daemon.
    *   **Unit Sizing:** Scan and hash a maximum of **10 file nodes** per work unit.
    *   **Interrupt Logic:** Record the directory walk cursor in the local `files.db` state table. If user interaction occurs, halt immediately. On the next idle window, the watcher resumes precisely where it left off, avoiding redundant scans.

---

## 📋 5. Master Plan Integration Roadmap

We officially fold these scheduled task migrations into our **Project M3-v3 Master Implementation Plan**:

### Milestone 1: Path Decoupling, SDK Realignment & Hardened Startup
*   [ ] Refactor `bin/install_schedules.py` to remove legacy static cron scripts (`pg_sync.sh` and time-based sweeper tasks).
*   [ ] Register `pg_sync.py` and `files_watch_once.py` as adaptive Governor-controlled background worker loops.

### Milestone 4: Rust Crate Expansion & SDK Oxidation
*   [ ] Move the directory scanning and SHA-256 comparison steps of the filesystem watcher into `m3-ingest-rs` (Rust), allowing incremental walks to run in under **0.5ms** per unit.
