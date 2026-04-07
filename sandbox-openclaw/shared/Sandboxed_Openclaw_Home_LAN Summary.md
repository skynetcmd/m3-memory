# 🏠 Home LAN Summary

**Generated:** 2026-03-02 04:51 UTC
**Agent:** Jeeves (OpenClaw sandbox)

---

## Network
- **Subnet:** `private.subnet.x.x` — UniFi-managed with VLANs
  - `10.21.32.x` — Management / infrastructure
  - `10.21.40.x` — Servers / VMs
- **Router:** UniFi Gateway (UXG) at `10.x.x.x` (SSH: `***UXG_USER_REDACTED***@10.x.x.x`)
- **Controller:** UniFi Network at `10.x.x.x:11443` (site `hh1srtpv`)

## Compute

### M3 Max MacBook (workstation)
- LM Studio serving DeepSeek-R1 70B (MLX, 5-bit) on `localhost:1234`
- Embedding model: nomic-embed-text-v1.5 (768-dim) on the same endpoint
- OpenClaw gateway running Jeeves' sandbox (OrbStack/Docker)
- Local coding agents: Claude Code, Gemini, Aider — connected via MCP bridges

### Proxmox (`pve-database-host`) at `10.x.x.x`
- **VMID 501** → ChromaDB server at `10.x.x.x:8000`

## Shared Infrastructure

### ChromaDB (v2 API at `http://10.x.x.x:8000`)

| Collection | Dimension | Doc Count | Notes |
|---|---|---|---|
| `agent_memory` | 768 | 10 | Shared brain — ARCHITECTURE.md chunks, all 5 protocols |
| `user_facts` | 768 | 1 | User facts with category/source metadata |
| `home_memory` | 2 | 7 | "Home AI memory" — placeholder/test dimensions? |
| `entities` | null | 0 | Empty |

### SQLite (`memory/agent_memory.db`)
- Local on Mac, federated to ChromaDB via sync queue
- Tables: activity_logs, project_decisions, hardware_specs, system_focus, memory_items, memory_embeddings, memory_relationships, chroma_sync_queue

### MCP Bridges (5 active)

| Bridge | Purpose |
|---|---|
| `custom_pc_tool` | Core tools: logging, focus, decisions, thermal, local model, web search, Grok |
| `memory` | Full memory system: CRUD, conversations, ChromaDB sync, maintenance |
| `grok_intel` | Grok 3 — real-time X/Twitter data and fast reasoning |
| `web_research` | Perplexity sonar-pro — live web search (Grok fallback) |
| `mcp_proxy` | MCP tool execution proxy (localhost:9000) for non-native clients |

## Jeeves (OpenClaw Sandbox)

### Container
- **Image:** node:22-slim (OrbStack/Docker)
- **Memory:** 4 GB
- **Port:** localhost:8000 → container 18789

### Filesystem
- `/shared` (rw) — cross-agent drop zone for user, Jeeves, Claude Code, Gemini, Aider
- `/shared/ARCHITECTURE.md` (ro) — system spec
- `/home/clawuser/.openclaw` (rw) — OpenClaw workspace and memory

### Network Access
- ✅ ChromaDB at `10.x.x.x:8000`
- ✅ LM Studio embeddings at `host.internal:1234` (auth: `$LM_API_TOKEN`)
- ✅ Internet (web search, web fetch)

### Tools
curl, wget, ping, jq, ffmpeg, git-lfs, zip, unzip, sqlite3, dig/nslookup, pip3, file, tree, imagemagick

### Capabilities
- Web search and content fetching
- File operations (sandboxed)
- Shell commands (sandboxed)
- Semantic search (embed via LM Studio → query ChromaDB)
- Media processing (ffmpeg, imagemagick)
- Messaging (webchat, extensible to other channels)

### Limitations
- No direct MCP bridge calls (those are registered in Claude/Gemini on the Mac)
- No host filesystem access outside /shared
- No macOS Keychain access
- No LM Studio inference (only embeddings confirmed)

## Auth Model
- macOS Keychain for API keys (LM Studio, Perplexity, Grok/xAI)
- `$LM_API_TOKEN` in sandbox env for embedding calls
- Gateway auth via token

## Operational Protocols (from ARCHITECTURE.md)
1. **Reasoning Rule** — auto-archive think chains; manually log complex reasoning
2. **Hardware Rule** — check thermal load after heavy inference, log if not Nominal
3. **Decision Rule** — log every agreed decision immediately
4. **Search Rule** — query prior decisions before starting any new task
5. **Focus Protocol** — update focus summary every 3 turns, retire on completion
