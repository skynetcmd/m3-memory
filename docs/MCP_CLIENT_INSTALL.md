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
/plugin install m3@skynetcmd
```

The plugin's `mcpServers.m3.env` block reads `userConfig.embed_gguf`
and `userConfig.embed_fallback_url` set during install. Both knobs are
optional; the embed_fallback_url defaults to `http://127.0.0.1:8082`.

**Verify**: `tools_list_domains` from any Claude Code session lists 9
domains including `diagnostics`. Calling `memory_doctor` returns
`{"summary": "healthy"}` if the embedder service is up.

---

## Client 2 — Gemini CLI

Edit `~/.gemini/settings.json` (or wherever Gemini stores its config):

```json
{
  "mcpServers": {
    "m3": {
      "command": "mcp-memory",
      "env": {
        "M3_EMBED_FALLBACK_URL": "http://127.0.0.1:8082",
        "M3_EMBED_GGUF": ""
      }
    }
  }
}
```

Set `M3_EMBED_GGUF` if you have a BGE-M3 GGUF on disk for tier-1.

Restart Gemini CLI. Verify: tool list shows `mcp__m3__memory_search`
and friends.

---

## Client 3 — OpenCode

OpenCode (the [sst/opencode](https://github.com/sst/opencode) terminal
agent) reads MCP servers from `~/.opencode/config.json` or
`./opencode.config.json` in the project root:

```json
{
  "mcp": {
    "servers": {
      "m3": {
        "command": "mcp-memory",
        "args": [],
        "env": {
          "M3_EMBED_FALLBACK_URL": "http://127.0.0.1:8082",
          "M3_EMBED_GGUF": ""
        }
      }
    }
  }
}
```

Restart OpenCode. Verify with the in-app tool browser.

---

## Client 4 — OpenClaw (via the MCP HTTP proxy)

**OpenClaw has no native MCP support** — it talks to an OpenAI-compatible
chat endpoint. m3 ships an MCP-to-OpenAI proxy that bridges it. See
memory entry `a18d6a67` (OpenClaw & MCP Proxy Integration
Architecture) for the protocol details.

Setup:
```bash
# 1. Install m3 with the proxy extra
pipx install 'm3-memory[proxy]'

# 2. Run the proxy (default port 9000)
m3 proxy start
# Or run as a service:
m3 proxy install   # Windows service / systemd unit
```

Point OpenClaw at the proxy as its chat endpoint:
- Endpoint URL: `http://localhost:9000`
- Auth: token from `OPENCLAW_GATEWAY_TOKEN` env var if set

The proxy exposes the full 55+ MCP tool catalog with the same domain
grouping as native clients. `tools_load_domain` works through the
proxy identically.

**Auto-detection by `m3 setup` wizard:** the wizard checks for any of
`openclaw` on PATH, `~/.npm-global/bin/openclaw`, the `~/.openclaw/`
workspace dir, or an `OPENCLAW_GATEWAY_TOKEN` env var. Any of those
flips the proxy default to ON during interactive setup.

---

## Client 5 — Aider

Same proxy path as OpenClaw — Aider talks OpenAI-shape; the m3 proxy
adapts:

```bash
aider --openai-api-base http://localhost:9000 \
      --openai-api-key proxy-local
```

Tool catalog parity with native MCP clients (fixed in CHANGELOG_2026
entry — was 15/44 in early versions, now full 55+).

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
