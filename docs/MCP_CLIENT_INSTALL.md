# M3 Memory — MCP Client Install Guide

> Per-client registration for m3-memory across all supported MCP-speaking
> environments. The MCP tool surface is identical everywhere; only the
> **how-to-register** differs by client.

For most users: run `m3 setup` once and the wizard auto-detects + wires
every supported client on your machine (Claude Code, Gemini CLI,
OpenCode, OpenClaw, Aider). This doc is for users who want to wire
clients manually or understand what the wizard does.

---

## Prerequisites (all clients)

1. **Install m3-memory** (`pipx install m3-memory` or `pip install m3-memory`).
2. **Install the sovereign CPU embedder service** so MCP cold-cascade
   always has a healthy fallback at `http://127.0.0.1:8082`:
   ```bash
   m3 embedder install
   ```
   On Windows this registers `m3-embed-server` as a Windows Service
   (auto-start). On Linux it installs a systemd unit. On macOS it
   installs a launchd plist. Verify with:
   ```bash
   m3 embedder status
   ```
   Expected: `running`.
3. **(Optional) Configure tier-1 in-process GGUF** for ~10-100× faster
   embeds on the hot path. Set in your shell or per-client env:
   ```bash
   export M3_EMBED_GGUF=/path/to/bge-m3-GGUF-Q4_K_M.gguf
   ```
   Without this, all embeds route through tier-2 (the :8082 service),
   which still works fine — just slower per call.

Once those two prerequisites are in place, every client below works
identically — the MCP protocol does the rest.

---

## Client 1 — Claude Code

**Native plugin** — easiest path.

```bash
# In Claude Code:
/plugin marketplace add skynetcmd/m3-memory
/plugin install m3@skynetcmd
```

> **No GitHub SSH key?** The `owner/repo` shorthand uses SSH. If you get "Premature close" or "ERR_STREAM_PREMATURE_CLOSE", use the HTTPS URL:
> ```
> /plugin marketplace add https://github.com/skynetcmd/m3-memory
> /plugin install m3@skynetcmd
> ```

The plugin's `mcpServers.m3.env` block reads `userConfig.embed_gguf`
and `userConfig.embed_fallback_url` set during install. Both knobs are
optional; the embed_fallback_url defaults to `http://127.0.0.1:8082`.

**Verify**: `tools_list_domains` from any Claude Code session lists 9
domains including `diagnostics`. Calling `memory_doctor` returns
`{"summary": "healthy"}` if the embedder service is up.

---

## Client 2 — Gemini CLI & Google Antigravity

### Gemini CLI

Auto-wired by `m3 setup` (writes to `~/.gemini/settings.json`).

Manual config — edit `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "m3": {
      "command": "m3",
      "env": {
        "M3_EMBED_FALLBACK_URL": "http://127.0.0.1:8082",
        "M3_EMBED_GGUF": ""
      }
    }
  }
}
```

Set `M3_EMBED_GGUF` if you have a BGE-M3 GGUF on disk for tier-1.

Restart Gemini CLI. Verify: tool list includes the m3 MCP entries.

### Google Antigravity (CLI & Desktop)

**Native plugin** — easiest path.

```bash
# In the Antigravity CLI:
agy plugin install https://github.com/skynetcmd/m3-memory
```

This registers the `m3` memory MCP server in `~/.gemini/antigravity-cli/settings.json`, wires the chatlog Stop + PreCompact hooks, and loads all 15 `/m3:*` slash commands as native agent Skills.

**Manual config** — edit `~/.gemini/antigravity-cli/settings.json`:

```json
{
  "mcpServers": {
    "m3": {
      "command": "m3",
      "env": {
        "M3_EMBED_FALLBACK_URL": "http://127.0.0.1:8082",
        "M3_EMBED_GGUF": ""
      }
    }
  }
}
```

Set `M3_EMBED_GGUF` if you have a BGE-M3 GGUF on disk for tier-1.

Restart the Antigravity CLI or Desktop app.

---

## Client 3 — OpenCode

Auto-wired by `m3 setup` (writes to OS-specific config path).

Manual config — edit:

- **Windows**: `%APPDATA%\opencode\opencode.json`
- **macOS / Linux**: `~/.config/opencode/opencode.json`

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "memory": {
      "type": "local",
      "command": ["m3"],
      "enabled": true
    }
  }
}
```

If you need to pass env vars (e.g. `M3_EMBED_GGUF`), set them in the
shell that launches OpenCode — OpenCode inherits the parent env.

Restart OpenCode. Verify via in-app tool browser.

---

## Client 4 — OpenClaw (via the MCP HTTP proxy)

**OpenClaw has no native MCP support.** It talks an OpenAI-compatible
chat shape; m3 ships an MCP→OpenAI proxy (`bin/mcp_proxy.py`) that
injects MCP tools into every chat request and executes `tool_calls`
by calling the bridge functions directly. Memory `a18d6a67`
documents the proxy architecture in detail.

### Start the proxy

From a clone of m3-memory:

```bash
# foreground
python3 ./bin/mcp_proxy.py

# or via the launcher script (handles env + logging)
bash ./bin/start_mcp_proxy.sh
```

Default bind: `localhost:9000`. The proxy is a long-running process —
keep it in a separate terminal, tmux pane, or supervise it via your OS
service manager (systemd / launchd / nssm).

### Point OpenClaw at the proxy

```bash
export OPENAI_BASE_URL=http://localhost:9000/v1
export OPENCLAW_GATEWAY_TOKEN=<optional auth token>
openclaw
```

The proxy exposes the full MCP tool catalog with the same domain
grouping as native clients. `tools_load_domain` works through the
proxy identically.

### Sandboxed OpenClaw via Docker

m3-memory ships a reference Docker setup at
`examples/sandbox-openclaw/` (Dockerfile + docker-compose.yml) that
runs OpenClaw + the proxy in an isolated container. Useful for
development or untrusted workloads. Copy `.env.example` to `.env`,
set `OPENCLAW_GATEWAY_TOKEN` + `OPENAI_API_KEY`, then
`docker compose up`.

### Auto-detection by `m3 setup`

The wizard flags OpenClaw as detected when any of these is true:
- `openclaw` on PATH (`shutil.which`)
- `~/.npm-global/bin/openclaw` exists
- `~/.openclaw/` workspace directory exists
- `OPENCLAW_GATEWAY_TOKEN` env var is set

When detected, the wizard defaults the proxy-install prompt to ON.

---

## Client 5 — Aider

Same proxy path as OpenClaw (Aider is also OpenAI-shape):

```bash
aider --openai-api-base http://localhost:9000/v1 \
      --model openai/claude-sonnet-4-6
```

Tool catalog parity with native MCP clients (CHANGELOG_2026 records
the early-version 15/44 gap; current builds expose the full 55+
catalog via the proxy).

---

## Client 6 — Claude Agent SDK (Python / TypeScript)

If you're building a custom agent on the Anthropic SDK, register
m3 like any other MCP server in your agent's session config.

**Python (claude-agent-sdk):**
```python
from claude_agent_sdk import Agent, MCPServerConfig

agent = Agent(
    mcp_servers=[
        MCPServerConfig(
            name="m3",
            command="mcp-memory",
            env={
                "M3_EMBED_FALLBACK_URL": "http://127.0.0.1:8082",
                "M3_EMBED_GGUF": "",  # optional tier-1 path
            },
        ),
    ],
)
```

**TypeScript** equivalent uses the same spec shape via the SDK's
`mcpServers` config option.

---

## Verifying any client

Once registered, every client should expose these meta-tools:

| Tool | Purpose |
|---|---|
| `tools_list_domains` | List all 9 domains + tool counts |
| `tools_load_domain` | Surface a domain's tools to the agent |
| `memory_doctor` (in `diagnostics` domain) | Run health probes — tier-1/tier-2/db/roundtrip with structured recommendations |
| `memory_search` (essential, always loaded) | Hybrid FTS5 + vector search |

A healthy install: `memory_doctor` returns `{"summary": "healthy"}` (or
`"degraded"` with explicit recommendations if tier-1 GGUF isn't set —
that's expected on minimal installs).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `memory_search` hangs > 10s | No embedder reachable | Run `m3 embedder install` then `m3 embedder status` |
| `memory_search` returns wrong vectors | Cascade fell to Ollama (cross-space) | Same — ensure :8082 is up; m3 cascade now prefers it (commit 0dfdf56+) |
| `tools_load_domain('diagnostics')` returns 0 tools | Pre-cascade-fix server version | `pip install -U m3-memory` then restart the MCP server |
| Plugin install dialog doesn't show new userConfig knobs | Cached plugin manifest | Re-pull the plugin: `/plugin remove m3@skynetcmd && /plugin install m3@skynetcmd` |

For deeper diagnostics, call `memory_doctor` and read the
`recommendations` list — it points at the specific fix for each
detected issue.

---

## Cross-references

- `docs/install_windows.md` / `install_macos.md` / `install_linux.md` —
  OS-level prerequisites and `m3 setup` walkthrough
- `docs/ENVIRONMENT_VARIABLES.md` — every M3_* env var the cascade
  understands
- `bin/memory/doctor.py` — the diagnostic impl
- Memory `a18d6a67` — OpenClaw & MCP Proxy Integration Architecture
