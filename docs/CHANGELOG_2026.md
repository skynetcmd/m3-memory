# M3 Memory System Changelog - 2026

## April 12, 2026

### ✨ Multi-Agent Orchestration

#### Orchestration Primitives
- **Agent registry** — `agent_register`, `agent_heartbeat`, `agent_list`, `agent_get`, `agent_offline` for tracking online agents and their capabilities.
- **Handoffs** — `memory_handoff` transfers ownership of a memory item between agents with audit trail.
- **Notifications** — `notify`, `notifications_poll`, `notifications_ack`, `notifications_ack_all` for inter-agent messaging.
- **Tasks** — `task_create`, `task_assign`, `task_update`, `task_get`, `task_list`, `task_set_result`, `task_tree` for distributed work tracking.

#### `m3-team` CLI
- New entry point `m3-team init|check|run` (registered in `pyproject.toml`).
- Implementation: `m3_memory/team_cli.py` (237 lines).
- Spins up multi-agent teams from a single YAML file (`team.yaml` or `team.minimal.yaml`).

#### Dispatch Loop
- `examples/multi-agent-team/dispatch.py` (366 lines) — provider-agnostic multi-turn MCP dispatch loop.
- `DispatchLimits`: `max_turns=8`, `max_tool_calls=24`, `max_seconds=120`, `max_tokens_per_call=4096`, `provider_retries=3`.
- `DispatchResult` terminal taxonomy with bounded retries and loop detection.
- Provider translation for OpenAI, Anthropic, and Gemini tool-calling formats.

### 🔧 MCP Proxy v2 (`bin/mcp_proxy.py`)

#### Catalog-Driven Dispatch
- Replaced 250-line hardcoded `MCP_TOOLS` list with three sources:
  - `PROTOCOL_TOOLS` (5 inline): `log_activity`, `query_decisions`, `update_focus`, `retire_focus`, `check_thermal_load`.
  - `DEBUG_TOOLS` (6 inline): `debug_analyze`, `debug_bisect`, `debug_trace`, `debug_correlate`, `debug_history`, `debug_report`.
  - `_build_catalog_tools()` lazy loader pulling from `bin/mcp_tool_catalog.py` (35 default / 44 with destructive enabled).
- New `_LazyToolList` class for backwards-compat `MCP_TOOLS` attribute.
- New `_LEGACY_DISPATCH` dict for protocol/debug tool routing.

#### Agent Identity Enforcement
- `/v1/chat/completions` now reads `X-Agent-Id` HTTP header.
- `_execute_tool(name, args, agent_id="mcp-proxy-client")` propagates identity to catalog dispatch.
- Catalog tools with `inject_agent_id=True` (`memory_write`, `agent_heartbeat`, `agent_offline`, `memory_inbox`, `notifications_poll`, `notifications_ack_all`) are non-bypassable: client-claimed `agent_id` is overridden from the header.

#### Destructive Tool Gating
- `MCP_PROXY_ALLOW_DESTRUCTIVE=1` opt-in env flag.
- 9 destructive tools hidden by default: `memory_delete`, `chroma_sync`, `memory_maintenance`, `memory_set_retention`, `memory_export`, `memory_import`, `gdpr_export`, `gdpr_forget`, `agent_offline`.
- `/health` endpoint reports per-source counts and `allow_destructive` flag.

### 🐛 Bug Fixes
- **mcp_proxy ImportError**: `from m3_sdk import M3Context, LM_STUDIO_BASE, LM_READ_TIMEOUT` failed because `m3_sdk.py` only exports `M3Context`. Inlined the constants as proxy-local env reads (`LMSTUDIO_BASE`, `READ_TIMEOUT`).
- **15-tool gap**: Proxy clients (Aider, OpenClaw) previously had access to only 15 of 44 catalog tools. Now full parity at 55/55.
- Agent identity bypass**: Proxy did not enforce `inject_agent_id`, letting clients spoof `agent_id` on `memory_write`. Now overridden from `X-Agent-Id` header.
- **Stale `memory_write` schema**: Proxy advertised an outdated parameter set; now sourced from catalog `ToolSpec`.

### 📚 Documentation & Architecture
- License changed from MIT to **Apache 2.0** for clearer patent grant in multi-agent contexts.
- `bin/mcp_tool_catalog.py` (NEW) — single source of truth for all MCP tool definitions via `ToolSpec` dataclass.
- `VALID_MEMORY_TYPES` expanded to 20 types; `bin/memory_core.py` auto-classifier kept in sync (added `knowledge` type).
- Knowledge entry `da5d487d` ("Available Knowledgebase Item Types") rewritten with full 20-type taxonomy.
- Knowledge entry `a18d6a67` ("OpenClaw & MCP Proxy Integration Architecture") updated for v2.

### ✅ Verification
- **Test Suite**: 193/193 end-to-end tests passing.
- **MCP Proxy Unit Tests**: 12/12 passing in `bin/test_mcp_proxy_unit.py`.
- **Default tool count**: 5 protocol + 6 debug + 46 catalog = 57.
- **With `MCP_PROXY_ALLOW_DESTRUCTIVE=1`**: 5 + 6 + 55 = 66.

---

## April 6, 2026

### ✨ New Features

#### Conversation Summarization
- Implemented `conversation_summarize_impl` in `bin/memory_core.py`.
- Automated summarization of long conversations into 3-5 key points using local LLM inference.
- New MCP tool: `conversation_summarize(conversation_id, threshold=20)`.
- Summaries are stored as `summary` type and linked via `references` relationship.

#### Tier 5 Implementation Complete
- **LLM Auto-Classification**: Intelligent categorization of memories using LLM inference (enabled via `type='auto'`).
- **Explainability (memory_suggest)**: New search mode providing detailed scoring breakdowns (Vector + BM25 + MMR penalty).
- **Multi-layered Consolidation**: Background summarization of old memories by type/agent to reduce clutter while preserving knowledge.
- **Portable Export/Import**: JSON-based backup and restoration of memories, including embeddings and relationships.
- **Retrieval Benchmarks**: New `bin/benchmark_memory.py` utility for measuring retrieval quality (MRR, Hit@N, Latency).
- **Configurable Limits**: Moved hardcoded thresholds to environment variables:
  - `DEDUP_LIMIT` (default 1000)
  - `DEDUP_THRESHOLD` (default 0.92)
  - `CONTRADICTION_THRESHOLD` (default 0.85)
  - `SEARCH_ROW_CAP` (default 500)

### 🐛 Bug Fixes
- **Search Recursion Fix**: Resolved a critical bug in `memory_search_impl` where recursion for FTS-to-semantic fallback was incorrectly passing state into bitemporal filter parameters.
- **Relationship Schema**: Fixed `memory_export` to exclude non-existent `metadata_json` column from `memory_relationships`.
- **Benchmark Reliability**: Standardized LM Studio connectivity checks to use `localhost` and proper API tokens.

### 📚 Documentation & Architecture
- Updated `ARCHITECTURE.md` with 6 new Tier 5 tools.
- Expanded `VALID_MEMORY_TYPES` to include `auto`.
- Added `consolidates` to `VALID_RELATIONSHIP_TYPES`.

### ✅ Verification
- **Test Suite**: 161/161 tests passing in `bin/test_memory_bridge.py`.
- **Quality**: Retrieval MRR 1.0 achieved in standardized benchmarks.
