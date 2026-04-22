# M3 Memory System Changelog - 2026

## April 21, 2026

### 🔧 Database Parameter Refactor

#### Universal per-call `database` routing
- Every MCP tool (all 55 in the catalog) gained an optional `database` argument, injected at module load by `mcp_tool_catalog._inject_database_arg()`. The dispatcher pops it, resolves it via `m3_sdk.resolve_db_path()`, and wraps the impl call in `active_database(path)` — a `ContextVar` that `memory_core._db()` consults on every call. Impl signatures are unchanged.
- Resolution order: explicit arg > `M3_DATABASE` env > active `ContextVar` > default `memory/agent_memory.db`.
- `M3Context.for_db(path)` replaces `M3Context()` — per-path cached contexts, each with its own SQLite connection pool. Bounded LRU cache (default 16, override via `M3_CONTEXT_CACHE_SIZE`) evicts + closes cold pools to prevent growth on long-running servers.

#### CLI flag on every DB-aware script
- `--database PATH` wired into `bench_memory.py`, `ai_mechanic.py`, `build_kg_variant.py`, `chatlog_ingest.py`, `chatlog_embed_sweeper.py`, `cli_kb_browse.py`, `cli_knowledge.py`, `memory_doctor.py`, `migrate_memory.py`, `migrate_flat_memory.py`, `re_embed_all.py`, `secret_rotator.py`, `setup_secret.py`, `sync_all.py` (propagates via env to subprocesses), `weekly_auditor.py`, and `benchmarks/longmemeval/bench_longmemeval.py`. `ai_mechanic.py` requires `--database` explicitly (no default) since it drops tables.
- Standardized via `m3_sdk.add_database_arg(parser)` helper — one call, identical semantics.

#### New helper scripts
- `bin/setup_test_db.py` seeds a fresh schema-complete scratch DB by applying every forward migration. Run with `--database memory/_test.db --force` to bootstrap an isolated test DB, then `M3_DATABASE=memory/_test.db python bin/test_memory_bridge.py`.

#### Chatlog unification
- The three-mode (integrated / separate / hybrid) system is **removed**. The chatlog DB path now resolves via: `CHATLOG_DB_PATH` env > active `ContextVar` > `M3_DATABASE` env > `.chatlog_config.json` db_path > default `agent_chatlog.db`. Same path as main = unified-file behavior; different path = separate file with cross-DB `chatlog_promote`. `CHATLOG_MODE` env var is deprecated (warns once, then ignored). The `mode` field in `.chatlog_config.json` is ignored silently. `chatlog_status` output no longer has a `mode` field — gained `unified` bool instead.
- Async-queue routing fixed: queued chatlog items now capture their target DB path at enqueue time (`_db_path` on each item), so the flush worker groups by path and writes to the correct DB even when items from multiple `database`-routed tool calls land in a single batch. Spill-drain (`chatlog_embed_sweeper.drain_spill`) honors the captured path too.

#### NULL-semantic fixes
- `memory_write_impl` and `memory_write_bulk_impl` now coerce empty-string `variant` and `valid_to` to SQL `NULL` before INSERT. Previously the write path stored `""` while the search path filtered untagged rows with `IS NULL`, silently hiding fresh writes from the default search. Symmetrical read-side predicate widened to accept both `NULL` and `""` so legacy rows keep working.
- Migration `020_normalize_empty_to_null.up.sql` rewrites historical `""` rows to `NULL` so the inconsistency doesn't persist.

#### Test suite routing
- `test_memory_bridge.py`, `test_debug_agent.py`, `test_mcp_proxy.py` now resolve `DB_PATH` via `resolve_db_path(None)` at import time. Default behavior unchanged (env unset → live DB). To run against an isolated DB: `M3_DATABASE=memory/_test.db python bin/test_memory_bridge.py`.

#### Tool inventory generator
- `bin/gen_tool_inventory.py` now walks `benchmarks/` recursively (previously only `bin/` and `scripts/`), covers more core libraries (chatlog_config, chatlog_core, memory_maintenance, memory_sync, llm_failover), and detects the `add_database_arg(parser)` helper to synthesize the `--database` row on every script that uses it. INDEX grew from ~50 to 60 tools.

#### Docs
- New `docs/CLI_REFERENCE.md` documents every DB-aware script and the `--database` / `M3_DATABASE` precedence.
- `docs/MCP_TOOLS.md` gains a "Universal `database` parameter" section.
- `docs/CHATLOG.md` rewritten for the unified model (removed three-mode explainer).

#### ⚠️ Operational note — restart your MCP clients
- Claude Code, Gemini CLI, and other MCP clients cache tool schemas at connection time. Adding the `database` parameter to every tool schema requires a **one-time client restart** before the clients can actually pass the argument. Existing tool calls that don't set `database` continue to work unchanged.

## April 21, 2026 (late) — intent routing + SLM classifier plumbing

### 🔧 Ports from bench-wip to main (behind env gates)

#### SLM intent classifier (`bin/slm_intent.py`)
- New module with `classify_intent(query, profile=None)` and
  `extract_entities(text, profile=None)`. Gated behind `M3_SLM_CLASSIFIER`
  (off by default). Named profile loader reads YAML-per-name from
  `config/slm/`; bench harnesses can stack their own dir ahead of the
  default via `M3_SLM_PROFILES_DIR` (os.pathsep-separated list).
- Ships 4 starter profiles: `default.yaml`, `memory.yaml`, `chatlog.yaml`
  (intent triage stubs for each subsystem) and `entity_extract.yaml`
  (free-text entity pull used by `bin/augment_memory.py`). The bench
  profile intentionally lives with the harness, not in `config/slm/`.
- 11 new pytest cases under `tests/test_slm_intent.py` covering gate,
  profile loader, search-dir stacking, and label matching.
- Full reference: [docs/SLM_INTENT.md](SLM_INTENT.md) — YAML format, gate
  combinations, and walkthroughs for Ollama / LM Studio / OpenAI / bench
  harness setups.

#### Intent-aware retrieval in `memory_core`
Three related capabilities, all behind the new `M3_INTENT_ROUTING` env
gate and dormant by default:
- `intent_hint` kwarg threaded through `memory_search_scored_impl` and
  `memory_search_impl`. Auto-adds `metadata_json` + `conversation_id`
  to extra_columns when a hint is present.
- Role-biased score boost for user-authored turns when
  `intent_hint == "user-fact"` (default +0.1, overridable via
  `M3_INTENT_USER_FACT_BOOST`).
- Predecessor-turn pull: when the top hits on a user-fact query are
  assistant echoes at turn N+1, fetch turn N from the same
  conversation so the user's original statement enters the candidate
  set at 0.85x the parent score. Capped at top-10 to bound DB work.
- `_maybe_route_query` extended to honor `intent_hint ==
  "temporal-reasoning"` or `"multi-session"` for the BM25 weight shift.

#### `bin/augment_memory.py` CLI (rewrite)
Replaced the bench-wip placeholder stub with a real runnable utility.
Two subcommands:
- `link-adjacent` — create `related` edges between consecutive
  conversation turns so graph expansion bridges the gap between an
  assistant echo and the user statement behind it.
- `enrich-titles` — use `slm_intent.extract_entities` to prefix user
  turn titles with 1-3 pithy entities so BM25 hits on proper nouns
  even when body text uses pronouns. Idempotent-ish (skips titles
  that already have a ` | ` separator at the head).
- `all` runs both in sequence. Full argparse with `--database` via the
  standard helper.

#### Smaller bench-wip ports
- `bin/temporal_utils.py`: overflow guard on numeric relatives (cap
  at 100 years), `finditer` instead of `search` in two loops, new
  helpers `has_temporal_cues` and `extract_referenced_dates`.
- `bin/agent_protocol.py`: `_THINK_TAG_RE` compiled once at module
  scope and reused from `custom_tool_bridge` and `debug_agent_bridge`
  instead of the 5 separate `re.compile`/`re.search`/`re.sub` calls
  that existed before.
- `bin/m3_sdk.py`: new `M3Context.get_logger()` and `query_memory()`
  helpers from b97d2a2. Module-level `LM_STUDIO_BASE` and
  `LM_READ_TIMEOUT` constants — resolves a pre-existing broken
  import in `debug_agent_bridge` as a side effect.

#### Environment variables added
| Env | Role |
|---|---|
| `M3_SLM_CLASSIFIER` | Gate for `bin/slm_intent.py`. Off by default. |
| `M3_SLM_PROFILE` | Default profile name when caller doesn't pass `profile=`. |
| `M3_SLM_PROFILES_DIR` | `os.pathsep`-separated list of dirs searched before `config/slm/`. |
| `M3_INTENT_ROUTING` | Gate for role-boost + predecessor-pull on `memory_search_scored_impl`. Off by default. |
| `M3_INTENT_USER_FACT_BOOST` | Additive boost for user-authored turns (default 0.1). |
| `LM_STUDIO_BASE` | Local LLM base URL (default `http://localhost:1234/v1`). |
| `LM_READ_TIMEOUT` | Local LLM read timeout in seconds (default 4800.0). |

### Test status
Bridge tests 193/0/0, pytest 163/0/0 (152 prior + 11 new). Everything
ported to main is dormant until its gate is enabled, so behavior with
no env changes is byte-identical to the pre-port tree.

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
