# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory — Quick Start

Get persistent memory running with your MCP agent in under five minutes. Under the hood you're getting a benchmark-leading hybrid retriever (FTS5 + BGE-M3 vector + MMR) — **99.2% retrieval @ k=10 on LongMemEval-S**, SOTA-class recall running entirely on your CPU.

This is the generic quick start. For OS-specific walkthroughs see
[QUICKSTART_LINUX.md](./QUICKSTART_LINUX.md),
[QUICKSTART_MACOS.md](./QUICKSTART_MACOS.md), and
[QUICKSTART_WINDOWS.md](./QUICKSTART_WINDOWS.md).

---

## 1️⃣ Install M3 Memory

### Easiest path — one-line installer (Linux + macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
```

Detects your distro, installs prerequisites (`pipx`, `git`, `sqlite3`)
via your package manager, runs `pipx install m3-memory`, then drives the
**one-command wizard** (`m3 setup`) which installs the system payload,
the sovereign CPU embedder (bundled — no LM Studio / Ollama / GPU
required), per-agent MCP wiring, chatlog hooks, and runs a final brief
`m3 doctor` health check (add `--verbose` for full detail). Refuses to run
as root; sudo only for OS package install.

### Claude Code users — install as a plugin

```
/plugin marketplace add skynetcmd/m3-memory
/plugin install m3@skynetcmd
```

> **No GitHub SSH key?** The `owner/repo` shorthand above uses SSH. If you
> get a "Premature close" or "ERR_STREAM_PREMATURE_CLOSE" error, use the
> HTTPS URL instead:
> ```
> /plugin marketplace add https://github.com/skynetcmd/m3-memory
> /plugin install m3@skynetcmd
> ```

15 `/m3:*` slash commands (`/m3:health`, `/m3:search`, `/m3:save`, `/m3:status`, …), the `m3:curate-memory` and `m3:curate-chatlog` subagents, and auto-wired Stop + PreCompact chatlog hooks. See [claude_code_plugin.md](./claude_code_plugin.md) for the full reference.

### Google Antigravity users — install as a plugin

```bash
agy plugin install https://github.com/skynetcmd/m3-memory
```

15 `/m3:*` slash commands as native Skills, two curator subagents (`m3:curate-memory`, `m3:curate-chatlog`), and auto-wired chatlog hooks. See [antigravity_plugin.md](./antigravity_plugin.md) for the full reference.

### Windows

```powershell
# Prerequisites (elevated PowerShell once):
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
winget install -e --id SQLite.SQLite

# Install m3 as your normal user (pipx recommended — auto-manages PATH):
pip install --user pipx
pipx ensurepath
# Open a new terminal, then:
pipx install m3-memory
m3 setup
```

> **`m3` not found after install?** pip puts `m3.exe` in
> `%APPDATA%\Python\Python312\Scripts\` — add that to your user PATH, or
> use pipx which handles PATH automatically. Full details:
> [install_windows.md § Common gotchas](./install_windows.md#common-gotchas).

### Manual / older Linux / pip route

```bash
pipx install m3-memory   # recommended — isolates m3 and manages PATH automatically
m3 setup
```

> **`pipx` not installed?** Install it via your package manager:
> ```bash
> sudo apt install pipx          # Debian/Ubuntu/Mint
> sudo dnf install pipx          # Fedora/RHEL
> sudo pacman -S python-pipx     # Arch
> brew install pipx              # macOS
> ```
> Or with pip (may hit PEP 668 on managed Python — see below):
> ```bash
> pip install --user pipx && pipx ensurepath
> ```
>
> **PEP 668 error (`externally-managed-environment`)?** Your system Python is
> OS-managed. Install pipx via the package manager above, or use a virtualenv:
> ```bash
> python3 -m venv ~/.venv/m3 && source ~/.venv/m3/bin/activate
> pip install m3-memory && m3 setup
> ```

`m3 setup` is the recommended path — interactive wizard, sensible
defaults. Power users can still run individual steps with `m3 install-m3`,
`m3 embedder install`, `m3 chatlog init`, etc. — see `m3 --help`.
Upgrade path: `pip install -U m3-memory && m3 update`.

You can also reach **any** memory tool from the shell — every catalog tool is
exposed as `m3 <domain> <tool>` (e.g. `m3 files files_stats`,
`m3 memory memory_search --query "..."`). Chatlog tools live under
`m3 chat <tool>` (e.g. `m3 chat chatlog_search`), since plain `m3 chatlog` is
the operational subsystem command. Add `--dry-run` to validate without
executing, and `--yes` to confirm a destructive tool. Run `m3 <domain> --help`
to list a domain's tools.

> **Tool catalog stays small in your context.** m3 ships 100+ MCP tools but
> groups them into 9 domains (memory, chatlog, files, entity, agent, tasks,
> conversations, diagnostics, admin). Only the ~18 essentials load at MCP startup
> (~3,540 tokens, ~1.8% of a 200K window; the full catalog loads on demand). The
> agent pulls in a domain on demand — just say "load the files tools" and it does.
> Set `M3_TOOLS_LAZY=0` to disable.

### claude.ai (web/desktop) integration

```bash
m3 serve --host 127.0.0.1 --port 8080
```

Then expose `127.0.0.1:8080/mcp` via Cloudflare Tunnel / Tailscale Funnel /
ngrok / a reverse proxy and paste the URL into claude.ai's connector
settings. Full self-host walkthrough at
[claude_ai_connector.md](./claude_ai_connector.md).

### Already have the repo cloned?

If you cloned `m3-memory` and did `pip install -e .`, everything works
without further action — the CLI auto-detects a sibling
`bin/memory_bridge.py` via its package path and uses that.

---

## 2️⃣ The embedder is already set up

`m3 setup` installed the sovereign CPU embedder (BGE-M3, running
**in-process** via the m3-core-rs `oxidation` extra — llama.cpp linked
directly, zero IPC, so there's no separate service to run or monitor;
a local HTTP embed server exists only as an automatic fallback). No
LM Studio, no Ollama, no GPU, no internet required for embedding to work.

For ~10–50× faster embeddings, the wizard offers an opt-in GPU build
(CUDA / Vulkan / Metal autodetected). You can add it later with
`m3 embedder install-gpu`.

### Optional: load a small chat model for enrichment

Some features — `auto_classify`, conversation summaries, write-time
enrichment — call your local LLM with short prompts. M3 picks up any
OpenAI-compatible endpoint via `LLM_ENDPOINTS_CSV`; no extra config
needed once a server is running.

Pick whichever fits your hardware and runtime:

| Runtime | Example small model | Size |
|---|---|---|
| **Ollama** | `ollama pull qwen2.5:0.5b` or `ollama pull llama3.2:1b` | ~400 MB / ~1.3 GB |
| **LM Studio** | Qwen2.5-0.5B-Instruct (GGUF, Q8) | ~500 MB |
| **llama.cpp** | `llama-server -m qwen2.5-0.5b-instruct-q8_0.gguf` | ~500 MB |
| **vLLM / LocalAI** | Any HF-compatible 0.5B–1B instruct model | varies |

> **Pointing M3 at your runtime:** endpoint discovery probes LM Studio (`:1234`) by
> default only. Adjust for your setup:
> - **Ollama** — `export M3_ENABLE_OLLAMA_FAILOVER=1` (add `M3_ENABLE_LMSTUDIO_FAILOVER=0`
>   if you don't also run LM Studio).
> - **llama.cpp / vLLM / LocalAI / remote** — `export M3_LLM_URL="http://localhost:8080/v1"`
>   (your server is tried first; the LM Studio probe is auto-disabled).
> - **Multiple endpoints, specific order** — `export LLM_ENDPOINTS_CSV="url1,url2,…"`.
>
> See [ENVIRONMENT_VARIABLES → Endpoint discovery & failover](ENVIRONMENT_VARIABLES.md#endpoint-discovery--failover).

M3 picks the largest loaded chat model for enrichment. If you only want
embedding-based memory, skip this — those features simply become no-ops.

---

## 3️⃣ Configure your agent

`m3 setup` wires every agent it detects on PATH. If you skipped the
wizard or add an agent later, here's the manual recipe per agent.

**Claude Code** — the [plugin route](./claude_code_plugin.md) is recommended. Manual:

```bash
claude mcp add --scope user memory m3
```

Or edit `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "memory": { "command": "m3" }
  }
}
```

**Gemini CLI** (`~/.gemini/settings.json`):
```json
{
  "mcpServers": {
    "memory": { "command": "m3" }
  }
}
```

**Google Antigravity** — the [plugin route](./antigravity_plugin.md) is recommended. Manual:

Edit `~/.gemini/antigravity-cli/settings.json`:
```json
{
  "mcpServers": {
    "memory": { "command": "m3" }
  }
}
```

**OpenCode** (`opencode.json` in project root or `~/.config/opencode/`):
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

Restart your agent after saving the config.

---

### Agents without native MCP support

Aider and OpenClaw can't speak MCP directly. Route them through the
bundled [`mcp_proxy`](../bin/mcp_proxy.py) — an OpenAI-compatible server
on `localhost:9000` that injects m3-memory tools into every request and
executes `tool_calls` by calling bridge functions directly.

**High-level flow:** agent → `localhost:9000/v1` (OpenAI-compatible) →
proxy injects tools → real model provider (Anthropic / Google / xAI) →
model emits `tool_calls` → proxy executes them against m3-memory →
results fed back → final answer returned to agent.

The proxy routes by model name: `claude-*` → Anthropic, `gemini-*` →
Google AI, `grok-*` → xAI, `sonar-*` → Perplexity. Set
`MCP_PROXY_ALLOW_DESTRUCTIVE=1` to expose `memory_delete`, `gdpr_*`,
`*_export` etc. (off by default).

#### Starting the proxy

**Foreground (development):**
```bash
python3 bin/mcp_proxy.py
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

**Windows native (no WSL):**
```powershell
Start-Process -WindowStyle Hidden `
  -FilePath "C:\Users\<you>\m3-memory\.venv\Scripts\python.exe" `
  -ArgumentList "bin\mcp_proxy.py" `
  -WorkingDirectory "C:\Users\<you>\m3-memory" `
  -RedirectStandardOutput "$env:TEMP\mcp_proxy.log" `
  -RedirectStandardError  "$env:TEMP\mcp_proxy.err"
```

**Boot-time autostart:**
- Linux: systemd user unit running `python3 bin/mcp_proxy.py` with `Restart=on-failure` (containers/SSH without D-Bus: use a `@reboot` cron entry instead)
- macOS: launchd `~/Library/LaunchAgents/ai.m3-memory.mcp-proxy.plist` with `KeepAlive` + `RunAtLoad`
- Windows: Task Scheduler `At log on` → `python.exe bin\mcp_proxy.py`, or NSSM for a real service

**Health check:** `curl -sf http://localhost:9000/v1/models && echo "proxy OK"`

> **Important:** Aider / OpenClaw will fail to reach a model if the proxy
> isn't running — their `OPENAI_BASE_URL` points at `localhost:9000`. Start
> the proxy before launching those agents, or use one of the autostart
> options above.

#### Aider

```yaml
# ~/.aider.conf.yml
openai-api-base: http://localhost:9000/v1
```

Export the API key for whichever upstream you're routing to
(`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `GEMINI_API_KEY`).
The proxy forwards the key to the provider matching the model prefix.

Keep `CONVENTIONS.md` at the repo root pointing at `AGENT_INSTRUCTIONS.md`
so Aider gets the "always use memory_search first" rules in its system
prompt — without that, the tools are available but unused.

#### OpenClaw

Edit `~/.openclaw/openclaw.json`'s `env` block:
```json
"env": {
  "OPENAI_API_KEY": "sk-...existing...",
  "OPENAI_BASE_URL": "http://localhost:9000/v1"
}
```

Back up first: `cp ~/.openclaw/openclaw.json ~/.openclaw/openclaw.json.bak`

Seed the system prompt via OpenClaw's `hooks.internal.entries.boot-md`
(enabled by default) — tell the model to call `memory_search` before
answering and `memory_write` after learning anything important. Without
that instruction the tools are available but the model won't know to use
them.

OpenClaw's own `session-memory` hook can stay enabled in parallel, or
disable it with `hooks.internal.entries.session-memory.enabled = false`
to make m3-memory the sole store. Caveat: when the proxy is down,
OpenClaw's chat completions will fail.

#### Migrating existing flat-file memory

If you already have memories in Claude Code flat files, `GEMINI.md`'s
added-memories section, or OpenClaw's SQLite, use
[`bin/migrate_flat_memory.py`](../bin/migrate_flat_memory.py). Idempotent
(safe to re-run), SHA-256 round-trip verified, never deletes source files
itself.

```bash
python3 bin/migrate_flat_memory.py --dry-run                    # preview
python3 bin/migrate_flat_memory.py                              # migrate + verify
python3 bin/migrate_flat_memory.py --sources claude,gemini      # subset
python3 bin/migrate_flat_memory.py --include-rules              # also import CLAUDE.md etc.
```

### Disable your host agent's built-in memory

- **Claude Code** has a built-in flat-file memory at
  `~/.claude/projects/<project>/memory/`. This project's
  [AGENT_INSTRUCTIONS.md](./AGENT_INSTRUCTIONS.md) already instructs
  Claude to ignore it; migrate any existing flat-file memories with the
  script above, then delete the flat files.
- **Gemini CLI, Aider, OpenCode** — no built-in memory to disable. They
  pick up `GEMINI.md` / `CONVENTIONS.md` / `AGENTS.md` respectively (all
  shims pointing at `AGENT_INSTRUCTIONS.md`).

---

## 4️⃣ Verify it works

In your agent session, write a test memory:

```
Write a memory: "M3 Memory installed successfully"
```

Your agent should call `memory_write` and return a UUID. That confirms
the MCP bridge is connected, SQLite is working, and embeddings are being
generated.

Now open a **new session** and search for it:

```
Search for: "M3 install"
```

If the memory you wrote comes back, everything is working.

### What success looks like

- `memory_write` returns a UUID (e.g., `Created: a1b2c3d4-...`)
- `memory_search` returns the memory you wrote, with a relevance score
- No errors about embedding failures or connection refused

### What failure looks like

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Embedding failed" or "Connection refused" | Sovereign CPU embedder not running | `m3 embedder status`; if not running: `m3 embedder install-gpu` (installs binary), then `m3 embedder install` (registers service), or just `m3 embedder start` if already installed |
| "m3: command not found" | Package not on PATH | `pip install m3-memory` and check `which m3` (`mcp-memory` is the backwards-compatible alias) |
| Memory tools don't appear in agent | Config not loaded | Check JSON syntax, ensure `"mcpServers"` key, restart agent fully |
| Search returns nothing in new session | Different working directory | Run from same directory, or set `M3_MEMORY_ROOT` env var |

---

## 5️⃣ Optional: cross-device sync

M3 Memory works standalone with local SQLite — no additional
infrastructure needed. For multi-device sync, you can optionally connect:

- **PostgreSQL** — bi-directional delta sync across machines
- **ChromaDB** — federated vector search across your LAN

See [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md) for `PG_URL`
and `CHROMA_BASE_URL` configuration.

---

## ▶️ Next steps

- [CORE_FEATURES.md](./CORE_FEATURES.md) — what M3 Memory can do
- [AGENT_INSTRUCTIONS.md](./AGENT_INSTRUCTIONS.md) — all MCP tools and agent behavioral rules
- [TECHNICAL_DETAILS.md](./TECHNICAL_DETAILS.md) — search internals, schema, sync, security
- [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md) — credentials and runtime config
