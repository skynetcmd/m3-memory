# Changelog

All notable changes to M3 Memory are documented here.

---

## [Unreleased] — Phase 1 Ingestion Optimizations

### Added
- **Always-on: temporal-anchor prefix in `embed_text`.** When `metadata["temporal_anchors"]` contains resolved `YYYY-MM-DD` dates, they are prepended to the embed text as `[YYYY-MM-DD, ...] …` before embedding. No flag; free when anchors are absent. Lets vector / FTS queries hit absolute dates even when the source says "yesterday".
- **New memory type `event_extraction`** added to `VALID_MEMORY_TYPES` (now 21 types) and the `type="auto"` classifier's local set.
- **Opt-in ingestion enrichment** (off by default; fire only for `type="message"` rows with a `conversation_id`):
  - `M3_INGEST_WINDOW_CHUNKS=1` — emit a `type="summary"` row every `M3_INGEST_WINDOW_SIZE` (default 3) turns concatenating prior bodies. Captures Q&A pairs single-turn embeds miss.
  - `M3_INGEST_GIST_ROWS=1` — emit a heuristic `type="summary"` gist row once a conversation passes `M3_INGEST_GIST_MIN_TURNS` (default 8), then every `M3_INGEST_GIST_STRIDE` (default 8) turns. Deterministic; no LLM.
  - `M3_INGEST_EVENT_ROWS=1` — regex-extract `<ProperNoun> <verb> ... <date hint>` sentences and emit one `type="event_extraction"` row per match, linked back via `references`. Deterministic; no LLM.
  - `M3_QUERY_TYPE_ROUTING=1` — retrieval-side: when a query matches "When / what date / which day" + a proper noun, shift `vector_weight` to `0.3` (BM25-heavy) so the named-entity signal isn't diluted by embedding similarity.

### Docs
- **ENVIRONMENT_VARIABLES.md** — new "Ingestion Enrichment (opt-in)" section with the five new env vars and the always-on temporal-anchor behavior.
- **TECHNICAL_DETAILS.md** — env-var rows added; valid-type count corrected 20 → 21 (includes `knowledge` and new `event_extraction`).

### Notes
- Emitters run from the per-item `memory_write` path only; `memory_write_bulk` intentionally bypasses enrichment for fast loader throughput.

---

## [2026.4.12b] — April 12, 2026 — Conversation Grouping, Refresh Lifecycle, Reversible Migrations

### Added
- **Reversible migration system** — `bin/migrate_memory.py` rewritten as a subcommand CLI: `status`, `up`, `down --to N`, `backup`, `restore`. Paired `NNN_name.up.sql` / `NNN_name.down.sql` files. File-level DB backups (including `-wal` / `-shm`) written automatically before every `up`/`down` to a user-chosen directory (default `~/.m3-memory/backups/`, persisted in `memory/.migrate_config.json`). Interactive confirmation with `-y` escape hatch for CI. Legacy v001–v012 treated as up-only — `down` refuses to cross them with a clear error naming the lowest reversible target.
- **`memory_items.conversation_id`** (migration v013) — groups memories by conversation / team session. Same ID space as `conversation_start` / `conversation_append`. Accepted as a parameter on `memory_write`, `memory_update`, and `memory_search`.
- **`memory_items.refresh_on` + `refresh_reason`** (migration v014) — planned-obsolescence timestamps. Partial index on `refresh_on WHERE refresh_on IS NOT NULL` keeps lookups O(flagged-rows).
- **`memory_refresh_queue` MCP tool** (45 total) — read-only query for memories due for review. Params: `agent_id`, `limit`, `include_future`.
- **Refresh backlog surfaces via three off-path channels:**
  - Pull: `memory_refresh_queue` tool
  - Lifecycle hint: `agent_register` and `agent_offline` response strings append `| N memories of yours due for refresh` when backlog is non-empty
  - Push: `memory_maintenance` emits one `refresh_due` notification per distinct owning agent, deduped against existing unacked notifications
- **Composite partial index** `idx_mi_conversation_id ON memory_items(conversation_id, created_at) WHERE is_deleted = 0` (migration v015) — replaces the plain v013 index so `conversation_id` scoped retrieval gets an index scan with ordered results. Verified with `EXPLAIN QUERY PLAN` on a synthetic 1000-row fixture.

### Changed
- **`memory_write`** — accepts `conversation_id`, `refresh_on`, `refresh_reason` parameters. All nullable; existing callers unaffected.
- **`memory_search`** — accepts `conversation_id` filter. Propagated through all recursive fallback paths (FTS → semantic, no-match → semantic, operational-error → semantic).
- **`memory_update`** — accepts `refresh_on`, `refresh_reason`, `conversation_id`. Sentinel `"clear"` sets a field to NULL; empty string means no change. Field-level audit rows written to `memory_history`.
- **`memory_maintenance`** — appends `Refresh queue: N memories due for review` to its report when the backlog is non-empty, then fans out notifications by owning agent.

### Docs
- **AGENT_INSTRUCTIONS.md** — new behavioral rule §6 "Review the Refresh Queue Periodically" with startup / long-session / breakpoint guidance; new parameters documented in `memory_write` / `memory_search` / `memory_update` tables; `memory_refresh_queue` added to retrieval table.
- **CORE_FEATURES.md** — new "Refresh Lifecycle" and "Conversation Grouping" feature sections; 25→45 MCP tool summary table (now grouped by category including Orchestration).
- **TECHNICAL_DETAILS.md** — new "Indexes on `memory_items`" table, expanded "Migrations" section covering subcommands / file naming / version tracking / backups / reversibility rules, new top-level "Refresh Lifecycle" section with data flow diagram and design rationale for reusing `memory_history` instead of a parallel soft-delete lifecycle.
- **README.md** — minimal updates (44→45 tool count in badge and summary text).

### Test Coverage
- 193/193 end-to-end tests passing (unchanged from previous entry — all new paths are additive)
- 12/12 mcp_proxy unit tests passing — `test_full_catalog_count` bumped 44→45; `test_legacy_dispatch_table_complete` confirms `memory_refresh_queue` is reachable through the proxy's legacy dispatch path; `test_inject_agent_id_on_memory_write` confirms agent_id enforcement still holds with the new `conversation_id` / `refresh_on` / `refresh_reason` parameters
- New end-to-end verification covers: conversation_id write/read roundtrip, refresh_on past/future/clear lifecycle, maintenance notification fan-out and dedup, post-ack re-notification, planner confirmation for v015 composite index

---

## [2026.4.12] — April 12, 2026 — Multi-Agent Orchestration + MCP Proxy v2

### Added
- **Orchestration primitives** — agent registry (`agent_register`, `agent_heartbeat`, `agent_list`), handoffs (`memory_handoff`), notifications (`notify`, `notifications_poll`, `notifications_ack`), and tasks (`task_create`, `task_assign`, `task_update`, `task_set_result`, `task_tree`) for multi-agent coordination
- **`m3-team` CLI** — `m3-team init|check|run` for spinning up multi-agent teams from a single YAML file
- **`examples/multi-agent-team/`** — provider-agnostic orchestrator with bounded dispatch loop (`DispatchLimits`: max_turns=8, max_tool_calls=24, max_seconds=120, provider_retries=3) and terminal `DispatchResult` taxonomy
- **`team.minimal.yaml`** — single LM Studio agent example, zero API keys required
- **`bin/mcp_tool_catalog.py`** — single source of truth for all MCP tool definitions via `ToolSpec` dataclass; 55 tools (66 with destructive enabled)
- **MCP proxy v2** (`bin/mcp_proxy.py`) — catalog-driven dispatch replacing the prior 15-tool hardcoded list; reads `X-Agent-Id` header and enforces `inject_agent_id` so client-claimed identity cannot be bypassed
- **`MCP_PROXY_ALLOW_DESTRUCTIVE`** env flag — gates 9 destructive tools (`memory_delete`, `chroma_sync`, `memory_maintenance`, `memory_set_retention`, `memory_export`, `memory_import`, `gdpr_export`, `gdpr_forget`, `agent_offline`) behind opt-in
- **`bin/test_mcp_proxy_unit.py`** — 12 in-process unit tests covering imports, tool counts, destructive filtering, dispatch, and agent_id injection

### Changed
- **License** → Apache 2.0 (from MIT) for clearer patent grant in multi-agent contexts
- **`VALID_MEMORY_TYPES`** expanded to 20 types; `bin/memory_core.py` auto-classifier kept in sync
- **MCP proxy** now sources its tool list from `mcp_tool_catalog` instead of an inline hardcoded list — adds 29 previously missing tools to proxy clients (Aider, OpenClaw)

### Fixed
- **mcp_proxy ImportError** — `LM_STUDIO_BASE` and `LM_READ_TIMEOUT` were imported from `m3_sdk` but no longer exist there; inlined as proxy-local env reads
- **Tool count gap** — proxy clients had access to only 15 of 55 catalog tools; now have full parity
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
- **MCP tool set** — memory ops, knowledge graph, conversations, lifecycle, data governance, and operations (55 catalog tools as of 2026.4.12)

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
