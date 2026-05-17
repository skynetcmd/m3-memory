# m3-memory as a Claude Code plugin

`m3-memory` ships as a Claude Code plugin in the same repo as the Python
package. The plugin auto-registers the memory MCP, wires up chatlog
hooks, and adds 15 `/m3:*` slash commands plus two curator subagents
(`m3:curate-memory` for the memory store and `m3:curate-chatlog` for
captured agentic-coding conversations).

## Install

```
/plugin marketplace add skynetcmd/m3-memory
/plugin install m3@skynetcmd
```

Or directly from the repo without going through the marketplace:

```
/plugin install github.com/skynetcmd/m3-memory
```

After install, restart your Claude Code session (or run `/plugin reload`)
so the new MCP server, hooks, and commands take effect.

---

## What it does on first run

The plugin's `SessionStart` hook checks for the `m3` (or `mcp-memory`)
CLI on PATH. If missing, it prints a one-line install hint:

```
[m3-memory] m3 CLI not on PATH. Run:
  pipx install m3-memory && pipx ensurepath
  m3 setup
```

The plugin can't run sudo or pipx for you (Claude Code plugins are
sandboxed), but the [one-line installer](../install.sh) at the repo
root does both — and then drives `m3 setup` end-to-end.

---

## Slash commands

Run `/m3:help` to see the full list. Highlights:

| Command | What |
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

The full 87-tool catalog is still callable via tool calls — these
slash commands are shortcuts to the high-leverage subset. The catalog is
domain-gated by default so unused tools don't burn context; see the
[lazy-loading note](../README.md#-87-tools-but-they-dont-all-crowd-your-context--domain-gating-keeps-the-catalog-small) in the README for details.

---

## Subagents: `m3:curate-memory` and `m3:curate-chatlog`

Two curator subagents handle the two stores:

- **`m3:curate-memory`** — triggered by "curate memory", "tidy memory",
  "dedupe memory", or "consolidate memory". Surveys the memory store,
  finds clusters of near-duplicates, and proposes a consolidate /
  supersede / leave-alone plan that you confirm before any deletion.
- **`m3:curate-chatlog`** — triggered by "curate chatlog", "tidy chatlog",
  "dedupe chatlog", or "consolidate chatlog". Same workflow against the
  chatlog store, plus aggressive ephemeral-content decay (transient PIDs,
  status snapshots, short user commands lose retrieval ranking with age).
  Deferred to `bin/chatlog_decay.py` for the heavy lifting.

Both use a two-spawn execution model: the first invocation surveys and
proposes a plan; you re-spawn with the structured plan back as input
(prefixed with `apply`) to actually execute it. The plan is always
human-reviewable and reversible.

---

## Hooks installed by the plugin

- `SessionStart` — checks `m3` is on PATH (advisory only)
- `PreCompact` — fires the chatlog ingest before context compaction
- `Stop` — fires the chatlog ingest at end of every assistant turn

These run alongside any hooks you have in `~/.claude/settings.json`. If
you previously wired chatlog hooks via `m3 chatlog init --apply-claude`
(or its legacy `mcp-memory chatlog init` form), you can leave them — the
hook scripts are idempotent.

---

## Configuration

The plugin exposes two `userConfig` knobs that Claude Code prompts for at
enable time (you can re-edit later):

- **`endpoint`** — pin `LLM_ENDPOINTS_CSV` for the small chat model used
  by enrichment. The embedder itself is the sovereign in-process BGE-M3
  installed by `m3 setup` — this knob is only for *generation*. Empty =
  probe local OpenAI-compatible servers (Ollama `:11434`, etc.).
- **`capture_mode`** — chatlog capture policy. `both` / `stop` /
  `precompact` / `none`. Default `both`.

---

## Claude.ai (web/desktop) integration

The plugin only works inside Claude Code. To use the same memory backend
from Claude.ai web/desktop, run `m3 serve` to start the HTTP transport,
expose it via a tunnel, and add it as a custom connector in Claude.ai
settings — see [docs/claude_ai_connector.md](claude_ai_connector.md).

---

## Uninstall

```
/plugin uninstall m3-memory
```

This removes the plugin's hooks, MCP registration, slash commands, and
subagent. The `m3` CLI and your local memory data at `~/.m3-memory/` are
not touched — uninstall those separately with
`pipx uninstall m3-memory && rm -rf ~/.m3-memory`.
