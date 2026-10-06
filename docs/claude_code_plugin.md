# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> m3-memory as a Claude Code plugin

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

> **No GitHub SSH key?** The `owner/repo` shorthand uses SSH. If you get a
> "Premature close" or "ERR_STREAM_PREMATURE_CLOSE" error, use the HTTPS URL:
> ```
> /plugin marketplace add https://github.com/skynetcmd/m3-memory
> /plugin install m3@skynetcmd
> ```

Or directly from the repo without going through the marketplace:

```
/plugin install https://github.com/skynetcmd/m3-memory
```

After install, restart your Claude Code session (or run `/reload-plugins`)
so the new MCP server, hooks, and commands take effect.

> **Plugin or `m3 setup`, not both.** `m3 setup` registers m3 directly
> (`claude mcp add --scope user -- m3_memory m3`) and keeps the plugin's server
> disabled. Running both gives two live m3 servers (`mcp__m3_memory__` and
> `mcp__plugin_m3_memory__`); `m3 doctor --fix --fix-hooks` converges back to one.

## Updating m3

To upgrade an installed plugin to the latest published version, run these
**in order**:

```
/plugin marketplace update skynetcmd      # refresh the marketplace metadata from GitHub
/plugin install m3@skynetcmd              # re-install → upgrades to the newest version
/reload-plugins                           # restart the m3 MCP server + reload hooks/commands
```

`/reload-plugins` restarts the MCP server in-place, so a full Claude Code
restart is not required.

> **⚠️ Re-installing can silently disable the plugin.** On some Claude Code
> versions, `/plugin install` of an already-installed plugin flips it to
> **disabled** — after which m3 vanishes from `/mcp` and no `mcp__…m3…` tools
> load, even though the reload "succeeded". If that happens:
> 1. Check `~/.claude/settings.json` → `enabledPlugins` → `"m3@skynetcmd"`.
> 2. If it is `false`, set it to `true`.
> 3. Run `/reload-plugins` again.
>
> `m3 doctor` detects this exact state (stale/disabled plugin) and prints these
> steps — run it if m3's tools ever go missing after an update.

**Verify the update landed:** `m3 doctor` reports the served plugin version, or
check that the m3 tools are back with `/mcp`. A successful update loads the new
version from `~/.claude/plugins/cache/skynetcmd/m3/<version>/`.

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

The full 100+ tool catalog is still callable via tool calls — these
slash commands are shortcuts to the high-leverage subset. The catalog is
domain-gated by default so unused tools don't burn context; see the
[lazy-loading note](../README.md#domain-gating) in the README for details.

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

The plugin exposes six `userConfig` knobs that Claude Code prompts for at
enable time (you can re-edit later):

- **`endpoint`** — pin `LLM_ENDPOINTS_CSV` for the small chat model used
  by enrichment. The embedder itself is BGE-M3 on the shared embed server
  (`127.0.0.1:8082`) installed by `m3 setup` — this knob is only for
  *generation*. Empty = probe local OpenAI-compatible servers (LM Studio
  `:1234`, Ollama `:11434`).
- **`capture_mode`** — chatlog capture policy. `both` / `stop` /
  `precompact` / `none`. Default `both`.
- **`embed_fallback_url`** — the shared embed server URL
  (`M3_EMBED_FALLBACK_URL`). Default `http://127.0.0.1:8082`.
- **`embed_gguf`** — path to a BGE-M3 GGUF (`M3_EMBED_GGUF`). Optional;
  in-process embedding still needs `M3_EMBED_INPROC=1` (or an
  `.embed_config.json` that permits it). Default empty.
- **`engine_root`** / **`config_root`** — advanced overrides for
  `M3_ENGINE_ROOT` / `M3_CONFIG_ROOT`. Leave empty for the defaults
  (`~/.m3/engine`, `~/.m3/config`); if set, they must match every other m3
  process or the server and the chatlog hooks read different databases.

---

## Claude.ai (web/desktop) integration

The plugin only works inside Claude Code. To use the same memory backend
from Claude.ai web/desktop, run `m3 serve` to start the HTTP transport,
expose it via a tunnel, and add it as a custom connector in Claude.ai
settings — see [docs/claude_ai_connector.md](claude_ai_connector.md).

---

## Uninstall

```
/plugin uninstall m3@skynetcmd
```

This removes the plugin's hooks, MCP registration, slash commands, and
subagents. The `m3` CLI, its payload, and your memory data are not touched.
To remove the CLI side too: `m3 uninstall` (removes the payload under
`~/.m3-memory/` and its config file), then `pipx uninstall m3-memory`. Your
databases live in `~/.m3/engine` (config in `~/.m3/config`, logs in
`~/.m3/logs`) — delete `~/.m3` only if you really want the memories gone.
