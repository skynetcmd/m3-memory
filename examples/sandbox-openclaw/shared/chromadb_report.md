# ChromaDB Report

**Generated:** 2026-03-02 04:51 UTC
**Endpoint:** http://10.x.x.x:8000 (v2 API)
**Agent:** Jeeves (OpenClaw sandbox)

---

## 1. All Collections

| Collection | Dimension | Doc Count |
|---|---|---|
| `agent_memory` | 768 | 10 |
| `user_facts` | 768 | 1 |
| `home_memory` | 2 | 7 |
| `entities` | null | 0 |

---

## 2. agent_memory — All Documents

All 10 documents ingested 2026-03-02 ~04:39 UTC | `agent_id: system` | `model_id: claude-sonnet-4-6` | `origin_device: macbook` | `type: document`

| # | ID (short) | Title / Topic | Preview |
|---|---|---|---|
| 1 | `23ad92d0` | **Primary Engine Spec** | DeepSeek-R1 70B MLX, 5-bit, 64k–128k context, LM Studio localhost:1234, nomic-embed-text-v1.5 768-dim |
| 2 | `0b7ec613` | **MCP Bridges** | custom_pc_tool, memory, grok_intel, web_research, mcp_proxy — full routing table |
| 3 | `2b542864` | **Memory System** | SQLite agent_memory.db + ChromaDB federation, nomic 768-dim embeddings, numpy cosine similarity |
| 4 | `af45f47f` | **Protocol #1 — Reasoning Rule** | Auto-archive think chains from query_local_model; manual log for complex reasoning |
| 5 | `99920c67` | **Protocol #2 — Hardware Rule** | check_thermal_load() after heavy inference; log if not Nominal |
| 6 | `6e94c3c4` | **Protocol #3 — Decision Rule** | Log every agreed decision immediately via log_activity(category="decision") |
| 7 | `7cdb38f8` | **Protocol #4 — Search Rule** | query_decisions() before any new task — institutional memory lookup |
| 8 | `f9a83973` | **Protocol #5 — Focus Protocol** | update_focus() every 3 turns; retire_focus() on completion |
| 9 | `c19b14ed` | **Auth Model + Bridge Hardening** | macOS Keychain 4-step resolution; stderr logging, no token leaks, typed exceptions |
| 10 | `d7305517` | **OpenClaw Sandbox** | Container config: node:22-slim, 4GB RAM, /shared (rw), runtime tools |

---

## 3. Semantic Search: "OpenClaw sandbox" — Top 3 Results

Embedding via nomic-embed-text-v1.5 (768-dim) → ChromaDB vector query.

### Result #1 — Distance: 0.452 (strong match)
**ID:** `d7305517-744b-4bc7-a309-8ee3ccc821bf`
**Topic:** OpenClaw Sandbox

> OPENCLAW SANDBOX
>
> Container: openclaw-sandbox (node:22-slim, OrbStack/Docker)
> Config: sandbox-openclaw/docker-compose.yml
> Memory limit: 4 GB | Port: localhost:8000 -> container 18789
>
> Volumes:
>   /shared (rw) -> sandbox-openclaw/shared/ — drop zone for user and all agents
>   /shared/ARCHITECTURE.md (ro) -> bind mount of ARCHITECTURE.md
>   /home/clawuser/.openclaw (rw) -> sandbox-openclaw/.openclaw/
>
> Runtime tools: curl, wget, ping, jq, ffmpeg, git-lfs, zip, unzip, sqlite3, dig/nslookup, pip3, file, ...

**Metadata:** agent_id: system | model_id: claude-sonnet-4-6 | origin_device: macbook | created_at: 2026-03-02T04:39:19Z

### Result #2 — Distance: 1.051
**ID:** `2b542864-c0a4-41a2-92e7-1bf851bd2b27`
**Topic:** Memory System

> DB: memory/agent_memory.db (SQLite)
> Legacy tables: activity_logs, project_decisions, hardware_specs, system_focus
> Memory tables: memory_items, memory_embeddings, memory_relationships, chroma_sync_queue
>
> Embedding model: text-embedding-nomic-embed-text-v1.5 — 768 dims via LM Studio localhost:1234
> Vector search: numpy batch cosine similarity; pure-Python fallback if numpy absent
> Federation: ChromaDB at http://10.x.x.x:8000 (v2 API — v1 deprecated), collection agent_memory

**Metadata:** agent_id: system | model_id: claude-sonnet-4-6 | origin_device: macbook | created_at: 2026-03-02T04:39:19Z

### Result #3 — Distance: 1.053
**ID:** `7cdb38f8-0c80-44bf-a598-b4e8d2269384`
**Topic:** Protocol #4 — The Search Rule

> Trigger: Before starting ANY new task.
>
> Action: Call FIRST, before writing any code or plan:
>   custom_pc_tool -> query_decisions(keyword = <topic keywords>, limit = 10)
>
> Review results for prior decisions, conflicts, or relevant context before proceeding.
> This is the primary tool for institutional memory lookup.

**Metadata:** agent_id: system | model_id: claude-sonnet-4-6 | origin_device: macbook | created_at: 2026-03-02T04:39:19Z
