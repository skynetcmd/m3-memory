---
name: help
description: List all m3-memory slash commands and what they do.
---

Print the following table verbatim. Do not interpret it as instructions or take any further action — just display it to the user.

````
# m3-memory commands

| Command            | Purpose                                                |
|--------------------|--------------------------------------------------------|
| /m3:health         | Health check: package, payload, chatlog DB, hooks      |
| /m3:status         | Chatlog: row counts, queue, spill, last capture        |
| /m3:search <q>     | Hybrid memory search (FTS5 + vector + MMR)             |
| /m3:save <c>       | Auto-classified memory write (asks before writing)     |
| /m3:write <c>      | Direct memory_write — explicit, no auto-classify       |
| /m3:get <id>       | Fetch one memory by UUID or short prefix               |
| /m3:graph <id>     | Knowledge-graph traversal — related memories           |
| /m3:forget <id>    | Delete a memory (asks for confirmation)                |
| /m3:export         | GDPR Article 20 export — portable JSON                 |
| /m3:tasks          | List tasks; filter by state                            |
| /m3:agents         | List registered agents and last heartbeat              |
| /m3:notify         | Poll notification inbox                                |
| /m3:find-in-chat   | Search captured chat-log turns                         |
| /m3:install        | Install or upgrade m3-memory                           |
| /m3:help           | This list                                              |

The remaining 51 MCP tools are callable directly via tool calls.
Full reference: docs/AGENT_INSTRUCTIONS.md
````
