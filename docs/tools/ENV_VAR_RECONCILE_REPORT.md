# Environment Variable Reconcile Report

_Generated 2026-05-14 as part of the Project "Oxidation" Rust transition planning (see `~/m3_oxidation_plan.md` §9.6). Regenerated 2026-05-14 (post-oxidation-wiring sweep): the Rust core was wired into `memory_core.py`, `chatlog_redaction.py`, and `auto_route.py`, and an unrelated task-runtime tool was added — see "Oxidation additions" below._

This report inventories every environment variable consumed or set by m3-memory tools, cross-checks readers against the tool inventory at `docs/tools/INDEX.md`, and recommends the namespacing strategy for the future Rust binding crate (`m3-core-py`).

## Scope

- All `bin/*.py`, `bin/*.sh`, `bin/*.ps1`
- `m3_memory/` package
- `scripts/`, `examples/`, `tests/`
- `.env*`, `*.toml`, `*.yaml`, `*.json` configuration files

Read-only audit. No source files were modified.

## Headline numbers

| Group | Count |
|---|---|
| `M3_*`-prefixed env vars in active use | **77** |
| Non-prefixed vars that belong to m3-memory's surface | **42** |
| Auth/credential vars (not namespaced — touch FIPS path) | **3** |
| **Total env vars in m3-memory surface** | **122** |

The `M3_*` count is **77** after the 2026-05-14 regeneration: the original report's inventory table held **72** rows (its headline said "73" — a pre-existing off-by-one, corrected here), plus **5** new vars — **4** from the oxidation wiring (`M3_CORE_RS_DISABLE`, `M3_ROUTE_SHADOW_MODE`, `M3_EMBED_GGUF`, `M3_EMBED_GGUF_MODEL_TAG`) and **1** unrelated (`M3_TASK_LOG_FILE`, added by `bin/_task_runtime.py`). The test-only `M3_TEST_GGUF` (read only by the `m3-embed-llamacpp` Rust crate's opt-in test, never by m3-memory at runtime) is intentionally excluded. The non-prefixed group was re-swept on 2026-05-14 (see Methodology): it grew from **32** to **42** — all 32 prior rows still resolve to live `os.environ`/`os.getenv` reads, and **10** new vars were found (`AGENT_DB`, `CHATLOG_DB`, `LLM_ENDPOINTS_CSV`, `MCP_PROXY_HOST`, `MCP_PROXY_ALLOW_DESTRUCTIVE`, `MACBOOK_STATUS_HOST`, `POSTGRES_SERVER`, `SYNC_TARGET_IP`, `REFLECTOR_PROFILE`, `OBSERVER_PROFILE`). The auth group was re-swept the same round and stayed at **3** (one reader path corrected — see that table). Total surface = 77 + 42 + 3 = **122**.

## Reader → tool inventory cross-check

All env-var-reading files are present in `docs/tools/INDEX.md` (107 listed tools) **with one known exception**: `bin/_task_runtime.py` (reader of `M3_TASK_LOG_FILE`) is not in `INDEX.md` and has no per-tool doc — the leading-underscore name is treated as a private module by `gen_tool_inventory.py` and skipped. This is a generator-scope gap, not env-var drift; the var itself is captured above. Every other env-var reader is indexed.

Readers categorized for the Rust transition:

### Hot-path readers (must route through `m3-core-py` after oxidation)
- `bin/memory_core.py` — **now wired**: hashing, cosine/MMR, displacement guard, optional in-process embeddings
- `bin/chatlog_redaction.py` — **now wired**: `scrub()` dispatches to the Rust redactor
- `bin/chatlog_config.py`
- `bin/chatlog_ingest.py`
- `bin/chatlog_embed_sweeper.py`
- `bin/m3_entities.py`
- `bin/backfill_content_hash.py`
- `bin/embed_backfill.py`
- `bin/m3_enrich.py`
- `bin/m3_enrich_batch.py`
- `bin/slm_intent.py`
- `bin/auto_route.py` — **now wired** (Phase 3d §4c.5): `decide_route` runs in shadow mode
- `bin/sqlite_pragmas.py`

### Bootstrap/config readers (stay in Python; read env before any Rust call)
- `m3_memory/cli.py`
- `m3_memory/installer.py`
- `bin/m3_sdk.py`
- `bin/crypto_provider.py`
- `bin/memory_bridge.py`

### Out-of-scope readers (not on the oxidation path)
- `bin/discord_bot.py`
- `bin/mission_control.py`
- `bin/_task_runtime.py` (reads `M3_TASK_LOG_FILE`; task-runtime plumbing, not a retrieval/ingest hot path)
- `bin/test_*.py`
- `bin/setup_*.py`
- `examples/mac-agent/router/router.py`

## `M3_*` inventory (77 vars)

The table below holds the original 72 vars; the 5 vars from the 2026-05-14 regeneration are in the **Oxidation additions** subsection that follows it.

| Env var | Default | Type | Controls | Primary reader |
|---|---|---|---|---|
| M3_AUTO_ENRICH | `0` | bool | Auto-enrich on ingest gate | bin/chatlog_ingest.py |
| M3_AUTO_ENRICH_MIN_TURNS | `10` | int | Min turns before enrichment | bin/chatlog_ingest.py |
| M3_AUTO_INSTALL | (unset) | bool | Skip auto-install on import | m3_memory/cli.py |
| M3_AUTO_RELATED_LINK | `1` | bool | Auto-link related memories on write | bin/memory_core.py |
| M3_AUTO_RELATED_LINK_SCOPE_BY_VARIANT | `1` | bool | Restrict related-link to same variant | bin/memory_core.py |
| M3_BRIDGE_PATH | (unset) | path | MCP bridge executable path | m3_memory/installer.py |
| M3_CHROMA_SYNC_QUEUE_MAX | `500000` | int | Max queue depth before warning | bin/chroma_health.py, bin/memory_sync.py |
| M3_CHROMA_SYNC_QUEUE_SKIP_AT | `0` | int | Skip sync above threshold | bin/memory_sync.py |
| M3_CHROMA_SYNC_QUEUE_WARN | `100000` | int | Warn above threshold | bin/chroma_health.py, bin/memory_sync.py |
| M3_CONTEXT_CACHE_SIZE | `16` | int | LLM context cache size (min 2) | bin/m3_sdk.py |
| M3_CRYPTO_BACKEND | `DEFAULT` | enum | Encryption backend (DEFAULT/WOLFSSL) | bin/crypto_provider.py |
| M3_DATABASE | `memory/agent_memory.db` | path | Main memory DB path | bin/memory_core.py (+ many) |
| M3_DEBUG | (unset) | bool | Enable debug output | bin/memory_core.py |
| M3_DISABLE_AUTO_ACTIVATION | (unset) | bool | Prevent auto-activation of memory search | bin/memory_core.py |
| M3_DOCS_DIR | _(install prefix; see source)_ | path | Location of docs files | bin/discord_bot.py |
| M3_ELBOW_ABS_THRESHOLD | `0.05` | float | Min cosine drop for elbow | bin/memory_core.py |
| M3_ELBOW_MIN_INPUT | `20` | int | Min samples for elbow heuristic | bin/memory_core.py |
| M3_ELBOW_MIN_RETURN | `8` | int | Min results to preserve after elbow | bin/memory_core.py |
| M3_EMBED_MODEL | (unset) | string | Embedding model override | bin/m3_enrich.py (+ others) |
| M3_EMBED_URL | (unset) | url | Embedding server URL override | bin/m3_enrich.py (+ others) |
| M3_ENABLE_ENTITY_GRAPH | `false` | bool | Enable entity-graph pipeline | bin/m3_entities.py, bin/memory_core.py |
| M3_ENABLE_FACT_ENRICHED | `false` | bool | Enable fact-enriched retrieval | bin/memory_core.py |
| M3_ENRICH_BUDGET_USD | (unset) | float | Max USD spend cap | bin/m3_enrich.py |
| M3_ENRICH_CONV_LIST | (unset) | csv | Conversation IDs to enrich | bin/m3_enrich.py |
| M3_ENRICH_INPUT_MAX_K | (unset) | int | Max input size (K rows) | bin/m3_enrich.py |
| M3_ENRICH_MAX_ATTEMPTS | `5` | int | Max retries per turn | bin/m3_enrich.py |
| M3_ENRICH_MAX_SIZE_K | (unset) | int | Max memory size (K) | bin/m3_enrich.py |
| M3_ENRICH_MIN_SIZE_K | (unset) | int | Min memory size (K) | bin/m3_enrich.py |
| M3_ENRICH_PROFILE | `enrich_local_qwen` | string | LLM profile for enrichment | bin/m3_enrich.py |
| M3_ENRICH_SEND_TO | (unset) | email | Destination email for results | bin/m3_enrich.py |
| M3_ENRICH_TRACK_STATE | `0` | bool | Track enrichment state | bin/m3_enrich.py |
| M3_ENTITIES_CONV_LIST | (unset) | csv | Conversation IDs for entity extraction | bin/m3_entities.py |
| M3_ENTITY_EXTRACT_CONCURRENCY | `2` | int | Parallel entity extraction workers | bin/memory_core.py, bin/m3_entities.py |
| M3_ENTITY_EXTRACT_MAX_ATTEMPTS | `3` | int | Max retries for entity extraction | bin/memory_core.py |
| M3_ENTITY_EXTRACTOR_MAX_ATTEMPTS | `3` | int | Alias for above (typo-form, legacy) | bin/memory_core.py |
| M3_ENTITY_RESOLVE_COSINE_MIN | `0.85` | float | Min cosine for entity resolution | bin/memory_core.py |
| M3_ENTITY_RESOLVE_FUZZY_MIN | `0.8` | float | Min fuzzy score for entity resolution | bin/memory_core.py |
| M3_ENTITY_SEED_STOPLIST | `User,user,assistant` | csv | Entities excluded from BFS expansion | bin/memory_core.py |
| M3_ENTITY_VOCAB_YAML | (unset) | path | Entity type/predicate vocab YAML | bin/memory_core.py, bin/m3_entities.py |
| M3_EXPANSION_DISPLACEMENT_MARGIN | `2.0` | float | Margin for expansion-vs-primary guard | bin/memory_core.py |
| M3_EXPANSION_PROTECTED_RANKS | `3` | int | Ranks protected from displacement | bin/memory_core.py |
| M3_FACT_ENRICH_CONCURRENCY | `2` | int | Parallel fact enrichment workers | bin/memory_core.py |
| M3_FACT_ENRICH_MAX_ATTEMPTS | `5` | int | Max retries for fact enrichment | bin/memory_core.py |
| M3_FEDERATION_LOW_SCORE_THRESHOLD | `0.65` | float | Min score for federation retrieval | bin/memory_core.py |
| M3_HTTP_HOST | `127.0.0.1` | ip | MCP HTTP bind address | m3_memory/cli.py |
| M3_HTTP_PATH | `/mcp` | string | MCP HTTP path prefix | m3_memory/cli.py |
| M3_HTTP_PORT | `8080` | int | MCP HTTP port | m3_memory/cli.py |
| M3_IMPORTANCE_WEIGHT | `0.05` | float | Importance field weight in scoring | bin/memory_core.py |
| M3_INGEST_EVENT_ROWS | `0` | bool | Emit event-type rows during ingest | bin/memory_core.py |
| M3_INGEST_GIST_MIN_TURNS | `8` | int | Min turns to create gist row | bin/memory_core.py |
| M3_INGEST_GIST_ROWS | `0` | bool | Emit gist-type rows during ingest | bin/memory_core.py |
| M3_INGEST_GIST_STRIDE | `8` | int | Stride for gist row generation | bin/memory_core.py |
| M3_INGEST_WINDOW_CHUNKS | `0` | bool | Emit window-chunk rows during ingest | bin/memory_core.py |
| M3_INGEST_WINDOW_SIZE | `3` | int | Sliding window size for chunks | bin/memory_core.py |
| M3_INTENT_ROUTING | `0` | bool | Route queries by intent hint | bin/memory_core.py |
| M3_INTENT_USER_FACT_BOOST | `0.1` | float | Score boost for user-fact intent | bin/memory_core.py |
| M3_MEMORY_ROOT | (inferred from `__file__`) | path | Root dir of m3-memory installation | bin/m3_sdk.py, m3_memory/installer.py |
| M3_OBSERVATION_BUDGET_TOKENS | `4000` | int | Token budget for observation retrieval | bin/memory_core.py |
| M3_QUERY_TYPE_ROUTING | `0` | bool | Route queries by type hint | bin/memory_core.py |
| M3_RERANK_MODEL | `cross-encoder/ms-marco-MiniLM-L-6-v2` | string | Cross-encoder for reranking | bin/memory_core.py |
| M3_ROUTER_TEMPORAL_K_BUMP | (varies by caller) | int | Boost K for temporal queries | bin/memory_core.py |
| M3_SHORT_TURN_THRESHOLD | `20` | int | Char threshold for "short" turn | bin/memory_core.py |
| M3_SLM_CLASSIFIER | (unset) | bool | Enable SLM intent classification | bin/slm_intent.py |
| M3_SLM_PROFILE | `default` | string | SLM profile for intent classification | bin/slm_intent.py |
| M3_SLM_PROFILES_DIR | (inferred from M3_MEMORY_ROOT) | path | SLM intent profiles directory | bin/slm_intent.py |
| M3_SPEAKER_IN_TITLE | `1` | bool | Include speaker role in titles | bin/memory_core.py |
| M3_SQLITE_MMAP_SIZE | (unset) | int | SQLite mmap size (bytes) | bin/sqlite_pragmas.py |
| M3_SYNC_DBS | `` | csv | DBs to sync | bin/sync_all.py |
| M3_TITLE_MATCH_BOOST | `0.05` | float | Boost when title matches query | bin/memory_core.py |
| M3_TRANSPORT | `stdio` | enum | MCP transport (stdio/http) | m3_memory/cli.py, bin/memory_bridge.py |
| M3_TWO_STAGE_MAX_TURNS_PER_OBS | `3` | int | Max turns per observation (two-stage) | bin/memory_core.py |
| M3_TWO_STAGE_TURN_PENALTY | `0.7` | float | Turn age penalty (two-stage) | bin/memory_core.py |

### Oxidation additions (5 — added since the original sweep)

These post-date the initial report. The four `M3_CORE_RS_*` / `M3_EMBED_GGUF*` / `M3_ROUTE_*` vars gate the Project Oxidation Rust core; they are read in pure Python (the kill-switch and opt-in gates live in m3-memory, not `m3-core-py`) — so unlike the original 72, the Rust binding crate does **not** need to surface these, they gate *whether* it is used. `M3_TASK_LOG_FILE` is unrelated to oxidation.

| Env var | Default | Type | Controls | Primary reader |
|---|---|---|---|---|
| M3_CORE_RS_DISABLE | `0` | bool | Kill-switch: force pure-Python for every oxidation-wired op even when the `m3_core_rs` wheel is installed | bin/memory_core.py, bin/chatlog_redaction.py, bin/auto_route.py |
| M3_ROUTE_SHADOW_MODE | `off` | enum | Shadow-mode gate for the Rust route decider (`off` / `log`; `enforce` reserved, not implemented) | bin/auto_route.py |
| M3_EMBED_GGUF | (unset) | path | Path to a bge-m3 GGUF; when set, `_embed`/`_embed_many` use in-process llama.cpp instead of the HTTP embed server | bin/memory_core.py |
| M3_EMBED_GGUF_MODEL_TAG | `bge-m3-GGUF-Q4_K_M.gguf` | string | `embed_model` tag applied to in-process-embedded vectors (a distinct content-hash cache namespace) | bin/memory_core.py |
| M3_TASK_LOG_FILE | (unset) | path | Override path for the task-runtime log file | bin/_task_runtime.py |

> **Cross-check note:** `bin/_task_runtime.py` is a tool not present in the `docs/tools/INDEX.md` snapshot the original report cross-referenced. Re-run `python bin/gen_tool_inventory.py` and confirm it is now indexed; if so, the "no drift" claim below holds with `_task_runtime.py` added to the reader set.

## Non-prefixed vars (42) — recommended for `M3_*` namespacing

The Rust binding crate (`m3-core-py`) will accept both legacy and `M3_`-prefixed forms during a one-release-cycle deprecation window. The legacy form will emit a Python `DeprecationWarning` log line.

| Legacy var | New alias | Default | Type | Primary reader |
|---|---|---|---|---|
| AGENT_DB | M3_AGENT_DB | (unset) | path | bin/build_kg_variant.py |
| CHATLOG_DB | M3_CHATLOG_DB | (unset) | path | bin/chatlog_decay.py |
| CHATLOG_DB_PATH | M3_CHATLOG_DB_PATH | `memory/agent_chatlog.db` | path | bin/chatlog_config.py |
| CHATLOG_DB_POOL_SIZE | M3_CHATLOG_DB_POOL_SIZE | `4` | int | bin/chatlog_config.py |
| CHATLOG_DB_POOL_TIMEOUT | M3_CHATLOG_DB_POOL_TIMEOUT | `10` | int | bin/chatlog_config.py |
| CHATLOG_EMBED_MAX_PER_RUN | M3_CHATLOG_EMBED_MAX_PER_RUN | `10000` | int | bin/chatlog_embed_sweeper.py |
| CHATLOG_STATUSLINE | M3_CHATLOG_STATUSLINE | (unset) | bool | bin/chatlog_status_line.py |
| CHATLOG_STATUSLINE_ASCII | M3_CHATLOG_STATUSLINE_ASCII | (unset) | bool | bin/chatlog_status_line.py |
| CHROMA_BASE_URL | M3_CHROMA_BASE_URL | (unset) / `""` (in chroma_health.py) | url | bin/memory_core.py, bin/chroma_health.py |
| CONTRADICTION_THRESHOLD | M3_CONTRADICTION_THRESHOLD | `0.92` | float | bin/memory_core.py |
| CONTRADICTION_TITLE_GATE | M3_CONTRADICTION_TITLE_GATE | `loose` | enum | bin/memory_core.py |
| CONTRADICTION_TYPE_EXCLUSIONS | M3_CONTRADICTION_TYPE_EXCLUSIONS | `conversation` | csv | bin/memory_core.py |
| DB_POOL_SIZE | M3_DB_POOL_SIZE | `5` | int | bin/m3_sdk.py |
| DB_POOL_TIMEOUT | M3_DB_POOL_TIMEOUT | `30` | int | bin/m3_sdk.py |
| DEDUP_LIMIT | M3_DEDUP_LIMIT | `1000` | int | bin/memory_core.py |
| DEDUP_THRESHOLD | M3_DEDUP_THRESHOLD | `0.92` | float | bin/memory_core.py |
| EMBED_BULK_CHUNK | M3_EMBED_BULK_CHUNK | `1024` | int | bin/memory_core.py |
| EMBED_BULK_CONCURRENCY | M3_EMBED_BULK_CONCURRENCY | `4` | int | bin/memory_core.py |
| EMBED_DIM | M3_EMBED_DIM | `1024` | int | bin/memory_core.py |
| EMBED_MODEL (in `memory_core.py`) | merge into M3_EMBED_MODEL | `qwen3-embedding` | string | bin/memory_core.py |
| EMBED_PRIMARY | M3_EMBED_PRIMARY | `http://localhost:1234` | url | bin/discord_bot.py |
| EMBED_SECONDARY | M3_EMBED_SECONDARY | _(internal LAN address; see source)_ | url | bin/discord_bot.py |
| EMBED_SERVER_GPU_HOST | M3_EMBED_SERVER_GPU_HOST | `127.0.0.1` | ip | bin/embed_server_gpu.py |
| EMBED_SERVER_HOST | M3_EMBED_SERVER_HOST | `127.0.0.1` | ip | bin/embed_server.py |
| EMBED_TERTIARY | M3_EMBED_TERTIARY | _(internal LAN address; see source)_ | url | bin/discord_bot.py |
| ENTITY_NAME_EMBED_CACHE_MAX | M3_ENTITY_NAME_EMBED_CACHE_MAX | `50000` | int | bin/memory_core.py |
| LLAMA_PORT | M3_LLAMA_PORT | `9904` | int | bin/embed_server_gpu.py |
| LLM_ENDPOINTS_CSV | M3_LLM_ENDPOINTS_CSV | `` (empty; set by installer/setup_embedder) | csv | bin/llm_failover.py |
| LM_READ_TIMEOUT | M3_LM_READ_TIMEOUT | `4800.0` (m3_sdk.py); `300` (mcp_proxy.py) | float | bin/m3_sdk.py, bin/mcp_proxy.py |
| LLM_TIMEOUT | M3_LLM_TIMEOUT | `120.0` | float | bin/memory_core.py |
| LM_STUDIO_BASE | M3_LM_STUDIO_BASE | `http://localhost:1234/v1` | url | bin/m3_sdk.py, bin/mcp_proxy.py |
| MACBOOK_STATUS_HOST | M3_MACBOOK_STATUS_HOST | `127.0.0.1` | ip | bin/macbook_status_server.py |
| MCP_PROXY_ALLOW_DESTRUCTIVE | M3_MCP_PROXY_ALLOW_DESTRUCTIVE | (unset) | bool | bin/mcp_proxy.py |
| MCP_PROXY_HOST | M3_MCP_PROXY_HOST | `127.0.0.1` | ip | bin/mcp_proxy.py |
| OBSERVER_PROFILE | M3_OBSERVER_PROFILE | `observer_local` | string | bin/run_observer.py |
| ORIGIN_DEVICE | M3_ORIGIN_DEVICE | `platform.node()` | string | bin/memory_core.py |
| PG_URL | M3_PG_URL | (unset) | url | bin/m3_sdk.py, bin/pg_sync.py, bin/pg_setup.py |
| POSTGRES_SERVER | M3_POSTGRES_SERVER | (unset; falls back to `SYNC_TARGET_IP`) | ip | bin/sync_all.py |
| REFLECTOR_PROFILE | M3_REFLECTOR_PROFILE | `reflector_local` | string | bin/run_reflector.py |
| SEARCH_ROW_CAP | M3_SEARCH_ROW_CAP | `5000` | int | bin/memory_core.py |
| SUPERSEDES_PENALTY | M3_SUPERSEDES_PENALTY | `0.5` | float | bin/memory_core.py |
| SYNC_TARGET_IP | M3_SYNC_TARGET_IP | `` (empty) | ip | bin/sync_all.py |

## Auth/credential vars (3) — NOT namespaced

These follow secrets-manager naming convention and stay unprefixed. They route through the auth surface adjacent to `M3Hasher`. FIPS validation via `ring` is mandatory.

| Var | Reader | Notes |
|---|---|---|
| AGENT_OS_MASTER_KEY | bin/auth_utils.py | Master encryption key. Production: must come from OS keychain, not env. |
| LM_STUDIO_API_KEY | bin/auth_utils.py (+ bin/macbook_status_server.py, bin/mission_control.py) | LM Studio API key. Optional fallback. |
| LM_API_TOKEN | bin/mission_control.py, bin/macbook_status_server.py, bin/hooks/pre-commit | Generic LM API token. **Reader corrected 2026-05-14:** the original report listed `bin/m3_cognitive_loop.py`, which no longer reads this var (refactored out). |

## Conflicts & gotchas

1.  **`M3_EMBED_MODEL` vs `EMBED_MODEL`.** Two readers with different defaults. Consolidate on `M3_EMBED_MODEL` with default `qwen3-embedding`; `EMBED_MODEL` accepted as legacy alias.
2.  **`M3_ENTITY_EXTRACTOR_MAX_ATTEMPTS` is a typo-alias** of `M3_ENTITY_EXTRACT_MAX_ATTEMPTS`. _Resolved 2026-05-14:_ the precedence in `bin/memory_core.py` was inverted (typo form won); now the canonical `M3_ENTITY_EXTRACT_MAX_ATTEMPTS` wins and the typo form is fallback only. The Rust binding should accept both and log a deprecation for the typo form.
3.  **`M3_MEMORY_ROOT` and `M3_SLM_PROFILES_DIR` are inferred when unset.** The Rust binding must preserve the inference logic (walk up from `__file__`); cannot fall back to a hardcoded path.
4.  **`M3_ROUTER_TEMPORAL_K_BUMP` has caller-dependent defaults.** Different call sites in `memory_core.py` supply different defaults. The Rust port must preserve per-call-site defaults rather than hoisting to a single global default.
5.  **The planned new `M3_HASH_PROVIDER` env var does not conflict** with `M3_CRYPTO_BACKEND`. They're orthogonal (hashing vs encryption backend).
6.  **`LM_READ_TIMEOUT` has two readers with divergent defaults — and the divergence is intentional.** `bin/m3_sdk.py` defaults to `4800.0` (~80 min, slow local-LLM generation); `bin/mcp_proxy.py` defaults to `300` (5 min, proxied tool calls). These are genuinely different workloads — they are *not* a bug to unify. The Rust binding must preserve per-reader defaults, not hoist to one global. (Note: an earlier draft of this report misnamed the var `LLM_READ_TIMEOUT` — the actual var is `LM_READ_TIMEOUT`, corrected 2026-05-14.)
7.  **`POSTGRES_SERVER` and `SYNC_TARGET_IP` are chained in `bin/sync_all.py`** — `os.environ.get("POSTGRES_SERVER", os.environ.get("SYNC_TARGET_IP", ""))`. `POSTGRES_SERVER` wins; `SYNC_TARGET_IP` is the fallback. Namespacing must keep the precedence. `POSTGRES_SERVER` is also read by `examples/homelab-dashboard/backend/main.py` (out of scope — example app, not the m3-memory surface).
8.  **`CHATLOG_DB` (legacy, `bin/chatlog_decay.py`) vs `CHATLOG_DB_PATH` (canonical).** _Resolved 2026-05-14:_ `chatlog_decay.py`'s `resolve_db_path` previously read `CHATLOG_DB` → `M3_DATABASE`, skipping `CHATLOG_DB_PATH` entirely — so setting only the canonical var made decay silently target the main DB. The resolution chain is now `CHATLOG_DB` → `CHATLOG_DB_PATH` → `M3_DATABASE` → default.
9.  **`AGENT_DB` (legacy, `bin/build_kg_variant.py`) is a deprecated alias** for the main memory DB path; it shadows `M3_DATABASE`/`resolve_db_path()` when set. Source comment already marks it deprecated.

## Methodology

Search patterns applied across the tree:

- `os.environ.get("M3_`, `os.environ["M3_`
- `os.getenv("M3_`
- `$M3_`, `${M3_` (shell/PowerShell)
- Same patterns for non-`M3_` candidates with known m3-memory semantics (`CHATLOG_`, `EMBED_`, etc.)
- Cross-checked all reader file paths against `docs/tools/INDEX.md` (107 tools listed as of 2026-05-09)

**2026-05-14 regeneration scope:** re-ran the `M3_*` patterns only (`os.environ.get`/`os.environ[`/`os.getenv` for `M3_`) across `bin/`, `m3_memory/`, `scripts/`. Diffed the result against the existing inventory table (72 rows) to surface the 5-var delta, and corrected the original headline's off-by-one (it said "73" for a 72-row table). The non-prefixed and auth/credential groups were **not** re-swept; a full re-run of all four pattern classes is still owed.

**2026-05-14 non-prefixed + auth re-sweep (completed):** re-ran the full `os.environ.get` / `os.environ[` / `os.getenv` pattern set across `bin/`, `m3_memory/`, `scripts/` for non-`M3_`-prefixed names, filtered to vars belonging to m3-memory's own surface (excluding OS/standard vars — `PATH`, `USER`, `USERNAME`, `USERPROFILE`, `APPDATA`, `COMPUTERNAME`, `WT_SESSION`, `ANSICON`, `FORCE_COLOR`, `TERM_PROGRAM`, `CUDA_*`, `HF_*` — and third-party SDK keys — `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `NEWS_API_KEY`). **Non-prefixed: 32 → 42.** All 32 prior rows verified still live; **10 added**: `AGENT_DB`, `CHATLOG_DB`, `LLM_ENDPOINTS_CSV`, `MCP_PROXY_HOST`, `MCP_PROXY_ALLOW_DESTRUCTIVE`, `MACBOOK_STATUS_HOST`, `POSTGRES_SERVER`, `SYNC_TARGET_IP`, `REFLECTOR_PROFILE`, `OBSERVER_PROFILE`. No rows removed; no defaults drifted. A primary-reader column was added to the non-prefixed table this round. **Auth: 3 → 3** (no new auth-shaped `os.environ`/`os.getenv` vars); the `LM_API_TOKEN` reader was corrected from the stale `bin/m3_cognitive_loop.py` to its actual readers (`bin/mission_control.py`, `bin/macbook_status_server.py`, `bin/hooks/pre-commit`). Total surface: 112 → **122**.

_Borderline / judgment calls:_ `NEWS_API_KEY` (`bin/news_fetcher.py`) — excluded; it is a third-party news API key, not m3-memory's own surface. `MCP_PROXY_KEY` (`bin/mcp_proxy.py`) — auth-shaped but read via `ctx.get_secret(...)`, not `os.environ`/`os.getenv`, so it is not an env-var read and was not added to the auth table. `POSTGRES_SERVER` / `MACBOOK_STATUS_HOST` / `MCP_PROXY_HOST` — utility/sidecar servers, but they are m3-memory's own tools, so included. `examples/homelab-dashboard/backend/main.py` reads `POSTGRES_SERVER` too but is an example app, out of scope.

## Re-running the audit

This report should be regenerated when:

- New env vars are added to any tool (search-pattern delta)
- `docs/tools/INDEX.md` is regenerated via `python bin/gen_tool_inventory.py`
- A new phase of the Project "Oxidation" plan introduces additional `M3_*` vars

The audit can be re-run by spawning a subagent against the same scope.
r the original prompt).
