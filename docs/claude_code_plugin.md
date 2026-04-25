# m3-memory as a Claude Code plugin

`m3-memory` ships as a Claude Code plugin in the same repo as the Python
package. The plugin auto-registers the memory MCP, wires up chatlog
hooks, and adds 15 `/m3:*` slash commands plus a `memory-curator` subagent.

## Install

```
/plugin marketplace add skynetcmd/m3-memory
/plugin install m3-memory@skynetcmd
```

Or directly from the repo without going through the marketplace:

```
/plugin install github.com/skynetcmd/m3-memory
```

After install, restart your Claude Code session (or run `/plugin reload`)
so the new MCP server, hooks, and commands take effect.

## What it does on first run

The plugin's `SessionStart` hook checks for the `mcp-memory` CLI on
PATH. If missing, it prints a one-line install hint:

```
[m3-memory] mcp-memory CLI not on PATH. Run:
  pipx install m3-memory && pipx ensurepath
```

The plugin can't run sudo or pipx for you (Claude Code plugins are
sandboxed), but the [one-line installer](../install.sh) at the repo
root does both.

## Slash commands

Run `/m3:help` to see the full list. Highlights:

| Command | What |
|---|---|
| `/m3:doctor` | Health check — package, payload, chatlog DB, hook state |
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

The other 51 MCP tools are still callable directly via tool calls — these
slash commands are shortcuts to the high-leverage subset.

## Subagent: memory-curator

Triggered by phrases like "tidy memory", "dedupe memories", or
"consolidate notes". Surveys the memory store, finds clusters of
near-duplicates, and proposes a consolidate / supersede / leave-alone
plan that you confirm before any deletion.

## Hooks installed by the plugin

- `SessionStart` — checks `mcp-memory` is on PATH (advisory only)
- `PreCompact` — fires the chatlog ingest before context compaction
- `Stop` — fires the chatlog ingest at end of every assistant turn

These run alongside any hooks you have in `~/.claude/settings.json`. If
you previously wired chatlog hooks via `mcp-memory chatlog init
--apply-claude`, you can leave them — the hook scripts are idempotent.

## Configuration

The plugin exposes two `userConfig` knobs that Claude Code prompts for at
enable time (you can re-edit later):

- **`endpoint`** — pin `LLM_ENDPOINTS_CSV` for embedding / enrichment.
  Empty = probe both LM Studio (`:1234`) and Ollama (`:11434`).
- **`capture_mode`** — chatlog capture policy. `both` / `stop` /
  `precompact` / `none`. Default `both`.

## Claude.ai (web/desktop) integration

The plugin only works inside Claude Code. To use the same memory backend
from Claude.ai web/desktop, run `mcp-memory serve` to start the HTTP
transport, expose it via a tunnel, and add it as a custom connector in
Claude.ai settings — see [docs/claude_ai_connector.md](claude_ai_connector.md).

## Uninstall

```
/plugin uninstall m3-memory
```

This removes the plugin's hooks, MCP registration, slash commands, and
subagent. The `mcp-memory` CLI and your local memory data at
`~/.m3-memory/` are not touched — uninstall those separately with
`pipx uninstall m3-memory && rm -rf ~/.m3-memory`.
