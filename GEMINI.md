docs/AGENT_INSTRUCTIONS.md

## Git Standards
- **Commit Messages:** Do NOT include "Co-Authored-By" lines in commit messages. Focus on clear, concise descriptions of "why" and "what".

## Homecoming Architecture
- **Unified Root:** All persistent state (databases, configs, logs, models) defaults to `~/.m3-memory/`.
- **Environment Override:** Use `M3_MEMORY_ROOT` to relocate the entire system state.
- **Data Location:**
    - Databases: `~/.m3-memory/memory/*.db`
    - Configs: `~/.m3-memory/memory/.chatlog_config.json`, etc.
    - Security Salt: `~/.m3-memory/.agent_os_salt`
- **Migration:** Use `bin/homecoming.py` to relocate legacy repo-relative state to the new home root.
