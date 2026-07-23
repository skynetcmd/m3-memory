docs/AGENT_INSTRUCTIONS.md

## Git Standards
- **Commit Messages:** Do NOT include "Co-Authored-By" lines in commit messages. Focus on clear, concise descriptions of "why" and "what".
- **Pre-push process (MANDATORY, all agents):** After cloning, run `python bin/setup_hooks.py` once to enable the shared pre-push gate. Before any push, the tool-catalog drift check (`python bin/check_tool_catalog_drift.py`) and bench-data leakage scan must pass. If you change `bin/mcp_tool_catalog.py`, regenerate the catalog/inventory and update the "N tools" counts in the same change. Full rationale: the "Pre-push process — ALL agents" section in `docs/AGENT_INSTRUCTIONS.md`. This is enforced by the local hook AND CI, not just convention.

## Homecoming Architecture
- **Decoupled Roots:** Persistent state is split across three roots so databases and configuration can be relocated and secured independently. All three are overridable via the matching env var.
- **Data Location:**
    - Databases + runtime state — `M3_ENGINE_ROOT`, default `~/.m3/engine`: `agent_memory.db`, `agent_chatlog.db`, `files_database.db`, chatlog spill, `logs/`
    - Configs — `M3_CONFIG_ROOT`, default `~/.m3/config`: `.chatlog_config.json`, `.migrate_config.json`, `.agent_os_salt`
    - Payload / repo clone — `M3_MEMORY_ROOT`, default `~/.m3-memory`
- **Precedence:** `M3_ENGINE_ROOT` / `M3_CONFIG_ROOT` → `M3_MEMORY_ROOT/{engine,config}` → `~/.m3/{engine,config}`. `M3_MEMORY_ROOT` acts as a master override only when the specific vars are unset; a specific root always wins. Canonical implementation: `get_m3_engine_root()` / `get_m3_config_root()` in `bin/m3_core/paths.py`.
- **Split-brain hazard:** the MCP server reads roots from its `env` block in client settings, while hooks inherit the client's process env. Pin `M3_ENGINE_ROOT` + `M3_CONFIG_ROOT` in **both** or the server and the chatlog hook will write to different stores.
- **Migration:** Use `bin/homecoming.py` to relocate legacy repo-relative state to the new home root.
