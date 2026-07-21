# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Environment Variables & Secure Credentials


This document specifies the environment variables required by M3 Memory.
 It is essential for security and portability that **no hardcoded values (IPs, API keys, etc.)** are present in any repository files.

## 🏛️ The "Zero-Leak" Architecture Principle

```mermaid
graph TD
    subgraph "1. Highest Priority"
        ENV[Environment Variables]
    end
    subgraph "2. OS Native"
        KEY[OS Keyring / Keychain]
    end
    subgraph "3. Synchronized Vault"
        VLT[Encrypted synchronized_secrets]
    end

    ENV -->|Fallback| KEY
    KEY -->|Fallback| VLT
```

All user-specific variables MUST be loaded into your shell's environment from a secure, local-only source.
 The recommended method is to use your operating system's native secret management service:

*   **macOS**: Keychain
*   **Linux**: Secret Service API (e.g., GNOME Keyring, KeePassXC)
*   **Windows**: Credential Manager

We provide example `zshenv.example` and `zshrc.example` files in the `config/` directory. These scripts automatically detect your OS and load secrets from the appropriate backend, making them available as environment variables.

---

## 🚀 Quick Setup

1.  **Copy the examples**:
    ```bash
    cp config/zshenv.example ~/.zshenv
    cp config/zshrc.example ~/.zshrc
    ```
2.  **Edit the new files (`~/.zshenv`, `~/.zshrc`)**:
    *   Set the `M3_MEMORY_ROOT` variable to the absolute path of your `m3-memory` directory.
    *   Follow the commented-out instructions to store your secrets (API keys, IPs, etc.) in your OS's keychain for the first time.
3.  **Restart your shell** (`zsh`). The scripts will now automatically and securely load your configuration on every new terminal session.

---

## 📋 Core Environment Variables

Your `.zshenv` should define and export the following variables by calling the `get_secret` function.

### Roots & precedence (the single source of truth)

All state roots resolve through **one** implementation (`m3_core.paths`, re-exported
by `m3_sdk`). Every subsystem uses it; there is no second resolver. Precedence,
per root:

| Root | Precedence (first match wins) | Default |
|---|---|---|
| `M3_MEMORY_ROOT` | `M3_MEMORY_ROOT` env | `~/.m3-memory` |
| `M3_CONFIG_ROOT` | `M3_CONFIG_ROOT` env → `M3_MEMORY_ROOT`/config | `~/.m3/config` |
| `M3_ENGINE_ROOT` | `M3_ENGINE_ROOT` env → `M3_MEMORY_ROOT`/engine | `~/.m3/engine` |

`M3_MEMORY_ROOT` is the **master override**: set it alone and config/engine derive
from it (`<root>/config`, `<root>/engine`) unless their own env var is also set.
None are *required* — every root has a working default.

**DB path** (`M3_DATABASE`) precedence: explicit `database` tool arg → `M3_DATABASE`
env → active-database contextvar → `<engine_root>/agent_memory.db` (with a
populated-DB guard that prefers a legacy populated store over an empty stub).

**⚠️ Split-brain hazard.** The MCP **server** reads its roots from the `env` block
in the client's `settings.json` (it does **not** source your shell). The chatlog
**hooks** inherit the client *process* env. Pin the roots in **both** places
(server `env` block AND the hook `command` prefixes) or the two can resolve to
different DBs. The `session_start_capture_check` hook resolves the DB via the
canonical resolver first (so it checks the same DB the server writes), then falls
back to `M3_ENGINE_ROOT`. See `CLAUDE.md` "Homecoming Architecture".

### Universal tool controls (injected on every MCP tool)

| Variable | Default | Purpose |
|---|---|---|
| `M3_TOOL_TIMEOUT` | `30` | Default per-call timeout (seconds) for every MCP tool. Per-call override: pass `timeout` to any tool. A value `<= 0` disables the timeout (for genuinely long ops). Bounds async impls so a slow/hung call can't block the server indefinitely. |

Every tool also accepts a per-call `database` arg (route one call to a non-default
SQLite DB) and `timeout` arg — both are stripped before the impl runs.

### LangChain / LangGraph integration

| Variable | Default | Purpose |
|---|---|---|
| `M3_DEFAULT_USER_ID` | (unset) | Fallback `user_id` for the LangChain surfaces (`Memory`, `M3Store`, `M3Retriever`, `MemoryWrite`, …) so a **single-user** app need not pass `user_id=` on every call. Resolution order is **explicit arg → constructor default → `M3_DEFAULT_USER_ID` → raise**. It never weakens tenancy: when unset and no `user_id` is supplied, the surfaces still raise (there is no anonymous/global mode). Multi-tenant apps leave it unset and keep passing `user_id` per call. |

### Primary database backend

By default m3 stores everything in a local **SQLite** file — zero infrastructure,
nothing to configure. PostgreSQL as the **primary** store is opt-in. The installer
asks which backend to use (default SQLite); you can also pass
`mcp-memory install-m3 --db-backend postgres` (it reads the DSN from
`M3_PRIMARY_PG_URL`). m3 selects its backend from the **environment**, not the
config file, so these must be set wherever m3 runs (your MCP server's `env` block,
your shell, or the process that imports m3 — LangChain/SDK/CLI):

| Variable | Purpose | Example |
|---|---|---|
| `M3_DB_BACKEND` | Primary backend: `sqlite` (default) or `postgres`. | `export M3_DB_BACKEND=postgres` |
| `M3_PRIMARY_PG_URL` | Primary-store DSN when `M3_DB_BACKEND=postgres` (falls back to `M3_PG_URL`). Never reads a warehouse/CDW var. | `export M3_PRIMARY_PG_URL="postgresql://m3:PASSWORD@localhost:5432/m3_primary"` |

The schema is created automatically on first connect if the server wasn't reachable
at install time — nothing else to do once the database is up.

### Infrastructure & Connectivity

| Variable | Purpose | Example Keychain Command (macOS) |
|---|---|---|
| `M3_MEMORY_ROOT` | Optional master state-root override (see [Roots & precedence](#roots--precedence-the-single-source-of-truth)). Defaults to `~/.m3-memory`. | `export M3_MEMORY_ROOT="/path/to/state"` (Set directly) |
| `SYNC_TARGET_IP` | IP address of the central PostgreSQL server. | `_keychain_set agentos_sync_target_ip "YOUR_SERVER_IP"` |
| `PG_URL`| **Optional — deprecated.** Legacy warehouse DSN. Use `M3_CDW_PG_URL` for the data-warehouse role or `M3_PRIMARY_PG_URL` for a PostgreSQL primary store (see "Primary database backend" above). The default install is SQLite and needs no PostgreSQL at all. | `_keychain_set agentos_cdw_pg_url "postgresql://USERNAME:REPLACE_WITH_YOUR_PASSWORD@host/db"` |

### API Keys & Authentication

| Variable | Purpose | Example Keychain Command (macOS) |
|---|---|---|
| `AGENT_OS_MASTER_KEY`| **Required.** Master key for the encrypted vault. | `_keychain_set AGENT_OS_MASTER_KEY "your-secure-key"` |
| `LM_API_TOKEN` | **Required.** Token for your local LLM server (e.g., LM Studio, Ollama, vLLM). | `_keychain_set LM_API_TOKEN "your-token"` |
| `PERPLEXITY_API_KEY`| API key for Perplexity AI (web search). | `_keychain_set PERPLEXITY_API_KEY "your-ppl-key"` |
| `XAI_API_KEY`| API key for xAI/Grok (web search fallback). | `_keychain_set XAI_API_KEY "your-grok-key"` |
| `ANTHROPIC_API_KEY`| API key for Anthropic/Claude models. | `_keychain_set ANTHROPIC_API_KEY "your-claude-key"` |
| `GEMINI_API_KEY`| API key for Google/Gemini models. | `_keychain_set GEMINI_API_KEY "your-gemini-key"` |

### FIPS / Cryptography (`bin/crypto_provider.py`)

Tiered FIPS crypto — see [`FIPS_MODULE_BOUNDARY.md`](FIPS_MODULE_BOUNDARY.md).
**FIPS mode fails closed**: set these only after wolfSSL is installed
(`m3 fips install-wolfssl`), or M3 refuses to start.

| Variable | Purpose |
|---|---|
| `M3_FIPS_MODE` | `1` = route all crypto through **wolfCrypt** (hardened, fail-closed if absent). Accepts the FREE open-source wolfSSL build. |
| `M3_FIPS_STRICT` | `1` = additionally REQUIRE the **CMVP-validated** wolfCrypt FIPS module (commercial wolfSSL). Implies `M3_FIPS_MODE`. Refuses the open-source build. |
| `M3_CRYPTO_BACKEND` | `WOLFSSL` to force the wolfCrypt backend without the FIPS lockouts; `DEFAULT` (Python crypto) otherwise. (FIPS vars override this.) |
| `M3_WOLFSSL_LIB` | Explicit **absolute path** to the wolfSSL library (highest-precedence, trusted source). |
| `M3_WOLFSSL_SHA256` | Pin the expected SHA-256 of the wolfSSL library (**self-pin** your trusted build). A mismatch is fatal — detects tampering / in-place swap. `m3 doctor` prints the hash to pin. |

### MCP Proxy (`bin/mcp_proxy.py`)

The MCP proxy bridges OpenAI-compatible chat clients (Aider, OpenClaw) to the MCP tool catalog. It runs on `localhost:9000` by default.

| Variable | Purpose | Default |
|---|---|---|
| `LM_STUDIO_BASE` | Base URL of the local LLM endpoint that the proxy forwards completion requests to. | `http://localhost:1234/v1` |
| `LM_READ_TIMEOUT` | Read timeout (seconds) for upstream LLM calls. | `300` |
| `MCP_PROXY_ALLOW_DESTRUCTIVE` | When set to `1`, `true`, or `yes`, exposes the 8 destructive catalog tools (`memory_delete`, `memory_maintenance`, `memory_set_retention`, `memory_export`, `memory_import`, `gdpr_export`, `gdpr_forget`, `agent_offline`). Default hides them. | unset |

**Per-request header**: clients should send `X-Agent-Id: <agent-name>` on `/v1/chat/completions`. The proxy propagates this to the catalog dispatcher and enforces `inject_agent_id` for tools that record agent identity (`memory_write`, `agent_heartbeat`, etc.) — clients cannot spoof identity in the request body.

### Retrieval & Ranking Tuning

These knobs change how results are ranked. Defaults are safe — override only if you need to. See `bin/memory_core.py` for implementation.

| Variable | Default | Purpose |
|---|---|---|
| `M3_SPEAKER_IN_TITLE` | `1` | Prepend `[Role]` to the title at write time when `metadata.role` is a proper name (not `user`/`assistant`/`system`/`tool`). Makes speaker visible to FTS5 so queries like "what did Caroline say about X" find speaker-scoped turns. Set to `0` to disable. |
| `M3_SHORT_TURN_THRESHOLD` | `20` | Character-length threshold below which the ranker applies a length penalty (floor 0.3×). Suppresses filler turns like "ok cool" from dominating rank. |
| `M3_TITLE_MATCH_BOOST` | `0.05` | Multiplier for the title-overlap boost: if a fraction `f` of query tokens appear in the title, add `M3_TITLE_MATCH_BOOST * f` to the final score. Set to `0` to disable. |
| `M3_IMPORTANCE_WEIGHT` | `0.05` | Weight of the caller-supplied `importance` field (0.0–1.0) in final ranking. Set to `0` to ignore importance entirely. |
| `M3_INTENT_ROUTING` | `1` | Retrieval-side intent routing (role-boost + predecessor-pull). On by default; set `0` to disable. Distinct from the SLM intent classifier (`M3_SLM_CLASSIFIER`, off by default). |
| `M3_INTENT_PROCEDURAL_BOOST` | `0.20` | Additive ranking boost applied to a `procedure`-type memory when the query intent is `procedural` ("how do I X"). Gated by `M3_INTENT_ROUTING`; a non-procedural intent (or routing off) leaves ranking byte-identical. Set `0` to disable. |
| `M3_ROUTER_TEMPORAL_K_BUMP` | `5` | Extra `k` added when a query is routed as temporal (e.g. contains "when", "before", "days ago"), widening verbatim retrieval for date-sensitive questions. |
| `SUPERSEDES_PENALTY` | `0.5` | At retrieval time, an older fact that has been superseded by a newer one is demoted by this multiplier (0.5 = ranked at half score). Set to `1.0` to disable demotion. |
| `CONTRADICTION_TITLE_GATE` | `loose` | How contradiction detection decides two memories are about the same thing: `strict` (legacy — require a title substring match), `loose` (cosine + type + content-diff, default), or `off` (no title check). |
| `CONTRADICTION_TYPE_EXCLUSIONS` | `conversation` | Comma-separated memory `type`s skipped during contradiction checks. |

#### Adaptive-k elbow trim

After MMR, results can be trimmed at a score-distribution "elbow" so large pools don't return long noisy tails. Defaults are conservative.

| Variable | Default | Purpose |
|---|---|---|
| `M3_ELBOW_MIN_INPUT` | `20` | Minimum pool size before elbow trimming is considered; smaller pools are returned untrimmed. |
| `M3_ELBOW_MIN_RETURN` | `8` | Floor on how many results the elbow trim may leave — never trims below this. |
| `M3_ELBOW_ABS_THRESHOLD` | `0.05` | Minimum score drop (slope) that counts as an elbow; below this the pool is treated as flat and not trimmed. |

#### Expansion-displacement guard

When a query triggers expansion (graph hops, session expansion), this guard keeps expanded rows from displacing strong primary hits unless they clear a score margin — preventing result volatility.

| Variable | Default | Purpose |
|---|---|---|
| `M3_EXPANSION_DISPLACEMENT_MARGIN` | `2.0` | An expanded row may outrank a primary result only if its score exceeds the primary's by this multiplier. |
| `M3_EXPANSION_PROTECTED_RANKS` | `3` | The top-N primary results are protected from displacement by expanded rows entirely. |

#### Knowledge Maintenance — Confidence & Trust (opt-in)

First-class memory **confidence** (derived from provenance + corroboration) and
per-agent **trust**. All default OFF / neutral — nothing about ranking or write
behavior changes until explicitly enabled. Requires migrations 035 (confidence
columns) and 036 (trust + corroboration ledger). See
`docs/plans/KNOWLEDGE_MAINTENANCE_PLAN.md`.

| Variable | Default | Purpose |
|---|---|---|
| `M3_CONFIDENCE_RANKING` | `0` | `1` blends a memory's stored `confidence` into the retrieval score as an additive term (like `M3_IMPORTANCE_WEIGHT`). Off = ranking byte-identical to today. |
| `M3_CONFIDENCE_WEIGHT` | `0.10` | Weight of the confidence term when `M3_CONFIDENCE_RANKING=1`. |
| `M3_CONFIDENCE_MODEL` | `transparent` | Which representation drives ranking: `transparent` (the stored, user-facing aggregate) or `bayesian` (the Beta-posterior mean kept alongside, experimental). The displayed `confidence` is always the transparent value. |
| `M3_CORROBORATION` | `0` | `1` makes a near-identical re-write (high cosine + same content) corroborate the existing memory — bumping its `corroboration_count`/`confidence` and recording a ledger event — instead of creating an orphan duplicate row. |
| `CORROBORATION_THRESHOLD` | `0.95` | Cosine floor for treating a same-content write as corroboration. Higher than `CONTRADICTION_THRESHOLD` so only true near-duplicates corroborate. |
| `M3_TRUST_AUTOTUNE` | `0` | `1` lets daily maintenance nudge agent trust from observed contradiction/corroboration. Off = explicit `agent_set_trust` only. |
| `M3_CONSOLIDATION_AUTO` | `0` | `1` lets the background job run autonomous episodic→semantic belief consolidation. Off = manual/curator-triggered only. |

### Ingestion Enrichment

**On by default.** These deterministic (no-LLM) heuristics enrich `type="message"` rows written with a `conversation_id`; other writes are unaffected. They add lightweight summary/event rows that improve retrieval recall, but on chatty conversations they multiply the row count — set any of them to `0` to opt out. (Set `M3_INGEST_WINDOW_CHUNKS=0 M3_INGEST_GIST_ROWS=0 M3_INGEST_EVENT_ROWS=0` for the lean, one-row-per-turn behavior.)

| Variable | Default | Purpose |
|---|---|---|
| `M3_INGEST_WINDOW_CHUNKS` | `1` | On writes, emit a `type="summary"` row every N turns that concatenates the previous N message bodies. Captures Q&A pairs that single-turn embeds miss. Set `0` to disable. |
| `M3_INGEST_WINDOW_SIZE` | `3` | Number of consecutive turns combined into each window chunk when `M3_INGEST_WINDOW_CHUNKS=1`. |
| `M3_INGEST_GIST_ROWS` | `1` | On writes, emit a heuristic `type="summary"` gist row for the conversation once it passes the minimum-turn threshold and every stride thereafter. Deterministic; no LLM. Set `0` to disable. |
| `M3_INGEST_GIST_MIN_TURNS` | `10` | Minimum turns in a conversation before the first gist row is emitted. |
| `M3_INGEST_GIST_STRIDE` | `5` | After the first gist, emit a new one every N additional turns. |
| `M3_INGEST_EVENT_ROWS` | `1` | Regex-extract event sentences (`<ProperNoun> <verb> ... <date hint>`) from each message and emit one `type="event_extraction"` row per match, linked back via `references`. Deterministic; no LLM. Set `0` to disable. |
| `M3_QUERY_TYPE_ROUTING` | `0` | Retrieval-side: when a query matches "When/what date/which day" plus a proper noun, shift `vector_weight` to `0.3` (BM25-heavy) so the named-entity signal isn't diluted by embedding similarity. |

**Always-on:** resolved temporal anchors from `metadata.temporal_anchors` are now automatically prefixed to the embed text as `[YYYY-MM-DD] …` so vector and FTS searches can hit absolute dates even when the source text says "yesterday". No flag; free when anchors are absent.

### Files Memory — Fact Extraction (opt-in)

Controls the optional LLM fact-extraction / summarization layer of the
file-ingestion subsystem (`files.db`). Off until `M3_FILES_EXTRACT_URL` is set;
without it, ingest produces text + extractive summaries only. Full guide:
[FILES_MEMORY.md → Enabling fact extraction](FILES_MEMORY.md#enabling-fact-extraction).

| Variable | Default | Purpose |
|---|---|---|
| `M3_FILES_EXTRACT_URL` | _(unset)_ | OpenAI-compatible chat endpoint base URL, **without** `/v1` (e.g. `http://127.0.0.1:1234`). Resolution order for extraction: this → `M3_FILES_SUMMARY_URL` → `M3_LMSTUDIO_URL`. All unset = extraction unavailable. |
| `M3_FILES_EXTRACT_MODEL` | `qwen3-4b-instruct` | Model id requested for fact extraction (falls back to `M3_FILES_SUMMARY_MODEL`). |
| `M3_FILES_SUMMARY_URL` | _(unset)_ | Endpoint for the abstractive summarizer; falls back to `M3_LMSTUDIO_URL`. Also serves as a fallback for extraction (see above). |
| `M3_FILES_SUMMARY_MODEL` | `qwen3-4b-instruct` | Model id for the summarizer. |
| `M3_LMSTUDIO_URL` | _(unset)_ | Shared last-resort fallback endpoint for both extract and summary when their specific URLs are unset. |

Auth: if the endpoint enforces a key (LM Studio default), set
[`LM_API_TOKEN`](#api-keys--authentication) — it is sent as
`Authorization: Bearer <token>`. Omit for tokenless endpoints (Ollama).

### Local LLM selection

M3 does not pin a specific chat model. `bin/llm_failover.py` discovers whatever is loaded on your OpenAI-compatible endpoint(s) and picks the largest model by parameter count, filtering out embedding-only models. To minimize latency for enrichment features (auto-classify, summarization), keep a **small** instruct model (0.5B–1B) loaded alongside your embedder:

- **Ollama**: `ollama pull qwen2.5:0.5b` or `ollama pull llama3.2:1b`
- **LM Studio**: load any 0.5B–1B instruct GGUF (Q6/Q8)
- **llama.cpp**: `llama-server -m qwen2.5-0.5b-instruct-q8_0.gguf`
- **vLLM / LocalAI**: any HF-compatible small instruct model

If only the small model is loaded, `get_best_llm` picks it automatically — no env var needed. If you also load a larger generation model on the same endpoint, it will currently be preferred for every feature (per-feature routing to prefer small-for-enrichment is on the roadmap). See [QUICKSTART → Optional: load a small chat model](QUICKSTART.md#optional-load-a-small-chat-model-for-enrichment).

#### Endpoint discovery & failover

M3 only probes endpoints you opt into. Probing a provider you don't run is not free on every platform (on Windows a connect to a non-listening port can block up to the connect timeout), so each built-in local endpoint is independently toggleable — neither single-provider group pays for the other's probe. By default only **LM Studio** (`http://localhost:1234/v1`) is probed.

| Variable | Default | Effect |
|---|---|---|
| `M3_LLM_URL` | _(unset)_ | A single OpenAI-compatible `/v1` base URL for **your own server** (llama.cpp, vLLM, LocalAI, a remote box). Tried **first**. Setting it also turns off the LM Studio default probe (you've named your endpoint), so a custom-server user gets no stray `:1234` probe. Re-add LM Studio with `M3_ENABLE_LMSTUDIO_FAILOVER=1` if you want it as a fallback. |
| `M3_ENABLE_LMSTUDIO_FAILOVER` | `1` (on; `0` when `M3_LLM_URL` is set) | Probe the LM Studio endpoint (`http://localhost:1234/v1`). Set to `0` if you don't run LM Studio (e.g. **Ollama-only users**) to skip its probe. |
| `M3_ENABLE_OLLAMA_FAILOVER` | `0` (off) | Set to `1`/`true`/`yes` to also probe the Ollama endpoint (`http://localhost:11434/v1`). **Ollama users: set this.** |
| `LLM_ENDPOINTS_CSV` | _(unset)_ | Comma-separated endpoint list, probed in order. **Overrides `M3_LLM_URL` and both toggles** — full control. Use for an ordered multi-endpoint / multi-machine LAN failover — e.g. `"http://localhost:8080/v1,http://gpu-box.local:8000/v1"`. |
| `M3_LLM_CONNECT_TIMEOUT` | `0.3` | Per-endpoint connect timeout in seconds. Raise for slow remote LAN endpoints. |

Examples by runtime:
- **LM Studio** (default) — no config.
- **Ollama only** — `M3_ENABLE_LMSTUDIO_FAILOVER=0 M3_ENABLE_OLLAMA_FAILOVER=1` (or `LLM_ENDPOINTS_CSV="http://localhost:11434/v1"`).
- **llama.cpp / vLLM / LocalAI / remote** — `M3_LLM_URL="http://localhost:8080/v1"` (no LM Studio probe; add `M3_ENABLE_LMSTUDIO_FAILOVER=1` to keep it as a fallback).
- **Multiple endpoints in a specific order** — `LLM_ENDPOINTS_CSV="url1,url2,…"`.

---

## Fact Enrichment

SLM-distillation pipeline to extract atomic facts from stored memories. **On by default** — `memory_write` enqueues fact extraction unless you turn it off. It only does work when a fact-extraction SLM endpoint is reachable (set via `M3_FACT_ENRICHED_URL`/`MODEL` or the `fact_enriched.yaml` profile); with no endpoint configured the queue simply no-ops. Because it calls a local LLM per write, it adds latency and (for chatty workloads) row volume — set `M3_ENABLE_FACT_ENRICHED=0` to disable. See ARCHITECTURE.md for design overview.

| Variable | Default | Purpose |
|---|---|---|
| `M3_ENABLE_FACT_ENRICHED` | `true` | Master gate. On by default; set to `0`/`false`/`no` to disable fact extraction on writes. |
| `M3_FACT_ENRICH_CONCURRENCY` | `2` | Maximum concurrent SLM enrichment tasks. Higher values parallelize fact extraction; lower values reduce latency jitter on write paths. |
| `M3_FACT_ENRICH_MAX_ATTEMPTS` | `5` | Maximum retries for failed enrichment queue items before they are marked as poison (poisoned items remain visible in queue with `last_error` for manual inspection). |
| `M3_FACT_ENRICHED_URL` | (empty) | Override SLM endpoint URL. If unset, reads from the `fact_enriched.yaml` profile `url` field. |
| `M3_FACT_ENRICHED_MODEL` | (empty) | Override SLM model name. If unset, reads from the `fact_enriched.yaml` profile `model` field. Both URL and model must be non-empty when enrichment runs, or the extraction fails with a clear error. |

---

## Procedure Distillation

Autonomous pipeline that rolls up successful (completed) task runs — a task plus its step/result memories — into reusable `procedure` memories (skill / runbook / how-to / checklist), linked back to their sources via `distills_from` edges (sources are **preserved**, never deleted). The engine is `memory_distill_procedures_impl`; `bin/distill_procedures.py` is the trigger, and the cognitive loop runs it event-driven + governor-gated. The distillation model is **local-first, cloud-capable**. See ARCHITECTURE.md / EXTENDING.md for design overview.

| Variable | Default | Purpose |
|---|---|---|
| `M3_DISTILL_AUTO` | `0` | Hard gate for autonomous procedure **writes**. Distillation runs **dry-run** unless `--apply` AND `M3_DISTILL_AUTO=1` — so a scheduled/loop invocation is a safe no-op until you opt in. |
| `M3_DISTILL_MODEL` | `slm` | Distillation model selector: unset/`slm` → the local `procedure_local` SLM profile (sovereign default); `llm` → the largest local model via `get_best_llm` failover; any other value → a profile name (another local model, or a cloud endpoint via a `backend: anthropic\|openai` profile — cloud is config, not new code). |

---

## Entity-Relation Graph

SLM-extraction pipeline to build a typed knowledge graph of entities and relationships from stored memories. **On by default** — writes are queued for entity extraction unless you turn it off. Like fact enrichment, it only does work when an extraction SLM endpoint is reachable (`M3_ENTITY_GRAPH_URL`/`MODEL` or the `entity_graph.yaml` profile); with no endpoint the queue no-ops. It calls a local LLM per write, so it adds latency — set `M3_ENABLE_ENTITY_GRAPH=0` to disable. The entity-type and predicate vocabulary is user-configurable via `M3_ENTITY_VOCAB_YAML` (see below). See ARCHITECTURE.md for design overview.

| Variable | Default | Purpose |
|---|---|---|
| `M3_ENABLE_ENTITY_GRAPH` | `true` | Master gate. On by default; set to `0`/`false`/`no` to disable entity extraction on writes. |
| `M3_ENTITY_VOCAB_YAML` | (`config/lists/entity_graph_default.yaml`) | Path to the entity-type + predicate vocabulary profile. Swap or author your own to retune the graph schema for your domain — no code changes. The stock vocabulary defines a 7-type / 34-predicate schema spanning general, human-life, and technical domains. |
| `M3_ENTITY_EXTRACT_CONCURRENCY` | `2` | Maximum concurrent SLM extraction tasks. Mirrors fact_enriched concurrency tuning. |
| `M3_ENTITY_EXTRACT_MAX_ATTEMPTS` | `5` | Queue retry cap before poisoned-item exclusion. Failed items remain in extraction queue with `last_error` for manual inspection. |
| `M3_ENTITY_RESOLVE_FUZZY_MIN` | `0.8` | Minimum token-Jaccard similarity score for fuzzy-match resolution tier. Entities with canonical names matching above this threshold within the same type are merged. |
| `M3_ENTITY_RESOLVE_COSINE_MIN` | `0.85` | Minimum embedding cosine similarity for cosine-match resolution tier (final fallback before creating a new entity). |
| `M3_ENTITY_GRAPH_URL` | (empty) | Override SLM endpoint URL. If unset, reads from the `entity_graph.yaml` profile `url` field. |
| `M3_ENTITY_GRAPH_MODEL` | (empty) | Override SLM model name. If unset, reads from the `entity_graph.yaml` profile `model` field. Both URL and model must be non-empty when extraction runs, or the process fails with a clear error. |

---

## Project Oxidation — Rust Core (`m3_core_rs`)

Optional Rust compute core ([`m3-core-rs`](https://github.com/skynetcmd/m3-core-rs)). Prebuilt wheels are published per platform — `m3 setup` / `m3 embedder install-gpu` install the matching one automatically (CPU/Vulkan/Metal from PyPI under the platform-suffixed names like `m3-core-rs-linux-cpu`; CUDA from the GitHub Release — see [CUDA_INSTALL.md](CUDA_INSTALL.md)). You only build from source when no prebuilt wheel matches your platform + Python version ([BUILD_WHEELS.md](BUILD_WHEELS.md)). When the `m3_core_rs` wheel is importable, hot-path operations — SHA-256 hashing, cosine / batch-cosine, MMR reranking, the expansion-displacement guard, chat-log redaction, and pre-retrieval query routing — route through Rust. 

**By default, when `m3_core_rs` is importable, all Rust integrations are active out-of-the-box.** Every pathway falls back gracefully and silently to the pure-Python implementation when the wheel is absent. Users can explicitly opt out of any Rust-accelerated hot paths by setting the escape-hatch environment variables described below.

| Variable | Default | Purpose |
|---|---|---|
| `M3_CORE_RS_DISABLE` | `0` | Kill-switch. Set to `1`/`true`/`yes` to disable the Rust core completely and force the pure-Python path for **every** oxidation-wired operation even when the `m3_core_rs` wheel is installed. |
| `M3_ROUTE_SHADOW_MODE` | `enforce` (if wheel loaded, otherwise `off`) | Configuration gate for the accelerated Rust route decider in `bin/auto_route.py`. `enforce` (default when Rust core importable) routes queries instantly via pre-retrieval Rust classification and maps branch names via a conceptual shim translation (delivers **30-40x routing speedup**). `log` runs shadow-mode drift comparison logging. `off` disables the Rust path and runs Python post-retrieval routing. |
| `M3_GOVERNOR_INITIAL_THRESHOLD` | `85` | Host-load percentage (clamped to 10–99) at which the [Adaptive Background Workload Governor](M3V3_OXIDATION.md) enters `THROTTLED` mode — background maintenance (dedup, PG sync, embed backfill, cognitive loops) runs with a long inter-unit delay so it never competes with foreground work. Load is the max of CPU/RAM/GPU utilization. **Live override:** set `initial_threshold` in `<config_root>/.governor_config.json` — the file is re-read every few seconds (no restart) and takes precedence over this env var. The config file is the cross-platform knob; headless launchers (Windows task / macOS launchd / Linux systemd) do not reliably inherit shell env. |
| `M3_GOVERNOR_LIMIT_THRESHOLD` | `95` | Host-load percentage (clamped to 20–100) at which the governor enters `HALTED` mode — background work stops and interactive work is delayed to prevent a system freeze. Set to `100` to disable the critical/HALTED-on-load tier entirely. If `INITIAL ≥ LIMIT` (and LIMIT ≠ 100) the initial threshold is auto-set to `LIMIT − 5`. The governor pacing ladder is computed natively (`m3_core_rs.Governor`) with an identical pure-Python fallback; both honor these vars. **Live override:** `limit_threshold` in `<config_root>/.governor_config.json` (see above). For a UX-first, interactive-priority host, `{"initial_threshold": 40, "limit_threshold": 75}` keeps GPU/CPU headroom for foreground work. |
| `M3_GOVERNOR_CFG_TTL` | `5.0` | Seconds between `.governor_config.json` mtime checks. The file is only re-opened/parsed when its mtime changes — an unchanged file costs one `os.stat` per interval, not a full read+parse. Lower = faster pickup of edits; higher = fewer stat calls. The file is auto-seeded with the current defaults at `m3` schedule install and at cognitive-loop startup if absent (idempotent; never clobbers your edits), so the tuning knob always exists. |
| `M3_GOVERNOR_THROTTLED_LIMIT` | `1` | Per-pass item ceiling the cognitive loop uses while `THROTTLED`. Default `1` sends a single item to the LLM, then returns to the top of the loop and re-probes load before the next — the most conservative, interactive-first cadence — instead of charging through a full `--limit-per-pass` (50) batch with no re-check. |
| `M3_GPU_PROBE_DISABLE` | _(unset)_ | Set `1`/`true` to skip the GPU-utilization probe (e.g. CPU-only hosts). When disabled, GPU load reports `0` and the governor reacts to CPU/RAM only. The probe is **multi-backend** and auto-detects across configs: CUDA via `nvidia-smi` (any OS); Windows AMD/Intel/Vulkan via the `\GPU Engine(*)\Utilization Percentage` perf counter; Apple-Silicon Metal via `ioreg` IOAccelerator; Linux AMD via `/sys/.../gpu_busy_percent`. The first backend that answers is pinned; if none answer (CPU-only) it settles to `0` and trips off after a few misses. |
| `M3_GPU_PROBE_TTL` | `2.0` | Seconds the GPU-utilization probe result is cached (a probe spawns a short subprocess). |
| `M3_EMBED_GGUF` | (empty → **auto-detected**) | Path to a bge-m3 GGUF file. When set (and `m3_core_rs` is built with the `embedded` feature), `_embed` / `_embed_many` produce embeddings **in-process via llama.cpp** (tier-1, ~10–85× faster) instead of POSTing to a llama-server. **When unset, tier-1 now auto-detects a bge-m3 GGUF** in the canonical model dirs (see `M3_EMBED_GGUF_AUTODETECT`) rather than silently skipping to HTTP. Guarded: a GGUF whose embedding dimension ≠ `EMBED_DIM` is rejected and HTTP is used. |
| `M3_EMBED_GGUF_AUTODETECT` | `1` | When `M3_EMBED_GGUF` is unset, search the canonical model dirs (`~/.lmstudio/models`, `~/Library/Application Support/LM Studio/models`, `~/.cache/lm-studio/models`, `~/.cache/m3/models`, `~/.m3-memory/_assets/embedder`, `~/models`) for a `*bge[-_]m3*.gguf` and use it for tier-1. Set `0` to disable (keeps the pre-auto-detect behavior: no GGUF env ⇒ HTTP). The walk is depth-bounded (~4) and first-match. |
| `M3_EMBED_GGUF_WALK_BUDGET` | `2.0` | Wall-clock budget (seconds) for the auto-detect filesystem walk. If a pathological models directory can't be searched within this budget, auto-detect gives up and tier-1 falls back to HTTP — cold start is never stalled. |
| `M3_EMBED_GGUF_MODEL_TAG` | `bge-m3-GGUF-Q4_K_M.gguf` | The `embed_model` tag applied to vectors produced by the in-process path (above). Defaults to the llama.cpp-served bge-m3 tag the embedded backend is parity-verified against (cosine ≈ 0.996 vs stored rows with that tag). This is a distinct content-hash cache namespace from LM Studio's `text-embedding-bge-m3` rows. |
| `M3_EMBED_FALLBACK_URL` | `http://127.0.0.1:8082` | URL of the CPU HTTP fallback embed server (m3-embed-server). When `M3_EMBED_GGUF` is set but the in-process `EmbeddedEmbedder` fails to construct (GGUF missing, CUDA OOM, wheel built without `--features embedded`) or raises mid-call, `_embed` / `_embed_many` POST to `{this URL}/embedding` (singular path) before falling through to `M3_EMBED_URL`. The fallback must serve bge-m3 (or a model with matching `EMBED_DIM`) to remain vector-compatible with rows tagged `M3_EMBED_GGUF_MODEL_TAG`. Vectors produced via this fallback are tagged with `M3_EMBED_GGUF_MODEL_TAG`, sharing the in-process cache namespace. |

### Observable backend selection

`bin/memory_core.py` exposes process-global counters so callers can see which
embed path actually served each call:

- `get_embed_backend_stats() -> dict[str, int]` — snapshot of served-call
  counts keyed by label: `'cuda-inprocess'`, `'vulkan-inprocess'`,
  `'metal-inprocess'`, `'cpu-inprocess'`, `'cpu-http-fallback'`,
  `'http-primary'`. The dict is a copy; mutate freely.
- `reset_embed_backend_stats()` — clear the counters between phases (handy
  in benchmarks that want to attribute embeds to a particular query workload).

Both helpers are thread-safe. `_embed()` increments by 1; `_embed_many()`
increments by the number of inputs served along that path.
| `M3_TEST_GGUF` | (empty) | Test-only. Points the `m3-embed-llamacpp` crate's opt-in real-inference test at a GGUF model. Unset → that test is skipped. Not read by m3-memory at runtime. |

> **Note — the `M3_MMR_SHADOW` var has been retired.** An earlier build added a shadow-mode flag for the MMR reranker; the Rust MMR (`mmr_rerank_scored`) is now authoritative when `m3_core_rs` is loaded (it replicates the Python loop's selection sequence exactly, verified by `tests/test_oxidation_parity.py`). No env var gates it — `M3_CORE_RS_DISABLE` is the only override.
