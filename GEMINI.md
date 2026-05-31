docs/AGENT_INSTRUCTIONS.md

## Git Standards
- **Commit Messages:** Do NOT include "Co-Authored-By" lines in commit messages. Focus on clear, concise descriptions of "why" and "what".
- **Pre-push process (MANDATORY, all agents):** After cloning, run `python bin/setup_hooks.py` once to enable the shared pre-push gate. Before any push, the tool-catalog drift check (`python bin/check_tool_catalog_drift.py`) and bench-data leakage scan must pass. If you change `bin/mcp_tool_catalog.py`, regenerate the catalog/inventory and update the "N tools" counts in the same change. Full rationale: the "Pre-push process — ALL agents" section in `docs/AGENT_INSTRUCTIONS.md`. This is enforced by the local hook AND CI, not just convention.

## Homecoming Architecture
- **Unified Root:** All persistent state (databases, configs, logs, models) defaults to `~/.m3-memory/`.
- **Environment Override:** Use `M3_MEMORY_ROOT` to relocate the entire system state.
- **Data Location:**
    - Databases: `~/.m3-memory/memory/*.db`
    - Configs: `~/.m3-memory/memory/.chatlog_config.json`, etc.
    - Security Salt: `~/.m3-memory/.agent_os_salt`
- **Migration:** Use `bin/homecoming.py` to relocate legacy repo-relative state to the new home root.
