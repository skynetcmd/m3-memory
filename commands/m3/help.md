---
name: m3:help
description: Show all m3-memory slash commands and what they do.
---

# m3-memory commands

| Command | Purpose |
|---|---|
| `/m3:doctor` | Health check: package version, payload, chatlog DB, per-agent hooks |
| `/m3:status` | Chatlog subsystem status — row counts, queue depth, last capture |
| `/m3:search <query>` | Hybrid memory search (FTS5 + vector + MMR) |
| `/m3:save <content>` | Suggested-best-for-context memory_write (asks before writing) |
| `/m3:write <content>` | Direct memory_write — explicit, no auto-classification |
| `/m3:get <id>` | Fetch one memory by UUID (or short prefix) |
| `/m3:graph <id>` | Show related memories — knowledge-graph traversal |
| `/m3:forget <id>` | Delete a memory (asks for confirmation) |
| `/m3:export` | GDPR Article 20 export of all memories you own |
| `/m3:tasks` | List your tasks and their status |
| `/m3:agents` | List registered agents |
| `/m3:notify` | Poll the inbox for new notifications |
| `/m3:find-in-chat <query>` | Search captured chat-log turns (Claude + Gemini history) |
| `/m3:install` | Install / upgrade m3-memory CLI + payload |
| `/m3:help` | This command |

The 66 underlying MCP tools are also callable directly via tool calls — these
slash commands just provide one-keypress shortcuts to the high-leverage ones.

Full tool reference: [docs/AGENT_INSTRUCTIONS.md](https://github.com/skynetcmd/m3-memory/blob/main/docs/AGENT_INSTRUCTIONS.md)
