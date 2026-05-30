# m3-memory as a Google Antigravity plugin

`m3-memory` ships as a Google Antigravity plugin in the same repo as the Python package. The plugin auto-registers the memory MCP, wires up chatlog hooks, and adds 15 `/m3:*` slash commands as native Skills plus two curator subagents (`m3:curate-memory` for the memory store and `m3:curate-chatlog` for captured agentic-coding conversations).

## Install

```bash
agy plugin install https://github.com/skynetcmd/m3-memory
```

After install, restart your Antigravity CLI/Desktop session (or reload the environment) so the new MCP server, hooks, and Skills take effect.

---

## What it does on first run

The plugin's `SessionStart` hook checks for the `m3` (or `mcp-memory`) CLI on PATH. If missing, it prints a one-line install hint:

```
[m3-memory] m3 CLI not on PATH. Run:
  pipx install m3-memory && pipx ensurepath
  m3 setup
```

---

## Slash commands (Agent Skills)

Run `/m3:help` (leveraging the `m3-help` Skill) to see the full list of commands. Highlights:

| Command / Skill | What |
|---|---|
| `/m3:health` | Health check — package, payload, chatlog DB, hook state |
| `/m3:status` | Chatlog row counts, queue, last capture |
| `/m3:search <q>` | Hybrid memory search |
| `/m3:save <content>` | Auto-classified memory_write with confirmation |
| `/m3:write <content>` | Direct memory_write |
| `/m3:get <id>` | Fetch one memory |
| `/m3:graph <id>` | Knowledge-graph traversal |
| `/m3:forget <id>` | Delete with confirmation |
| `/m3:export` | GDPR Article 20 export |
| `/m3:tasks` | Task list |
| `/m3:agents` | Registered agents |
| `/m3:notify` | Inbox poll |
| `/m3:find-in-chat <q>` | Search captured chat-log turns |
| `/m3:install` | Install / upgrade |
| `/m3:help` | This list |

The full catalog of MCP tools remains callable directly via tool calls — these Skills are shortcuts to the high-leverage subset.

---

## Subagents: `m3:curate-memory` and `m3:curate-chatlog`

Two curator subagents handle the two stores:

- **`m3:curate-memory`** — triggered by "curate memory", "tidy memory", "dedupe memory", or "consolidate memory". Surveys the memory store, finds clusters of near-duplicates, and proposes a consolidate / supersede / leave-alone plan that you confirm before any deletion.
- **`m3:curate-chatlog`** — triggered by "curate chatlog", "tidy chatlog", "dedupe chatlog", or "consolidate chatlog". Same workflow against the chatlog store, plus aggressive ephemeral-content decay.

Both use a two-spawn execution model: the first invocation surveys and proposes a plan; you re-spawn with the structured plan back as input (prefixed with `apply`) to actually execute it. The plan is always human-reviewable and reversible.

---

## Hooks installed by the plugin

- `SessionStart` — checks `m3` is on PATH (advisory only)
- `PreCompact` — fires the chatlog ingest before context compaction
- `Stop` — fires the chatlog ingest at the end of every assistant turn

These run alongside any hooks you have in `~/.gemini/antigravity-cli/settings.json`.

---

## Configuration

The plugin exposes four `userConfig` knobs that Google Antigravity prompts for at enable time:

- **`endpoint`** — pin `LLM_ENDPOINTS_CSV` for the small chat model used by enrichment. The embedder itself is the sovereign in-process BGE-M3 installed by `m3 setup`. Empty = probe local OpenAI-compatible servers (Ollama `:11434`, etc.).
- **`capture_mode`** — chatlog capture policy. `both` / `stop` / `precompact` / `none`. Default `both`.
- **`embed_fallback_url`** — fallback sovereign HTTP embedder service URL on `http://127.0.0.1:8082`.
- **`embed_gguf`** — optional absolute path to BGE-M3 GGUF for in-process acceleration.
