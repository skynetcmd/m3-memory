# M3 Memory — Quick Start

Get persistent memory running with your MCP agent in under five minutes.

---

## 1. Install M3 Memory

```bash
pip install m3-memory
```

Verify the command is available:

```bash
mcp-memory --help
```

If you get `command not found`, check that your Python scripts directory is on your PATH.

---

## 2. Start a local embedding server

M3 Memory needs a local model to generate embeddings for semantic search. [Ollama](https://ollama.com) is the easiest option:

```bash
# Download an embedding model
ollama pull nomic-embed-text

# Start the server (runs on localhost:11434)
ollama serve
```

Any OpenAI-compatible embedding endpoint works. [LM Studio](https://lmstudio.ai), vLLM, and LocalAI are also supported. If your server runs on a non-default port, set:

```bash
export LLM_ENDPOINTS_CSV="http://localhost:11434/v1"
```

---

## 3. Configure your agent

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

**Aider** — Aider does not natively speak MCP. Use the bundled [`mcp_proxy`](./bin/mcp_proxy.py) to expose m3-memory (plus the rest of the catalog — 55 tools total) to Aider over an OpenAI-compatible endpoint on `localhost:9000`, then point Aider at the proxy:
```bash
python bin/mcp_proxy.py               # or: bash bin/start_mcp_proxy.sh
aider --openai-api-base http://localhost:9000/v1
```
The proxy routes by model name — `--model openai/claude-sonnet-4-6` hits Anthropic, `gemini-*` hits Google AI, `grok-*` hits xAI, and anything else falls through to LM Studio on `localhost:1234`. Set `MCP_PROXY_ALLOW_DESTRUCTIVE=1` if you want `memory_delete`, `gdpr_*`, `*_export`, etc. exposed (off by default).

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

### Disable your host agent's built-in memory

Some agents ship with their own local memory system that will compete with m3-memory if left on. **m3-memory is the single source of truth for this project** — turn the built-in one off:

- **Claude Code** — has a built-in "auto memory" system that writes flat markdown files under `~/.claude/projects/<project>/memory/` with a `MEMORY.md` index. This project's [AGENT_INSTRUCTIONS.md](./AGENT_INSTRUCTIONS.md) already instructs Claude to ignore it, but if you previously had flat-file memories in that directory, migrate them into m3-memory via `memory_write` and then delete the flat files. Claude will load `CLAUDE.md` → `AGENT_INSTRUCTIONS.md` automatically from the project root, which applies the override.
- **Gemini CLI, Aider, OpenCode** — no built-in memory system to disable. They pick up `GEMINI.md` / `CONVENTIONS.md` / `AGENTS.md` respectively (all shims pointing at `AGENT_INSTRUCTIONS.md`).

---

## 4. Verify it works

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

## 5. Optional: cross-device sync

M3 Memory works standalone with local SQLite — no additional infrastructure needed. For multi-device sync, you can optionally connect:

- **PostgreSQL** — bi-directional delta sync across machines
- **ChromaDB** — federated vector search across your LAN

See [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md) for `PG_URL` and `CHROMA_BASE_URL` configuration.

---

## Next steps

- [CORE_FEATURES.md](./CORE_FEATURES.md) — what M3 Memory can do
- [AGENT_INSTRUCTIONS.md](./AGENT_INSTRUCTIONS.md) — all 44 MCP tools and agent behavioral rules
- [TECHNICAL_DETAILS.md](./TECHNICAL_DETAILS.md) — search internals, schema, sync, security
- [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md) — credentials and runtime config
