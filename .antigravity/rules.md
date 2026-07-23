# Google Antigravity Codebase Rules

All persistent rules, coding conventions, git standards, and memory protocols for this repository are defined in the canonical instruction file:

👉 **[AGENT_INSTRUCTIONS.md](../docs/AGENT_INSTRUCTIONS.md)**

## Git Standards
- **Commit Messages:** Do NOT include "Co-Authored-By" lines in commit messages. Focus on clear, concise descriptions of "why" and "what".

## Homecoming Architecture
- **Decoupled Roots:** Persistent state is split across three roots so databases and configuration can be relocated and secured independently. All three are overridable via the matching env var.
- **Data Location:**
    - Databases + runtime state — `M3_ENGINE_ROOT`, default `~/.m3/engine`: `agent_memory.db`, `agent_chatlog.db`, `files_database.db`, chatlog spill, `logs/`
    - Configs — `M3_CONFIG_ROOT`, default `~/.m3/config`: `.chatlog_config.json`, `.migrate_config.json`, `.agent_os_salt`
    - Payload / repo clone — `M3_MEMORY_ROOT`, default `~/.m3-memory`
- **Precedence:** `M3_ENGINE_ROOT` / `M3_CONFIG_ROOT` → `M3_MEMORY_ROOT/{engine,config}` → `~/.m3/{engine,config}`. `M3_MEMORY_ROOT` acts as a master override only when the specific vars are unset; a specific root always wins. Canonical implementation: `get_m3_engine_root()` / `get_m3_config_root()` in `bin/m3_core/paths.py`.
- **Split-brain hazard:** the MCP server reads roots from its `env` block in client settings, while hooks inherit the client's process env. Pin `M3_ENGINE_ROOT` + `M3_CONFIG_ROOT` in **both** or the server and the chatlog hook will write to different stores.
- **Migration:** Use `bin/homecoming.py` to relocate legacy repo-relative state to the new home root.

## Memory Protocol Override
- **M3 Memory System Protocol**: In workspaces containing 'm3-memory', always prioritize the M3 system for long-term state.
- **Search First**: Before answering any context-dependent questions, always call `memory_search` first.
- **Write Aggressively**: Call `memory_write` to persist any fact, decision, preference, or observation.
