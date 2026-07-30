# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Environment Variables & Secure Credentials


This document specifies the environment variables required by M3 Memory.
 It is essential for security and portability that **no hardcoded values (IPs, API keys, etc.)** are present in any repository files.

## `M3_` namespacing (backward-compatible migration)

All m3-owned environment variables are being namespaced under an **`M3_` prefix**
to avoid collisions with other tools (a bare `EMBED_MODEL` or `DEDUP_THRESHOLD`
could clash). The migration is **backward-compatible**: each variable is resolved
new-name-first, then the old bare name, then the default. If only the deprecated
bare name is set, m3 uses it **and prints a one-time warning** naming the `M3_`
replacement. Old names keep working for at least one release, then are removed.

- To migrate: rename `X` → `M3_X` in your shell rc / secret store (e.g.
  `DEDUP_THRESHOLD` → `M3_DEDUP_THRESHOLD`, `OBSERVER_PROFILE` → `M3_OBSERVER_PROFILE`).
- `m3 doctor` lists every deprecated bare name currently in use.
- **Not renamed** (external conventions, keep as-is): third-party API keys
  (`ANTHROPIC_API_KEY`, `LM_API_TOKEN`, `GH_TOKEN`, …), `AGENT_OS_MASTER_KEY`,
  and OS/system vars (`APPDATA`, `CUDA_PATH`, `NO_COLOR`, …).

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

### Database pooling & pragmas

Low-level knobs for the local SQLite/primary store: explicit DB paths, the
connection pool, and startup behavior. All have working defaults — override only
for a non-standard layout or to debug connection issues.

| Variable | Default | Purpose |
|---|---|---|
| `M3_DB_PATH` | _(resolver default)_ | Explicit path to the main memory DB, used by the session-start capture check when it can't resolve via the canonical roots. |
| `M3_MEMORY_DB` | _(resolver default)_ | Explicit memory-DB path as seen by the files-memory view (`files_memory.config`). |
| `M3_DB_POOL_SIZE` | `5` | Size of the main SQLite connection pool. |
| `M3_DB_POOL_TIMEOUT` | `30` | Seconds a caller waits to check out a pooled connection before erroring. |
| `M3_CONTEXT_CACHE_SIZE` | — | LRU capacity for cached per-database contexts in `m3_core.context`. |
| `M3_SQLITE_MMAP_SIZE` | — | Value for the SQLite `mmap_size` pragma (bytes). Larger values memory-map more of the DB for read-heavy workloads. |
| `M3_SKIP_MIGRATIONS` | _(unset)_ | Set to skip running schema migrations on startup (e.g. a read-only or already-migrated DB). |
| `M3_DISABLE_AUTO_ACTIVATION` | _(unset)_ | Set to skip the auto-activation step that runs when a DB connection is opened. |
| `M3_DEBUG` | _(unset)_ | Set to enable verbose DB/debug logging. |

### Chatlog subsystem

Configuration for the chatlog capture store (`agent_chatlog.db`) and its embed
sweeper — the hook-driven turn-capture pipeline. Paths default to the resolved
engine root; the sweeper knobs bound background embedding cost.

| Variable | Default | Purpose |
|---|---|---|
| `M3_CHATLOG_DB` | _(engine root)_ | Path to the chatlog SQLite DB. *(legacy alias: `CHATLOG_DB`, still honored via `getenv_compat`.)* |
| `M3_CHATLOG_DB_PATH` | _(engine root)_ | Alternate chatlog DB path used by the ingest path. |
| `M3_CHATLOG_DB_POOL_SIZE` | `4` | Size of the chatlog connection pool. |
| `M3_CHATLOG_DB_POOL_TIMEOUT` | `10` | Seconds to wait for a chatlog pool checkout before erroring. |
| `M3_CHATLOG_EMBED_DEADLINE_S` | `60` | Wall-clock budget for one embed-sweep run; the sweep stops cleanly when exceeded. |
| `M3_CHATLOG_EMBED_MAX_PER_RUN` | `10000` | Maximum rows embedded in a single sweep run. |
| `M3_CHATLOG_STATUSLINE` | _(on)_ | Set to `off` to disable the chatlog capture indicator in the status line. |
| `M3_CHATLOG_STATUSLINE_ASCII` | _(unset)_ | Set to `1` to render the indicator as ASCII (`[!]`) instead of the `⚠` glyph (for terminals without emoji support). |

### Infrastructure & Connectivity

| Variable | Purpose | Example Keychain Command (macOS) |
|---|---|---|
| `M3_MEMORY_ROOT` | Optional master state-root override (see [Roots & precedence](#roots--precedence-the-single-source-of-truth)). Defaults to `~/.m3-memory`. | `export M3_MEMORY_ROOT="/path/to/state"` (Set directly) |
| `M3_SYNC_TARGET_IP` | IP address of the central PostgreSQL server (fallback sync target). *(legacy alias: `SYNC_TARGET_IP`, still honored via `getenv_compat`.)* | `_keychain_set agentos_sync_target_ip "YOUR_SERVER_IP"` |
| `PG_URL`| **Optional — deprecated.** Legacy warehouse DSN. Use `M3_CDW_PG_URL` for the data-warehouse role or `M3_PRIMARY_PG_URL` for a PostgreSQL primary store (see "Primary database backend" above). The default install is SQLite and needs no PostgreSQL at all. | `_keychain_set agentos_cdw_pg_url "postgresql://USERNAME:REPLACE_WITH_YOUR_PASSWORD@host/db"` |

### Postgres & sync

Knobs for the PostgreSQL backend pool and the cross-machine sync job
(`bin/sync_all.py`). Only relevant when you run a PostgreSQL primary/warehouse or
the LAN sync workflow; the default SQLite install ignores all of these.

| Variable | Default | Purpose |
|---|---|---|
| `M3_PG_FORBIDDEN_HOSTS` | — | Comma-separated blocklist of PostgreSQL hosts the backend refuses to connect to (guardrail against accidentally targeting a protected server). |
| `M3_PG_POOL_MIN` | — | Minimum size of the PostgreSQL connection pool. |
| `M3_PG_POOL_MAX` | — | Maximum size of the PostgreSQL connection pool. |
| `M3_PG_SYNC_TIMEOUT` | — | Timeout (seconds) for a PostgreSQL sync operation. |
| `M3_POSTGRES_SERVER` | — | Target PostgreSQL host for the sync job. *(legacy alias: `POSTGRES_SERVER`, still honored via `getenv_compat`.)* Falls back to [`M3_SYNC_TARGET_IP`](#infrastructure--connectivity). |
| `M3_SYNC_DBS` | — | Which databases the sync job should replicate (selector/list). |

### API Keys & Authentication

`AGENT_OS_MASTER_KEY` is m3's own vault key; the third-party service keys below
have their own section.

| Variable | Purpose | Example Keychain Command (macOS) |
|---|---|---|
| `AGENT_OS_MASTER_KEY`| **Required.** Master key for the encrypted vault. | `_keychain_set AGENT_OS_MASTER_KEY "your-secure-key"` |

### Third-party service credentials & endpoints

Keys and endpoints for **external services** m3 can talk to. These follow each
vendor's own naming convention (they are **not** `M3_*`-namespaced by design — a
tool that already exports `ANTHROPIC_API_KEY` should just work). All are
**optional except `LM_API_TOKEN`**; a feature that needs an unset key degrades or
is skipped, never a hard failure of the core. Store them in your OS keychain, not
in shell rc files (see the Zero-Leak principle above).

| Variable | Service / used by | Required? |
|---|---|---|
| `LM_API_TOKEN` | Token for your local LLM server (LM Studio, Ollama, llama-server, vLLM) — the primary inference endpoint. | **Required** |
| `LM_STUDIO_API_KEY` | Alternate token some LM Studio setups expect; read by `auth_utils` as a fallback for the local LLM token. | Optional |
| `ANTHROPIC_API_KEY` | Anthropic / Claude models (e.g. via the MCP proxy's model routing). | Optional |
| `GEMINI_API_KEY` | Google / Gemini models. | Optional |
| `OPENAI_API_KEY` | OpenAI-compatible cloud models (MCP proxy routing / cloud fallback). | Optional |
| `PERPLEXITY_API_KEY` | Perplexity AI — web search. | Optional |
| `XAI_API_KEY` | xAI / Grok — web-search fallback. | Optional |
| `NEWS_API_KEY` | NewsAPI — the news-fetch tool. | Optional |

> GitHub tokens (`GITHUB_TOKEN` / `GH_TOKEN`) are **not** a runtime service key —
> m3 uses GitHub only for **development & deployment** (docs/badge generation, CI,
> releases), never at runtime. See [Development & repo tooling](#development--repo-tooling).

### Identity, agent & auth

Who a running m3 process is (user + agent identity), device-origin material for
the auth layer, and tenancy isolation. Most resolve from install-time config;
override only for multi-agent / multi-device setups.

| Variable | Default | Purpose |
|---|---|---|
| `M3_USER_ID` | _(config)_ | Owning user identity for the process (Hermes identity layer). |
| `M3_AGENT_ID` | _(config)_ | Agent identity for the process (Hermes identity layer). |
| `M3_AGENT_DB` | _(resolver default)_ | Per-agent DB path used when building a KG variant. *(legacy alias: `AGENT_DB`, still honored via `getenv_compat`.)* |
| `M3_AGENT_OS_SALT_HEX` | _(generated)_ | Hex-encoded salt for agent-OS auth derivation. |
| `M3_ORIGIN_DEVICE` | _(host)_ | Origin-device identifier stamped by the auth layer. |
| `M3_ENFORCE_AGENT_ISOLATION` | — | When enabled, enforces strict per-agent data isolation so one agent cannot read another's memories. |
| `M3_MACBOOK_STATUS_HOST` | — | Host/bind address for the MacBook status server. |

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
| `M3_LM_READ_TIMEOUT` | Read timeout (seconds) for upstream LLM calls (long: ~80 min, sized for 32k-token generations). *(legacy alias: `LM_READ_TIMEOUT`, still honored via `getenv_compat`.)* | `4800` |
| `M3_MCP_PROXY_ALLOW_DESTRUCTIVE` | When set to `1`, `true`, or `yes`, exposes the 8 destructive catalog tools (`memory_delete`, `memory_maintenance`, `memory_set_retention`, `memory_export`, `memory_import`, `gdpr_export`, `gdpr_forget`, `agent_offline`). Default hides them. *(legacy alias: `MCP_PROXY_ALLOW_DESTRUCTIVE`, still honored via `getenv_compat`.)* | unset |

**Per-request header**: clients should send `X-Agent-Id: <agent-name>` on `/v1/chat/completions`. The proxy propagates this to the catalog dispatcher and enforces `inject_agent_id` for tools that record agent identity (`memory_write`, `agent_heartbeat`, etc.) — clients cannot spoof identity in the request body.

### MCP bridge & transport

Bind addresses, transport mode, and payload paths for the memory MCP bridge
(`bin/memory_bridge.py`) and the MCP proxy host. Defaults suit a local stdio
server; set these to run the bridge over HTTP or bind to a specific interface.

| Variable | Default | Purpose |
|---|---|---|
| `M3_TRANSPORT` | `stdio` | Bridge transport mode: `stdio` (default) or `http`. |
| `M3_HTTP_HOST` | — | Bind host for the bridge when `M3_TRANSPORT=http`. |
| `M3_HTTP_PORT` | — | Bind port for the bridge when `M3_TRANSPORT=http`. |
| `M3_HTTP_PATH` | — | URL path the HTTP bridge serves the MCP endpoint on. |
| `M3_MCP_PROXY_HOST` | — | Bind host for the MCP proxy (see [MCP Proxy](#mcp-proxy-binmcp_proxypy)). |
| `M3_TOOLS_LAZY` | — | When set, defer loading tool implementations until first use (faster bridge startup). |
| `M3_PATH_BIN` | _(payload)_ | Path to the m3 `bin/` directory the bridge dispatches to. |
| `M3_BRIDGE_PATH` | _(installer)_ | Path to the bridge entry-point script recorded by the installer. |

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
| `M3_SUPERSEDES_PENALTY` | `0.5` | At retrieval time, an older fact that has been superseded by a newer one is demoted by this multiplier (0.5 = ranked at half score). Set to `1.0` to disable demotion. *(legacy alias: `SUPERSEDES_PENALTY`, still honored via `getenv_compat`.)* |
| `M3_CONTRADICTION_THRESHOLD` | `0.92` | Cosine floor above which a differing same-type memory is superseded on write. Deliberately conservative: it fires on near-restatements of the same claim, so two facts that are topically related but genuinely different (~0.74, say) are **both kept**. Use `memory_supersede` to close a fact explicitly; lower this only after measuring your own corpus. *(legacy alias: `CONTRADICTION_THRESHOLD`, still honored via `getenv_compat`.)* |
| `M3_CONTRADICTION_TITLE_GATE` | `loose` | How contradiction detection decides two memories are about the same thing: `strict` (legacy — require a title substring match), `loose` (cosine + type + content-diff, default), or `off` (no title check). *(legacy alias: `CONTRADICTION_TITLE_GATE`, still honored via `getenv_compat`.)* |
| `M3_CONTRADICTION_TYPE_EXCLUSIONS` | `conversation` | Comma-separated memory `type`s skipped entirely during contradiction detection. *(legacy alias: `CONTRADICTION_TYPE_EXCLUSIONS`, still honored via `getenv_compat`.)* |
| `M3_SEARCH_ROW_CAP` | `5000` | Hard cap on the number of candidate rows pulled from the store per search before ranking, bounding worst-case query cost. *(legacy alias: `SEARCH_ROW_CAP`, still honored via `getenv_compat`.)* |
| `M3_DEDUP_LIMIT` | `1000` | Maximum candidate rows considered by the retrieval-time near-duplicate collapse. |
| `M3_OBSERVATION_BUDGET_TOKENS` | — | Token budget for the two-stage observation expansion; caps how much surrounding turn text is pulled into an observation. |
| `M3_TWO_STAGE_MAX_TURNS_PER_OBS` | — | Ceiling on how many turns a single observation may expand to in two-stage retrieval. |
| `M3_TWO_STAGE_TURN_PENALTY` | — | Per-turn rank penalty applied as an observation expands, so long expansions decay in score. |
| `M3_INTENT_USER_FACT_BOOST` | — | Additive ranking boost for `user`-scoped fact memories when intent routing classifies the query as fact-seeking. |
| `M3_BYPASS_SURFACE_CAP` | — | Set to bypass the per-entity surface cap, letting all matching entity surfaces through (debug / exhaustive-recall). |
| `M3_AUTO_RELATED_LINK` | — | When enabled, automatically create `related` edges between newly written memories and their nearest neighbors. |
| `M3_AUTO_RELATED_LINK_SCOPE_BY_VARIANT` | — | Restrict auto-related linking to memories sharing the same variant/scope, preventing cross-scope link bleed. |

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
| `M3_CORROBORATION_THRESHOLD` | `0.95` | Cosine floor for treating a same-content write as corroboration. Higher than `M3_CONTRADICTION_THRESHOLD` so only true near-duplicates corroborate. *(legacy alias: `CORROBORATION_THRESHOLD`, still honored via `getenv_compat`.)* |
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

### Files Memory — ingestion, dedup & promotion

The rest of the file-ingestion subsystem's tunables (`files_memory/*`): corpus
selection and defaults, crawl/size limits, extraction concurrency, near-duplicate
detection, and promotion of file-derived facts into long-term memory. All have
working defaults.

| Variable | Default | Purpose |
|---|---|---|
| `M3_FILES_CORPUS` | _(default corpus)_ | Active corpus name for a files operation. |
| `M3_FILES_DEFAULT_CORPUS` | — | Default corpus used when none is specified. |
| `M3_FILES_DEFAULT_SCOPE` | — | Default scope applied to ingested files. |
| `M3_FILES_DEFAULT_EXTRACT_MODE` | — | Default extraction mode for new files (e.g. text vs. fact extraction). |
| `M3_FILES_DB_PATH` | _(engine root)_ | Explicit path to the files-memory DB (`files.db`). |
| `M3_FILES_FOLLOW_SYMLINKS` | — | When enabled, the crawler follows symlinks during ingestion. |
| `M3_FILES_MAX_FILE_BYTES` | — | Maximum size (bytes) of a single file the ingester will read. |
| `M3_FILES_MAX_FILES_PER_INGEST` | — | Cap on files processed in one ingest run. |
| `M3_FILES_MAX_LEAF_TOKENS` | — | Maximum tokens per leaf chunk when splitting a file. |
| `M3_FILES_EXTRACT_CONCURRENCY` | — | Maximum concurrent extraction tasks for file ingestion. |
| `M3_FILES_EXTRACT_MAX_ATTEMPTS` | — | Retry cap for a failed file-extraction item before it is poisoned. |
| `M3_FILES_EXTRACT_MIN_LEAF_CHARS` | — | Minimum leaf-chunk character count below which extraction is skipped. |
| `M3_FILES_DEDUP_THRESHOLD` | — | Cosine threshold above which two file leaves are treated as duplicates. |
| `M3_FILES_DEDUP_MAX_PAIRS` | — | Maximum candidate pairs the dedup pass compares. |
| `M3_FILES_DEDUP_LEAF_LIMIT` | — | Cap on leaves considered in a single dedup pass. |
| `M3_FILES_PROMOTION_SCOPE` | — | Scope assigned to file-derived facts when promoted to long-term memory. |
| `M3_FILES_PROMO_HALF_LIFE_DAYS` | — | Half-life (days) for the recency term in the promotability score. |
| `M3_FILES_PROMO_SUGGEST_THRESHOLD` | — | Promotability score above which a file fact is suggested for promotion. |
| `M3_FILES_PREWARM_TIMEOUT_S` | — | Timeout (seconds) for the summarizer prewarm step. |

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
| `M3_LLM_ENDPOINTS_CSV` | _(unset)_ | Comma-separated endpoint list, probed in order. **Overrides `M3_LLM_URL` and both toggles** — full control. Use for an ordered multi-endpoint / multi-machine LAN failover — e.g. `"http://localhost:8080/v1,http://gpu-box.local:8000/v1"`. *(legacy alias: `LLM_ENDPOINTS_CSV`, still honored via `getenv_compat`.)* |
| `M3_LLM_CONNECT_TIMEOUT` | `0.3` | Per-endpoint connect timeout in seconds. Raise for slow remote LAN endpoints. |
| `M3_LM_MODEL` | _(unset)_ | Explicit local-LM chat model id for pipelines that request a named model (e.g. promote/distill), instead of auto-selecting the loaded model. |
| `M3_LLM_TIMEOUT` | `45` | Generic per-request timeout (seconds) for local LLM chat calls that don't have their own timeout knob. |

Examples by runtime:
- **LM Studio** (default) — no config.
- **Ollama only** — `M3_ENABLE_LMSTUDIO_FAILOVER=0 M3_ENABLE_OLLAMA_FAILOVER=1` (or `LLM_ENDPOINTS_CSV="http://localhost:11434/v1"`).
- **llama.cpp / vLLM / LocalAI / remote** — `M3_LLM_URL="http://localhost:8080/v1"` (no LM Studio probe; add `M3_ENABLE_LMSTUDIO_FAILOVER=1` to keep it as a fallback).
- **Multiple endpoints in a specific order** — `LLM_ENDPOINTS_CSV="url1,url2,…"`.

### Embedding — model & client

Identity and client behavior of the embedder: which model/space vectors are
tagged with, chunking, in-process vs. HTTP embedding, bulk batching, and the httpx
connection pool. The stock bge-m3 defaults are correct for a standard install —
change the model/dim/space vars only if you deliberately re-embed your corpus into
a new vector space.

| Variable | Default | Purpose |
|---|---|---|
| `M3_EMBED_MODEL` | _(bge-m3)_ | Embed-model tag applied to stored vectors. *(legacy alias: `EMBED_MODEL`, still honored via `getenv_compat`.)* |
| `M3_EMBED_DIM` | — | Embedding vector dimension. A model whose output dim ≠ this is rejected. *(legacy alias: `EMBED_DIM`, still honored via `getenv_compat`.)* |
| `M3_EMBED_SPACE_TAG` | — | Identity tag for the embedding vector space; used to keep incompatible spaces from being compared. |
| `M3_EMBED_COMPATIBLE_MODELS` | — | CSV of model tags considered space-compatible with the active model (their vectors may be mixed in search). |
| `M3_EMBED_FALLBACK_MODEL_TAG` | — | Model tag applied to vectors produced on the fallback path. |
| `M3_EMBED_REQUIRE_UNIT_NORM` | — | When enabled, enforce that returned embeddings are unit-normalized. |
| `M3_EMBED_NORM_TOL` | — | Tolerance for the unit-norm check when `M3_EMBED_REQUIRE_UNIT_NORM` is on. |
| `M3_EMBED_INPROC` | — | When set, use the in-process embedder rather than POSTing to an embed server. |
| `M3_EMBED_INIT_TIMEOUT_S` | — | Timeout (seconds) for embedder initialization. |
| `M3_EMBED_SEARCH_DEADLINE_S` | — | Per-search wall budget for producing the query embedding before falling back. |
| `M3_EMBED_BULK_CHUNK` | `1024` | Batch size for bulk embedding. |
| `M3_EMBED_BULK_CONCURRENCY` | `4` | Concurrent batches during bulk embedding. |
| `M3_EMBED_CHUNK_MAX_CHARS` | — | Maximum characters per text chunk before embedding. |
| `M3_EMBED_CHUNK_OVERLAP_CHARS` | — | Character overlap between consecutive chunks. |
| `M3_EMBED_HTTP_MAX_CONNS` | — | httpx maximum total connections to the embed server. |
| `M3_EMBED_HTTP_MAX_KEEPALIVE` | — | httpx maximum keep-alive connections. |
| `M3_EMBED_HTTP_KEEPALIVE_EXPIRY` | — | httpx keep-alive expiry (seconds). |
| `M3_EMBED_DISCOVERY_NEG_TTL` | — | Negative-cache TTL (seconds) for failed embed-endpoint discovery. |
| `M3_ENTITY_NAME_EMBED_CACHE_MAX` | `50000` | Cap on cached entity-name embeddings held in memory. |
| `M3_MODELS_ROOT` | — | Root directory for local model files the embedder loads from. |
| `M3_DEBUG_EMBED_MODEL` | — | Debug-only embed-model override used by the agent-bridge debug path. |

### Embedding — circuit breakers

Per-tier failure count + cooldown for the embed-backend circuit breaker
(`bin/memory/config.py`). Each embed tier (in-process embedded, CPU fallback, HTTP
primary, cloud) trips independently after N consecutive failures and resets after a
cooldown. Defaults are conservative; tune only if a flaky backend trips too eagerly
or recovers too slowly.

| Variable | Default | Purpose |
|---|---|---|
| `M3_EMBED_BREAKER_EMBEDDED_THRESHOLD` | — | Consecutive failures before the in-process embedded tier trips open. |
| `M3_EMBED_BREAKER_EMBEDDED_RESET_SECS` | — | Cooldown (seconds) before the embedded tier is retried. |
| `M3_EMBED_BREAKER_CPU_FALLBACK_THRESHOLD` | — | Failure threshold for the CPU HTTP-fallback tier. |
| `M3_EMBED_BREAKER_CPU_FALLBACK_RESET_SECS` | — | Cooldown (seconds) for the CPU-fallback tier. |
| `M3_EMBED_BREAKER_PRIMARY_THRESHOLD` | — | Failure threshold for the HTTP primary tier. |
| `M3_EMBED_BREAKER_PRIMARY_RESET_SECS` | — | Cooldown (seconds) for the HTTP primary tier. |
| `M3_EMBED_BREAKER_CLOUD_THRESHOLD` | — | Failure threshold for the cloud embed tier. |
| `M3_EMBED_BREAKER_CLOUD_RESET_SECS` | — | Cooldown (seconds) for the cloud embed tier. |

### Embedding server

Bind, concurrency, and batching for the standalone embed servers
(`bin/embed_server_inproc.py`, `bin/embed_server_gpu.py`) and the embedder admin.

| Variable | Default | Purpose |
|---|---|---|
| `M3_EMBED_SERVER_HOST` | — | Bind host for the in-process embed server. |
| `M3_EMBED_SERVER_PORT` | — | Bind port for the in-process embed server. |
| `M3_EMBED_SERVER_GPU_HOST` | — | Bind host for the GPU embed server. |
| `M3_EMBED_SERVER_CONCURRENCY` | — | Maximum concurrent embed requests the server processes. |
| `M3_EMBED_SERVER_MAX_BATCH` | — | Maximum batch size the server coalesces per forward pass. |
| `M3_EMBED_SERVER_INTERACTIVE_RESERVED` | — | Concurrency slots reserved for interactive (low-latency) requests. |
| `M3_EMBED_INTERACTIVE_MAX_TEXTS` | — | Maximum texts allowed in a single interactive embed request. |
| `M3_EMBED_SERVER_BIN` | — | Path to the embed-server binary the admin launches. |
| `M3_LLAMA_PORT` | `9904` | Port the GPU embed server's llama.cpp backend listens on. |
| `M3_NO_MODEL_DOWNLOAD` | _(unset)_ | Set to forbid automatic model downloads (offline / air-gapped hosts). |

### SLM profiles

| Variable | Default | Purpose |
|---|---|---|
| `M3_SLM_PROFILE` | — | Active small-language-model profile name for the SLM intent classifier. |
| `M3_SLM_PROFILES_DIR` | — | Directory to load SLM profiles from. |

---

## Extraction & write-through

The generic SLM-extraction configuration shared by the fact-enrichment and
entity-graph pipelines (`bin/memory/extraction.py`). These are the **actual**
controlling vars — the older per-pipeline `M3_FACT_ENRICHED_*` /
`M3_ENTITY_GRAPH_*` names are not read by any code (see the corrected rows in
those sections). Write-through toggles govern whether extraction/classification
runs synchronously on the write path.

| Variable | Default | Purpose |
|---|---|---|
| `M3_EXTRACTION_TYPE` | `rule_based` | Which extraction pipeline to instantiate on write (`rule_based`, an SLM extractor, etc.). |
| `M3_EXTRACTION_URL` | (empty) | SLM extraction endpoint URL. When set (with a model), routes extraction to that OpenAI-compatible endpoint. |
| `M3_EXTRACTION_MODEL` | (empty) | SLM extraction model id. |
| `M3_EXTRACTION_PROMPT` | (empty) | Prompt override for the extractor. |
| `M3_EXTRACTION_SCRIPT` | (empty) | Path to a custom extraction script. |
| `M3_EXTRACTION_FUNCTION` | `extract` | Function/mode name invoked in the extractor. |
| `M3_EXTRACTION_WRITE_THROUGH` | — | When enabled, run extraction synchronously on the write path instead of enqueuing it. |
| `M3_INLINE_CLASSIFY` | — | When enabled, run type classification inline on the write path. |

---

## Fact Enrichment

SLM-distillation pipeline to extract atomic facts from stored memories. **On by default** — `memory_write` enqueues fact extraction unless you turn it off. It only does work when a fact-extraction SLM endpoint is reachable — configured through the generic **`M3_EXTRACTION_URL` / `M3_EXTRACTION_MODEL`** family (see [Extraction & write-through](#extraction--write-through)) or the `fact_enriched.yaml` profile; with no endpoint configured the queue simply no-ops. Because it calls a local LLM per write, it adds latency and (for chatty workloads) row volume — set `M3_ENABLE_FACT_ENRICHED=0` to disable. See ARCHITECTURE.md for design overview.

| Variable | Default | Purpose |
|---|---|---|
| `M3_ENABLE_FACT_ENRICHED` | `true` | Master gate. On by default; set to `0`/`false`/`no` to disable fact extraction on writes. |
| `M3_FACT_ENRICH_CONCURRENCY` | `2` | Maximum concurrent SLM enrichment tasks. Higher values parallelize fact extraction; lower values reduce latency jitter on write paths. |
| `M3_FACT_ENRICH_MAX_ATTEMPTS` | `5` | Maximum retries for failed enrichment queue items before they are marked as poison (poisoned items remain visible in queue with `last_error` for manual inspection). |
| ~~`M3_FACT_ENRICHED_URL`~~ | — | **Corrected.** No code reads this per-pipeline name. The SLM endpoint for fact extraction is set through the generic **`M3_EXTRACTION_URL`** (see [Extraction & write-through](#extraction--write-through)), falling back to the `fact_enriched.yaml` profile `url` field. |
| ~~`M3_FACT_ENRICHED_MODEL`~~ | — | **Corrected.** No code reads this per-pipeline name. The SLM model for fact extraction is set through the generic **`M3_EXTRACTION_MODEL`** (see [Extraction & write-through](#extraction--write-through)), falling back to the `fact_enriched.yaml` profile `model` field. |

### Enrichment pipeline knobs (batch enrichment CLI)

Controls for the batch-enrichment tooling (`bin/m3_enrich.py`) and the auto-enrich
trigger on chatlog ingest — profile selection, conversation targeting, retry/budget
caps, and input-size gates.

| Variable | Default | Purpose |
|---|---|---|
| `M3_ENRICH_PROFILE` | — | Enrichment profile the batch enricher runs under. |
| `M3_ENRICH_CONV_LIST` | — | Explicit list of conversation ids to enrich. |
| `M3_ENRICH_TRACK_STATE` | — | When enabled, persist per-conversation enrichment state to resume across runs. |
| `M3_ENRICH_MAX_ATTEMPTS` | — | Retry cap for a failed enrichment item. |
| `M3_ENRICH_BUDGET_USD` | — | Spend ceiling (USD) for a cloud-backed enrichment run. |
| `M3_ENRICH_INPUT_MAX_K` | — | Maximum input size (K tokens/chars) fed to the enricher per item. |
| `M3_ENRICH_MIN_SIZE_K` | — | Skip conversations smaller than this size. |
| `M3_ENRICH_MAX_SIZE_K` | — | Skip (or split) conversations larger than this size. |
| `M3_ENRICH_SEND_TO` | — | Routing target for enrichment output. |
| `M3_AUTO_ENRICH` | — | When enabled, chatlog ingest auto-triggers enrichment for qualifying conversations. |
| `M3_AUTO_ENRICH_MIN_TURNS` | — | Minimum turns a conversation needs before auto-enrich fires. |

---

## Procedure Distillation

Autonomous pipeline that rolls up successful (completed) task runs — a task plus its step/result memories — into reusable `procedure` memories (skill / runbook / how-to / checklist), linked back to their sources via `distills_from` edges (sources are **preserved**, never deleted). The engine is `memory_distill_procedures_impl`; `bin/distill_procedures.py` is the trigger, and the cognitive loop runs it event-driven + governor-gated. The distillation model is **local-first, cloud-capable**. See ARCHITECTURE.md / EXTENDING.md for design overview.

| Variable | Default | Purpose |
|---|---|---|
| `M3_DISTILL_AUTO` | `0` | Hard gate for autonomous procedure **writes**. Distillation runs **dry-run** unless `--apply` AND `M3_DISTILL_AUTO=1` — so a scheduled/loop invocation is a safe no-op until you opt in. |
| `M3_DISTILL_MODEL` | `slm` | Distillation model selector: unset/`slm` → the local `procedure_local` SLM profile (sovereign default); `llm` → the largest local model via `get_best_llm` failover; any other value → a profile name (another local model, or a cloud endpoint via a `backend: anthropic`/`openai` profile — cloud is config, not new code). |

---

## Entity-Relation Graph

SLM-extraction pipeline to build a typed knowledge graph of entities and relationships from stored memories. **On by default** — writes are queued for entity extraction unless you turn it off. Like fact enrichment, it only does work when an extraction SLM endpoint is reachable — configured through the generic **`M3_EXTRACTION_URL` / `M3_EXTRACTION_MODEL`** family (see [Extraction & write-through](#extraction--write-through)) or the `entity_graph.yaml` profile; with no endpoint the queue no-ops. It calls a local LLM per write, so it adds latency — set `M3_ENABLE_ENTITY_GRAPH=0` to disable. The entity-type and predicate vocabulary is user-configurable via `M3_ENTITY_VOCAB_YAML` (see below). See ARCHITECTURE.md for design overview.

| Variable | Default | Purpose |
|---|---|---|
| `M3_ENABLE_ENTITY_GRAPH` | `true` | Master gate. On by default; set to `0`/`false`/`no` to disable entity extraction on writes. |
| `M3_ENTITY_VOCAB_YAML` | (`config/lists/entity_graph_default.yaml`) | Path to the entity-type + predicate vocabulary profile. Swap or author your own to retune the graph schema for your domain — no code changes. The stock vocabulary defines a 7-type / 34-predicate schema spanning general, human-life, and technical domains. |
| `M3_ENTITY_EXTRACT_CONCURRENCY` | `2` | Maximum concurrent SLM extraction tasks. Mirrors fact_enriched concurrency tuning. |
| `M3_ENTITY_EXTRACT_MAX_ATTEMPTS` | `5` | Queue retry cap before poisoned-item exclusion. Failed items remain in extraction queue with `last_error` for manual inspection. |
| `M3_ENTITY_RESOLVE_FUZZY_MIN` | `0.8` | Minimum token-Jaccard similarity score for fuzzy-match resolution tier. Entities with canonical names matching above this threshold within the same type are merged. |
| `M3_ENTITY_RESOLVE_COSINE_MIN` | `0.85` | Minimum embedding cosine similarity for cosine-match resolution tier (final fallback before creating a new entity). |
| ~~`M3_ENTITY_GRAPH_URL`~~ | — | **Corrected.** No code reads this per-pipeline name. The SLM endpoint for entity extraction is set through the generic **`M3_EXTRACTION_URL`** (see [Extraction & write-through](#extraction--write-through)), falling back to the `entity_graph.yaml` profile `url` field. |
| ~~`M3_ENTITY_GRAPH_MODEL`~~ | — | **Corrected.** No code reads this per-pipeline name. The SLM model for entity extraction is set through the generic **`M3_EXTRACTION_MODEL`** (see [Extraction & write-through](#extraction--write-through)), falling back to the `entity_graph.yaml` profile `model` field. |

### Entity extraction & coalescing

Controls for the entity-coalescing pass (`bin/entity_coalesce.py`) that merges
duplicate entities, plus targeting/seed knobs for extraction. Coalescing is gated
off by default (`M3_ENTITY_COALESCE_FLAG`); the `MAX_*` caps bound its cost.

| Variable | Default | Purpose |
|---|---|---|
| `M3_ENTITY_COALESCE_FLAG` | — | Master enable for the entity-coalescing pass. |
| `M3_ENTITY_COALESCE_AUTOMERGE` | — | When enabled, high-confidence matches are merged automatically (vs. suggested only). |
| `M3_ENTITY_COALESCE_FUZZY_HIGH` | — | Fuzzy-similarity threshold above which two entities are treated as the same. |
| `M3_ENTITY_COALESCE_MAX_PAIRS` | — | Cap on candidate entity pairs compared per run. |
| `M3_ENTITY_COALESCE_MAX_BLOCK` | — | Maximum block size for the blocking stage. |
| `M3_ENTITY_COALESCE_MAX_CLUSTER` | — | Maximum cluster size a single merge may span. |
| `M3_ENTITY_SEED_STOPLIST` | — | Stoplist of seed terms excluded from entity extraction. |
| `M3_ENTITIES_CONV_LIST` | — | Explicit list of conversation ids to run entity extraction over. |

### GLiNER entity model

Configuration for the optional GLiNER neural entity extractor
(`bin/m3_entities_gliner.py`).

| Variable | Default | Purpose |
|---|---|---|
| `M3_GLINER_MODEL` | — | GLiNER model id/path to load. |
| `M3_GLINER_THRESHOLD` | — | Confidence threshold for accepting an extracted span. |
| `M3_GLINER_BATCH_SIZE` | — | Batch size for GLiNER inference. |
| `M3_GLINER_DEVICE` | — | Compute device for GLiNER (`cpu`, `cuda`, `mps`, …). |

---

## Wiki — synthesis & compilation

The [wiki](WIKI.md) can attach an LLM prose lede per topic (`--synthesize`) or
compile durable synthesis memories (`m3 wiki compile`). Both talk to an
OpenAI-compatible `/v1/chat/completions` endpoint (your local model). The
`COMPILE` vars fall back to the `SYNTH` vars when unset, so pointing one endpoint
via `M3_WIKI_SYNTH_*` configures both; set `M3_WIKI_COMPILE_*` only to route
compilation to a different/larger model than the lede synthesizer.

| Variable | Default | Purpose |
|---|---|---|
| `M3_WIKI_SYNTH_URL` | `http://127.0.0.1:1234/v1/chat/completions` | Chat endpoint for `--synthesize` ledes (LM Studio, llama-server, Ollama, vLLM, …). |
| `M3_WIKI_SYNTH_MODEL` | (empty → server's loaded model) | Model id to request for ledes. |
| `M3_WIKI_SYNTH_TIMEOUT` | `30` | Per-request timeout (seconds) for ledes. |
| `M3_WIKI_COMPILE_URL` | (falls back to `M3_WIKI_SYNTH_URL`, then `http://127.0.0.1:1234/v1/chat/completions`) | Chat endpoint for `m3 wiki compile`. Set to route compilation to a different endpoint than lede synthesis. |
| `M3_WIKI_COMPILE_MODEL` | (falls back to `M3_WIKI_SYNTH_MODEL`, then server's loaded model) | Model id for compilation — e.g. a larger model than the lede synthesizer. |
| `M3_WIKI_COMPILE_TIMEOUT` | `60` | Per-request timeout (seconds) for compilation. Higher than the lede default: a compiled page is longer than a 2–3 sentence lede. |
| `M3_WIKI_DRIFT_URL` | (falls back to `M3_WIKI_SYNTH_URL`) | Chat endpoint for the optional citation-drift judge (`gen_wiki.py --check-drift`). |
| `M3_WIKI_DRIFT_MODEL` | (falls back to `M3_WIKI_SYNTH_MODEL`) | Model id for the drift judge — shared with the derivability judge. |
| `M3_WIKI_DRIFT_TIMEOUT` | (falls back to the `DriftConfig` default) | Per-request timeout (seconds) for the drift/derivability judge. |
| `M3_WIKI_DRIFT_RECALL_FLOOR` | `0.80` | Minimum recall the drift judge must clear in its live-validation test (no fallback). |
| `M3_WIKI_DRIFT_PRECISION_FLOOR` | `0.70` | Minimum precision the drift judge must clear in its live-validation test (no fallback). |

The drift vars also configure the GDPR derivability judge (`--review-derivability`),
which reuses `DriftConfig`. The LLM token is read from `LM_API_TOKEN`
(see [API Keys](#api-keys--authentication)).
Admission-gate tuning is a config file, not an env var — see the [wiki docs](WIKI.md).

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

## Cognitive loop, observer & reflector

The autonomous cognitive loop (`bin/m3_cognitive_loop.py`) and its observer /
reflector passes. Deadlines and per-pass limits bound how much work each pass does;
profile vars select the model/behavior. See ARCHITECTURE.md for the loop design.

| Variable | Default | Purpose |
|---|---|---|
| `M3_CLASSIFY_DEADLINE_S` | — | Wall budget (seconds) for the loop's classification step before it yields. |
| `M3_LIMIT_PER_PASS` | — | Maximum items the loop processes in a single pass (see also `M3_GOVERNOR_THROTTLED_LIMIT`). |
| `M3_OBSERVER_PRECISE_PROVENANCE` | — | When enabled, the observer records precise (per-source) provenance for its observations. |
| `M3_REFLECTOR_PROFILE` | — | Model/behavior profile for the reflector pass. *(legacy alias: `REFLECTOR_PROFILE`, still honored via `getenv_compat`.)* |

## Dashboard

Bind address and health-probe caching for the local m3 dashboard
(`bin/dashboard_server.py`).

| Variable | Default | Purpose |
|---|---|---|
| `M3_DASHBOARD_HOST` | — | Bind host for the dashboard server. |
| `M3_DASHBOARD_PORT` | `8088` | Bind port for the dashboard server. |
| `M3_DASHBOARD_LLM_SMOKE` | — | When enabled, run an LLM smoke test as part of the dashboard health check. |
| `M3_DASHBOARD_LLM_SMOKE_TTL_S` | — | Cache TTL (seconds) for the LLM smoke-test result. |
| `M3_DASHBOARD_LLM_BLOCK_TTL_S` | — | TTL (seconds) for the LLM health "blocked" state before it is re-probed. |

## Cloud / privacy enclave

Opt-in cloud fallback for embedding/inference through a privacy-preserving enclave.
All default OFF — no data leaves the machine unless `M3_ALLOW_CLOUD_FALLBACK` is
enabled and an enclave URL is set. The minimization level governs how much of a
payload is stripped before it leaves the host.

| Variable | Default | Purpose |
|---|---|---|
| `M3_ALLOW_CLOUD_FALLBACK` | _(off)_ | Master gate allowing fallback to the cloud enclave when local backends are unavailable. |
| `M3_CLOUD_ENCLAVE_URL` | — | URL of the privacy enclave endpoint. |
| `M3_CLOUD_AUTH_TOKEN` | — | Bearer token for the cloud enclave. |
| `M3_CLOUD_AUTH_TOKEN_KEYRING` | — | Keyring entry name to read the enclave token from (preferred over the inline var). |
| `M3_CLOUD_MINIMIZATION_LEVEL` | — | How aggressively payloads are minimized/redacted before leaving the host. |

## Installation & runtime

| Variable | Default | Purpose |
|---|---|---|
| `M3_HOME` | _(resolver default)_ | m3 home root used by install/runtime hooks (e.g. pre-compact). |
| `M3_AUTO_INSTALL` | _(unset)_ | When set, the CLI auto-runs the installer if the payload is missing. |
| `M3_TASK_LOG_FILE` | — | Path the task runtime writes its log to. |

## Development & repo tooling

These are read by **development & deployment** tooling — git hooks, docs/badge
generators, CI, and release flows — **not** by the m3 runtime. A user running m3
never needs these; only a developer/maintainer does. They are read by a shell
process (not the MCP server), so they live in your shell / Windows-user
environment (or CI secrets), not in the MCP server `env` block that the runtime
`M3_*` roots use.

| Variable | Default | Purpose |
|---|---|---|
| `M3_PREPUSH_EXCLUDES` | `~/.m3-private/m3_PrePush_Excludes.txt` | Path to a file of **local** pathspecs the pre-push leakage scan (`.githooks/pre-push`, gate 3) should exempt — one pathspec per line, `#` comments and blanks ignored. For machine-/user-specific paths that legitimately contain scan patterns (a developer's own username directories, a local scratch tree) but are not leaks. Keeps the *mechanism* in the public tracked hook while the *exclusion list* stays in the private `~/.m3-private` tree. Read defensively: a missing / empty / unreadable file is a harmless no-op (the hook's tracked excludes always apply), so the var is entirely optional. Set it only to point the hook at a non-default location. |
| `GITHUB_TOKEN` / `GH_TOKEN` | (unset) | GitHub API token — m3 uses GitHub **only for development & deployment**, never at runtime: the docs-generation scripts (`bin/gen_star_history.py`, `bin/gen_download_badges.py`) read it to fetch stargazer / clone-count stats, and CI/release flows use it. In CI, `GITHUB_TOKEN` is injected automatically; locally use `GITHUB_TOKEN=$(gh auth token)`. `GH_TOKEN` is an accepted alias. Unset → those generators skip or error clearly; no effect on the m3 runtime. |
| `M3_STARGAZER` | (unset) | Fallback GitHub token for `bin/gen_download_badges.py` (README clone-count badge) when `GITHUB_TOKEN` is unset. A read-only token is sufficient. Unset → the clone badge is skipped, not an error. Badge/docs generation only; never read at runtime. |
