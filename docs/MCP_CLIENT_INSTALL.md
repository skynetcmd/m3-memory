# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> m3 Memory — MCP Client Install Guide

> Per-client registration for m3-memory across all supported MCP-speaking
> environments. The MCP tool surface is identical everywhere; only the
> **how-to-register** differs by client.

For most users: run `m3 setup` once and the wizard auto-detects + wires
every supported client on your machine (Claude Code, Cursor, Cline,
Gemini CLI, OpenCode, Antigravity, OpenClaw, Aider). This doc is for users
who want to wire clients manually or understand what the wizard does.

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
3. **(Optional) Configure tier-1 in-process GGUF** for ~10-85× faster
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

## Client 4 — OpenClaw

**OpenClaw speaks MCP natively since `2026.3.22`.** `m3 setup` registers a
roots-pinned stdio server directly, the same way Claude Code and Gemini are
wired — no proxy, no `OPENAI_BASE_URL` override, no second long-running process.

> Older than `2026.3.22`? Those builds have no `openclaw mcp` subcommand at all,
> so a server entry would never be read. `m3 setup` detects this and refuses with
> an upgrade instruction rather than writing config the client ignores. Upgrade
> with `npm install -g openclaw@latest`.

### Register m3

```bash
m3 setup --agents openclaw        # or just `m3 setup` and answer the prompt
```

Verify what landed:

```bash
openclaw mcp show m3_memory
```

You should see `transport: "stdio"`, a `command`/`args` pair pointing at your
interpreter and `bin/memory_bridge.py`, and an `env` block carrying
`M3_ENGINE_ROOT` / `M3_CONFIG_ROOT` / `M3_MEMORY_ROOT`. Those root pins matter:
the server and the chatlog hook must agree on the same databases (see the
split-brain hazard in `CLAUDE.md`). Restart the OpenClaw CLI or gateway to pick
the server up.

To register by hand — note the `env` block is the part people drop, and dropping
it is what causes the split-brain:

```bash
openclaw mcp set m3_memory '{
  "command": "/path/to/python",
  "args": ["/path/to/m3-memory/bin/memory_bridge.py"],
  "env": {"M3_ENGINE_ROOT": "...", "M3_CONFIG_ROOT": "..."},
  "transport": "stdio",
  "enabled": true
}'
```

`openclaw mcp set` **replaces** an existing entry of the same name outright, so
re-running is safe — but a partial spec silently drops the fields you omit.

### The startup tool surface

m3 registers a `toolFilter` of the same 10 tools it exposes to every other
client: `memory_search`, `memory_write`, `memory_get`, `memory_supersede`,
`chatlog_search`, `chatlog_status`, `files_search`, `m3_call`,
`tools_list_domains`, `tools_load_domain`.

That is 3,929 tokens on the wire instead of 29,658 for the full catalog
— an 86.8% reduction, measured with `python bin/measure_tool_tokens.py`. Nothing
is lost: `m3_call` invokes any catalog tool by name, and `tools_load_domain`
pulls a whole domain in live.

### Sandboxed OpenClaw via Docker

`examples/sandbox-openclaw/` runs OpenClaw in a container against m3 on the
**host**, over `streamable-http` rather than stdio — the image ships OpenClaw
alone, so a stdio `command` would have nothing to launch.

On the host:

```bash
m3 serve --generate-token     # once; prints the token
m3 serve --host 0.0.0.0 --port 8080 --public-host host.docker.internal
```

Both flags are required. `--host 0.0.0.0` because the `127.0.0.1` default is
unreachable from the container; `--public-host` because the transport allowlists
only the bind host plus loopback, so a request arriving with
`Host: host.docker.internal:8080` is rejected with **421 before auth runs**.
Bearer auth is mandatory and is not waived for loopback.

Then copy `.env.example` to `.env`, set `M3_SERVE_TOKEN` and
`OPENCLAW_GATEWAY_TOKEN`, and `docker compose up`.

### Auto-detection by `m3 setup`

The wizard flags OpenClaw as detected when any of these is true:
- `openclaw` on PATH (`shutil.which`)
- `~/.npm-global/bin/openclaw` exists
- `~/.openclaw/` workspace directory exists
- `OPENCLAW_GATEWAY_TOKEN` env var is set

The wiring prompt is offered only when OpenClaw is detected.

---

## Client 5 — Aider

Aider has no native MCP support — it talks an OpenAI-compatible chat shape, so it
reaches m3 through the MCP→OpenAI proxy (`bin/mcp_proxy.py`), which injects MCP
tools into each chat request and executes the returned `tool_calls` against the
bridge. Start it with `python3 ./bin/mcp_proxy.py` (or
`bash ./bin/start_mcp_proxy.sh`, which handles env + logging); it binds
`localhost:9000` and is a long-running process, so keep it in its own terminal or
supervise it via systemd / launchd / nssm.

```bash
aider --openai-api-base http://localhost:9000/v1 \
      --model openai/claude-sonnet-4-6
```

Tool catalog parity with native MCP clients (CHANGELOG_2026 records
the early-version 15/44 gap; current builds expose the full
catalog via the proxy — see [MCP_TOOLS.md](MCP_TOOLS.md) for the live count).

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

## Client 7 — Cursor & Cline (VS Code-family)

Both are auto-detected and auto-wired by `m3 setup` — it writes the `memory`
MCP entry (with the correct interpreter, bridge path, and root env) to each
client's own config. Re-run `m3 setup` after installing either, and
`m3 doctor --fix` repoints the entry if paths later move.

### Cursor

Auto-wired to `~/.cursor/mcp.json` (only when `~/.cursor` exists). Manual config:

```json
{
  "mcpServers": {
    "memory": {
      "command": "m3",
      "env": {
        "M3_EMBED_FALLBACK_URL": "http://127.0.0.1:8082"
      }
    }
  }
}
```

> **Cursor's ~40-tool cap.** Cursor limits the active tool surface across all
> MCP servers. m3 exposes 100+ tools but lazy-loads only the 20 essentials at
> startup (the rest via `tools_load_domain`), so it stays well under the ceiling.

### Cline (VS Code extension)

Auto-wired (only when the Cline extension's storage dir exists) to
`cline_mcp_settings.json`:

- **Windows**: `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json`
- **macOS**: `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`
- **Linux**: `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`

Manual config merges into the `mcpServers` object (see the root
[`llms-install.md`](../llms-install.md) for the full agent-followed guide, and
Cline's [MCP marketplace](https://github.com/cline/mcp-marketplace) for one-click
install):

```json
{
  "mcpServers": {
    "memory": {
      "command": "m3",
      "env": { "M3_EMBED_FALLBACK_URL": "http://127.0.0.1:8082" },
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

Restart Cursor / reload Cline's MCP servers. Verify via the client's MCP tool list.

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
- Memory `a18d6a67` — MCP Proxy Integration Architecture (the Aider path; its
  OpenClaw sections are superseded — OpenClaw is a native MCP client since
  `2026.3.22`)
