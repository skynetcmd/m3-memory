# M3 Memory — Quick Start

Get persistent memory running with your MCP agent in under a minute.

---

## Prerequisites

- Python 3.11+
- A local embedding model. [Ollama](https://ollama.com) is the easiest:

```bash
ollama pull nomic-embed-text && ollama serve
```

Any OpenAI-compatible embedding endpoint works ([LM Studio](https://lmstudio.ai), vLLM, etc.).

---

## Install

```bash
pip install m3-memory
```

---

## Configure your agent

Add the M3 Memory MCP server to your agent's config file:

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

**Aider** (`.aider.conf.yml` or MCP config):
```json
{
  "mcpServers": {
    "memory": { "command": "mcp-memory" }
  }
}
```

Restart your agent after adding the config.

---

## Verify it works

In your agent session, try:

```
Write a memory: "M3 Memory installed successfully"
```

Then in a new session:

```
Search for: "M3 install"
```

If it returns the memory you wrote, everything is working.

---

## Optional: cross-device sync

M3 Memory works standalone with local SQLite. For multi-device sync, you can optionally add:

- **PostgreSQL** — bi-directional delta sync across machines
- **ChromaDB** — federated vector search

See [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md) for `PG_URL` and `CHROMA_BASE_URL` configuration.

---

## Troubleshooting

### "Embedding failed" or "Connection refused"
Your embedding server isn't running. Start Ollama:
```bash
ollama serve
```
Or check that LM Studio is running on `localhost:1234`.

### "mcp-memory: command not found"
The package isn't installed or isn't on your PATH:
```bash
pip install m3-memory
which mcp-memory  # should return a path
```

### Memory server doesn't appear in agent
- Verify the JSON in your config file is valid
- Make sure the key is `"mcpServers"` (case-sensitive)
- Restart the agent completely (not just a new session)

### Agent can't find previous memories
- Memories are stored in `memory/agent_memory.db` relative to where `mcp-memory` runs
- Check that you're running from the same directory, or set `M3_MEMORY_ROOT`

---

## Next steps

- [CORE_FEATURES.md](./CORE_FEATURES.md) — what M3 Memory can do
- [AGENT_INSTRUCTIONS.md](./AGENT_INSTRUCTIONS.md) — all 25 MCP tools and agent behavioral rules
- [TECHNICAL_DETAILS.md](./TECHNICAL_DETAILS.md) — search internals, schema, sync, security
- [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md) — credentials and runtime config
