# Changelog

All notable changes to M3 Memory are documented here.

---

## [2026.4.12] — April 12, 2026 — Multi-Agent Orchestration + MCP Proxy v2

### Added
- **Orchestration primitives** — agent registry (`agent_register`, `agent_heartbeat`, `agent_list`), handoffs (`memory_handoff`), notifications (`notify`, `notifications_poll`, `notifications_ack`), and tasks (`task_create`, `task_assign`, `task_update`, `task_set_result`, `task_tree`) for multi-agent coordination
- **`m3-team` CLI** — `m3-team init|check|run` for spinning up multi-agent teams from a single YAML file
- **`examples/multi-agent-team/`** — provider-agnostic orchestrator with bounded dispatch loop (`DispatchLimits`: max_turns=8, max_tool_calls=24, max_seconds=120, provider_retries=3) and terminal `DispatchResult` taxonomy
- **`team.minimal.yaml`** — single LM Studio agent example, zero API keys required
- **`bin/mcp_tool_catalog.py`** — single source of truth for all MCP tool definitions via `ToolSpec` dataclass; 44 tools (55 with destructive enabled)
- **MCP proxy v2** (`bin/mcp_proxy.py`) — catalog-driven dispatch replacing the prior 15-tool hardcoded list; reads `X-Agent-Id` header and enforces `inject_agent_id` so client-claimed identity cannot be bypassed
- **`MCP_PROXY_ALLOW_DESTRUCTIVE`** env flag — gates 9 destructive tools (`memory_delete`, `chroma_sync`, `memory_maintenance`, `memory_set_retention`, `memory_export`, `memory_import`, `gdpr_export`, `gdpr_forget`, `agent_offline`) behind opt-in
- **`bin/test_mcp_proxy_unit.py`** — 12 in-process unit tests covering imports, tool counts, destructive filtering, dispatch, and agent_id injection

### Changed
- **License** → Apache 2.0 (from MIT) for clearer patent grant in multi-agent contexts
- **`VALID_MEMORY_TYPES`** expanded to 20 types; `bin/memory_core.py` auto-classifier kept in sync
- **MCP proxy** now sources its tool list from `mcp_tool_catalog` instead of an inline hardcoded list — adds 29 previously missing tools to proxy clients (Aider, OpenClaw)

### Fixed
- **mcp_proxy ImportError** — `LM_STUDIO_BASE` and `LM_READ_TIMEOUT` were imported from `m3_sdk` but no longer exist there; inlined as proxy-local env reads
- **Tool count gap** — proxy clients had access to only 15 of 44 catalog tools; now have full parity
- **Agent identity bypass** — proxy did not enforce `inject_agent_id`, letting clients spoof `agent_id` on `memory_write`; now overridden from `X-Agent-Id` header

### Test Coverage
- 193/193 end-to-end tests passing
- 12/12 mcp_proxy unit tests passing
- Default tool count: 5 protocol + 6 debug + 35 catalog = 46
- With `MCP_PROXY_ALLOW_DESTRUCTIVE=1`: 5 + 6 + 44 = 55

---

## [2026.4.8] — April 10, 2026 — PyPI Launch

### Added
- `m3_memory` Python package with `mcp-memory` CLI entry point — `pip install m3-memory` now works end-to-end
- `mcp-memory` command auto-starts the MCP server; no path configuration required for pip installs
- `ROADMAP.md` — v0.2 through v1.0 milestones with community voting link
- `publish.yml` GitHub Actions workflow — automated PyPI publish on GitHub Release via OIDC trusted publishing

### Changed
- `pyproject.toml` — proper package discovery, pinned `dependencies`, `[project.optional-dependencies]`, fixed license metadata

---

## [2026.04.06] — April 6, 2026 — Production Release

### Added
- **Conversation summarization** — `conversation_summarize` compresses long threads into 3-5 key points via local LLM
- **LLM auto-classification** — `type="auto"` lets the local LLM categorize memories into one of 18 types
- **Explainable search** — `memory_suggest` returns full score breakdowns (vector + BM25 + MMR penalty) per result
- **Multi-layered consolidation** — `memory_consolidate` merges old memory groups into LLM-generated summaries
- **Portable export/import** — JSON round-trip backup including embeddings and relationships
- **Retrieval benchmarks** — `bin/bench_memory.py` measures MRR, Hit@k, and latency
- **Configurable thresholds** — `DEDUP_LIMIT`, `DEDUP_THRESHOLD`, `CONTRADICTION_THRESHOLD`, `SEARCH_ROW_CAP` via env vars
- **MCP tool set** — memory ops, knowledge graph, conversations, lifecycle, data governance, and operations (44 tools as of 2026.4.12)

### Fixed
- Search recursion bug in `memory_search_impl` — FTS-to-semantic fallback was incorrectly passing state into bitemporal filter parameters
- `memory_export` excluded non-existent `metadata_json` column from `memory_relationships`
- LM Studio connectivity checks standardized to `localhost` with proper API tokens

### Changed
- `VALID_MEMORY_TYPES` expanded to include `auto`
- `VALID_RELATIONSHIP_TYPES` expanded to include `consolidates`
- AES-256 vault upgraded to PBKDF2 600K iterations (auto-migrates legacy 100K secrets on first decryption)

### Test Coverage
- 41 end-to-end tests passing across all features
- Retrieval MRR 1.0 achieved in standardized benchmarks
- CI: lint (Ruff) + typecheck (Mypy) + pytest on Ubuntu/macOS/Windows × Python 3.11/3.12

---

For the full technical history see [docs/CHANGELOG_2026.md](./docs/CHANGELOG_2026.md).
