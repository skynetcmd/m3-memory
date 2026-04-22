# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory — Quick Start

Get persistent memory running with your MCP agent in under five minutes.

---

## 1️⃣ Install M3 Memory

```bash
pip install m3-memory
```

Verify the command is available:

```bash
mcp-memory --help
```

If you get `command not found`, check that your Python scripts directory is on your PATH.

---

## 2️⃣ Start a local embedding server

M3 Memory needs a local model to generate embeddings for semantic search. [Ollama](https://ollama.com) is the easiest option:

```bash
# Download the default embedding model
ollama pull qwen3-embedding:0.6b

# Start the server (runs on localhost:11434)
ollama serve
```

Any OpenAI-compatible embedding endpoint works. [LM Studio](https://lmstudio.ai), vLLM, LocalAI, and `llama.cpp --server` are also supported. If your server runs on a non-default port, set:

```bash
export LLM_ENDPOINTS_CSV="http://localhost:11434/v1"
```

### Optional: load a small chat model for enrichment

Some features — `auto_classify`, conversation/consolidation summaries, and future write-time enrichment — call your local LLM with short prompts. For these you want a **small, fast** model running alongside your embedder. M3 auto-selects via the same OpenAI-compatible endpoint; no extra config needed.

Pick whichever fits your hardware and runtime:

| Runtime | Example small model | Size |
|---|---|---|
| **Ollama** | `ollama pull qwen2.5:0.5b` or `ollama pull llama3.2:1b` | ~400 MB / ~1.3 GB |
| **LM Studio** | Qwen2.5-0.5B-Instruct (GGUF, Q8) | ~500 MB |
| **llama.cpp** | `llama-server -m qwen2.5-0.5b-instruct-q8_0.gguf` | ~500 MB |
| **vLLM / LocalAI** | Any HF-compatible 0.5B–1B instruct model | varies |

M3 picks the largest loaded chat model for all features. Embedding-only models (names matching `embed`, `nomic`, `jina`, `bge`, `e5`, `minilm`) are filtered out of chat selection automatically. If you want enrichment to stay fast, keep a small instruct model as your only loaded chat model — that way `get_best_llm` picks it for classification/summarization work. If you also load a large generation model, it will be preferred for every call (the per-feature "use the small model for enrichment, large model for generation" routing is on the roadmap, not yet in this release).

If you only want embeddings, skip this step — M3 runs fine without a chat model; the enrichment features simply become no-ops.

---

## 3️⃣ Configure your agent

Add the M3 Memory MCP server to your agent's config file.

**Claude Code** (`~/.claude/settings.json`):
```json
{
  "mcpServers": {
    "memory": { "command": "mcp-memory" }
  }
}
```

**Gemini CLI** (`~/.gemini/settings.json`):
```json
{
  "mcpServers": {
    "memory": { "command": "mcp-memory" }
  }
}
```

**OpenCode** (`opencode.json` or `opencode.jsonc` in project root or `~/.config/opencode/`):
```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "memory": {
      "type": "local",
      "command": ["mcp-memory"],
      "enabled": true
    }
  }
}
```

Restart your agent after saving the config.

---

### Agents without native MCP support

Some agents can't speak MCP directly — Aider and Openclaw are the two we support today. For them, route chat completions through the bundled [`mcp_proxy`](../bin/mcp_proxy.py), an OpenAI-compatible server on `localhost:9000` that injects m3-memory tools (and the rest of the catalog — 66 tools total) into every request and executes `tool_calls` by calling bridge functions directly.

**High-level flow:** agent → `localhost:9000/v1` (OpenAI-compatible) → proxy injects tools → real model provider (Anthropic / Google / xAI / LM Studio) → model may emit `tool_calls` → proxy executes them against m3-memory → results fed back → final answer returned to agent.

The proxy routes by model name: `claude-*` → Anthropic, `gemini-*` → Google AI, `grok-*` → xAI, `sonar-*` → Perplexity, anything else → LM Studio on `localhost:1234`. Set `MCP_PROXY_ALLOW_DESTRUCTIVE=1` if you want `memory_delete`, `gdpr_*`, `*_export`, etc. exposed (off by default).

#### Starting the proxy

**Foreground (development):**
```bash
python bin/mcp_proxy.py               # POSIX / Windows (use .venv\Scripts\python.exe on Windows)
# or:
bash bin/start_mcp_proxy.sh           # POSIX only — prints URL and tails stdout
```

**Background (daily use):**
```bash
bash bin/start_mcp_proxy.sh --background
# writes PID to   ${TMPDIR:-~/.cache}/mcp_proxy.pid
# writes logs to  ${TMPDIR:-~/.cache}/mcp_proxy.log
# stop:           kill $(cat ~/.cache/mcp_proxy.pid)
```

**Windows native (no WSL):** the `.sh` launcher is bash-only. Run the Python directly and redirect:
```powershell
Start-Process -WindowStyle Hidden `
  -FilePath "C:\Users\<you>\m3-memory\.venv\Scripts\python.exe" `
  -ArgumentList "bin\mcp_proxy.py" `
  -WorkingDirectory "C:\Users\<you>\m3-memory" `
  -RedirectStandardOutput "$env:TEMP\mcp_proxy.log" `
  -RedirectStandardError  "$env:TEMP\mcp_proxy.err"
```

**Boot-time autostart** — pick one depending on the platform:
- Linux: systemd user unit running `python bin/mcp_proxy.py` under `[Service] Restart=on-failure`
- macOS: launchd `~/Library/LaunchAgents/ai.m3-memory.mcp-proxy.plist` with `KeepAlive` + `RunAtLoad`
- Windows: Task Scheduler task `At log on` → `python.exe bin\mcp_proxy.py`, or NSSM if you want it as a real service

**Health check:**
```bash
curl -sf http://localhost:9000/v1/models && echo "proxy OK"
```

> **Important:** any agent configured to use the proxy (Aider / Openclaw) will **fail to reach a model** if the proxy is not running, because their `OPENAI_BASE_URL` points at `localhost:9000` instead of the real upstream. Start the proxy before launching those agents, or use one of the autostart options above.

#### Aider

Aider has no MCP client and no persistent memory of its own — only `CONVENTIONS.md`. To make it an m3-memory participant:

1. Start the proxy (see above).
2. Point Aider at the proxy via `~/.aider.conf.yml`:
   ```yaml
   openai-api-base: http://localhost:9000/v1
   # Optional — cheaper routing for local dev:
   # model: openai/claude-sonnet-4-6   # proxy will route to Anthropic
   ```
   Or per-invocation: `aider --openai-api-base http://localhost:9000/v1`
3. Export whatever API key belongs to the upstream provider you're routing to (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `GEMINI_API_KEY`). The proxy forwards the key to whichever provider matches the model prefix.
4. Keep `CONVENTIONS.md` at the repo root as a shim pointing at `AGENT_INSTRUCTIONS.md` so Aider gets the "always use memory_search first" rules in its system prompt.

Caveat: Aider will only invoke m3-memory tools if the model decides to call them. Your `CONVENTIONS.md` → `AGENT_INSTRUCTIONS.md` chain is what prompts it to do so. Without that prompt, the tools are available but unused.

#### Openclaw

Openclaw is self-hosted, doesn't speak MCP, and has its own internal `session-memory` hook backed by a SQLite retrieval index at `~/.openclaw/memory/main.sqlite`. To make it use m3-memory *instead of / alongside* that internal store:

1. Start the proxy (see above).
2. Edit `~/.openclaw/openclaw.json` and add `OPENAI_BASE_URL` to the `env` block (leave other keys — `OPENAI_API_KEY`, Telegram tokens, gateway tokens — untouched):
   ```json
   "env": {
     "OPENAI_API_KEY": "sk-...existing...",
     "OPENAI_BASE_URL": "http://localhost:9000/v1"
   }
   ```
   Back up the file first: `cp ~/.openclaw/openclaw.json ~/.openclaw/openclaw.json.bak`
3. Seed the system prompt. Openclaw's `hooks.internal.entries.boot-md` is enabled by default — drop a markdown file that tells the model: *"Before answering, call `memory_search` to check m3-memory. After learning anything important, call `memory_write`."* Without that instruction the injected tools are available but the model won't know to use them.
4. (Optional) Keep Openclaw's own `session-memory` hook enabled — it runs in parallel and doesn't conflict. Or disable it in `hooks.internal.entries.session-memory.enabled = false` if you want m3-memory to be the sole store.
5. Restart Openclaw. It will now route through the proxy, get all 55 catalog tools injected into every request, and execute them via the bridge layer directly against m3-memory's SQLite.

Caveat: when the proxy is down, Openclaw's chat completions **will fail** because `OPENAI_BASE_URL` points at a dead local port. Either autostart the proxy (see above) or revert the `env` change when you want to run Openclaw standalone.

#### Migrating existing flat-file memory

If you already have memories in any of these stores — Claude Code flat files, `GEMINI.md` added-memories section, or Openclaw's SQLite — use [`bin/migrate_flat_memory.py`](../bin/migrate_flat_memory.py) to import them. It's idempotent (safe to re-run), verifies every write via SHA-256 round-trip, and prints a manual cleanup list after verification but never deletes source files itself.

```bash
python bin/migrate_flat_memory.py --dry-run                    # preview
python bin/migrate_flat_memory.py                              # migrate + verify
python bin/migrate_flat_memory.py --sources claude,gemini      # subset
python bin/migrate_flat_memory.py --include-rules              # also import CLAUDE.md etc.
```

### Disable your host agent's built-in memory

Some agents ship with their own local memory system that will compete with m3-memory if left on. **m3-memory is the single source of truth for this project** — turn the built-in one off:

- **Claude Code** — has a built-in "auto memory" system that writes flat markdown files under `~/.claude/projects/<project>/memory/` with a `MEMORY.md` index. This project's [AGENT_INSTRUCTIONS.md](./AGENT_INSTRUCTIONS.md) already instructs Claude to ignore it, but if you previously had flat-file memories in that directory, migrate them into m3-memory via `memory_write` and then delete the flat files. Claude will load `CLAUDE.md` → `AGENT_INSTRUCTIONS.md` automatically from the project root, which applies the override.
- **Gemini CLI, Aider, OpenCode** — no built-in memory system to disable. They pick up `GEMINI.md` / `CONVENTIONS.md` / `AGENTS.md` respectively (all shims pointing at `AGENT_INSTRUCTIONS.md`).

---

## 4️⃣ Verify it works

In your agent session, write a test memory:

```
Write a memory: "M3 Memory installed successfully"
```

Your agent should call `memory_write` and return a UUID. That confirms the MCP bridge is connected, SQLite is working, and embeddings are being generated.

Now open a **new session** and search for it:

```
Search for: "M3 install"
```

If the memory you wrote comes back, everything is working: persistence, embedding, and hybrid search.

### What success looks like

- `memory_write` returns a UUID (e.g., `Created: a1b2c3d4-...`)
- `memory_search` returns the memory you wrote, with a relevance score
- No errors about embedding failures or connection refused

### What failure looks like

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Embedding failed" or "Connection refused" | Embedding server not running | Run `ollama serve` or start LM Studio |
| "mcp-memory: command not found" | Package not on PATH | `pip install m3-memory` and check `which mcp-memory` |
| Memory tools don't appear in agent | Config not loaded | Check JSON syntax, ensure `"mcpServers"` key, restart agent fully |
| Search returns nothing in new session | Different working directory | Run from same directory, or set `M3_MEMORY_ROOT` env var |

---

## 5️⃣ Optional: cross-device sync

M3 Memory works standalone with local SQLite — no additional infrastructure needed. For multi-device sync, you can optionally connect:

- **PostgreSQL** — bi-directional delta sync across machines
- **ChromaDB** — federated vector search across your LAN

See [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md) for `PG_URL` and `CHROMA_BASE_URL` configuration.

---

## ▶️ Next steps

- [CORE_FEATURES.md](./CORE_FEATURES.md) — what M3 Memory can do
- [AGENT_INSTRUCTIONS.md](./AGENT_INSTRUCTIONS.md) — all 66 MCP tools and agent behavioral rules
- [TECHNICAL_DETAILS.md](./TECHNICAL_DETAILS.md) — search internals, schema, sync, security
- [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md) — credentials and runtime config
