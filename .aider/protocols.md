# M3 MAX AGENTIC OS: AIDER PROTOCOLS

## MCP Limitation
Aider has NO native MCP support. It cannot call `custom_pc_tool`, `memory`, `grok_intel`,
or `web_research` bridges automatically. The 5 Operational Protocols in ARCHITECTURE.md
must be followed MANUALLY by the orchestrating agent (Claude Code) before/after aider sessions.

## Architecture Reference
Full spec is in `[PROJECT_ROOT]/ARCHITECTURE.md` (4-bridge MCP architecture):
- `custom_pc_tool` — log_activity, update_focus, query_decisions, retire_focus, check_thermal_load, query_local_model
- `memory` — memory_write, memory_search, memory_update, memory_delete, conversation_*
- `grok_intel` — Grok 3 real-time X/Twitter data
- `web_research` — Perplexity sonar-pro live web search

> NOTE: `local_logic` is RETIRED. Do not reference or invoke it.

## Working Rules
- **Validation Mandate:** Never assume code works. Run tests after every major edit.
- **Architect Mode:** Prefer `--architect` for complex refactors.
- **Local Model:** When using `aider-local`, LM Studio must be running on port 1234.
- **Model ID:** `deepseek-r1-distill-llama-70b-mlx` — must match exactly.
- **Decision Logging:** After any agreed code change, the orchestrating Claude Code session
  must call `custom_pc_tool → log_activity(category="decision", ...)` per Protocol #3.
