# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Memory — Technical Reference


> Implementation specifics: schema, search internals, sync protocol, security, configuration, testing, and developer tooling.
>
> For the conceptual system design, see [docs/ARCHITECTURE.md](ARCHITECTURE.md).
> For the AI/LLM agent instruction set, see [AGENT_INSTRUCTIONS.md](./AGENT_INSTRUCTIONS.md).
> For the feature overview, see [CORE_FEATURES.md](./CORE_FEATURES.md).

---

## 📡 LLM Server Requirements

M3 Memory is **server-agnostic**. It communicates with local LLMs via the OpenAI-compatible API. Any server that exposes these two endpoints will work:

| Endpoint | Used For |
|----------|----------|
| `GET /v1/models` | Auto-detect loaded models (embedding and chat) |
| `POST /v1/embeddings` | Generate vector embeddings for semantic search |
| `POST /v1/chat/completions` | Auto-classification, conversation summarization, consolidation |

**Known compatible servers:** LM Studio, Ollama, vLLM, LocalAI, llama.cpp server, text-generation-webui (with OpenAI extension), Aphrodite, TGI.

Default endpoint: `http://localhost:1234/v1`. Override with `LLM_ENDPOINTS_CSV` env var for different ports or multi-machine setups (e.g., Ollama defaults to port 11434: `LLM_ENDPOINTS_CSV="http://localhost:11434/v1"`).

---

## 💾 Storage Implementation

> For the conceptual storage hierarchy (SQLite → PostgreSQL → ChromaDB), see [ARCHITECTURE.md](ARCHITECTURE.md).

### SQLite Configuration

- WAL mode enabled for concurrent read/write
- Connection pool (configurable, default size 5) via `m3_sdk.py`
- Semaphore-bounded embedding concurrency (max 4 concurrent) to prevent local LLM server overload
- Thread-safe HTTP client with double-check locking

### PostgreSQL Sync Details

- Configurable via `PG_URL` env var or encrypted vault — no hardcoded credentials
- Bi-directional delta sync via `bin/pg_sync.py` using watermark-based UPSERT
- Syncs: memory items (including `user_id`, `scope`, `valid_from`, `valid_to`, `content_hash`), relationships, embeddings, encrypted secrets
- Auto-creates `agent_retention_policies` and `gdpr_requests` tables if missing
- Hourly automated sync via `bin/pg_sync.sh` cron job
- Sync lock prevents concurrent runs (stale after 1 hour)

### ChromaDB Federation Details

- v2 API (configurable via `CHROMA_BASE_URL`), collection `agent_memory`
- `chroma_mirror` table serves reads during outages
- Stalled sync items auto-retry with configurable attempt limits

### Database Schema

#### Core Tables

```text
  [ memory_items ]          [ memory_embeddings ]      [ memory_relationships ]
  +----------------+        +-------------------+      +----------------------+
  | id (UUID)      |<---+   | id (UUID)         |      | id (UUID)            |
  | type           |    |   | memory_id (FK) ---|      | from_id (FK) --------|--+
  | title          |    +---| embedding (BLOB)  |      | to_id (FK) ----------|--+
  | content        |        | dim               |      | relationship_type    |
  | ...            |        +-------------------+      +----------------------+
```

| Table | Key Columns | Purpose |
|-------|-------------|---------|
| `memory_items` | `id` (UUID PK), `type`, `title`, `content`, `metadata_json`, `agent_id`, `model_id`, `change_agent`, `importance`, `source`, `origin_device`, `user_id`, `scope`, `is_deleted`, `expires_at`, `valid_from`, `valid_to`, `content_hash`, `created_at`, `updated_at`, `last_accessed_at`, `access_count`, `conversation_id`, `refresh_on`, `refresh_reason` | Primary memory storage |
| `memory_embeddings` | `id` (UUID PK), `memory_id` (FK), `embedding` (BLOB), `embed_model`, `dim`, `content_hash`, `created_at` | Vector embeddings stored as packed float32 arrays |
| `memory_relationships` | `id` (UUID PK), `from_id`, `to_id`, `relationship_type`, `created_at` | Directed knowledge graph edges |
| `memory_history` | `id` (UUID PK), `memory_id`, `event`, `prev_value`, `new_value`, `field`, `actor_id`, `created_at` | Immutable audit trail |

#### Support Tables

| Table | Purpose |
|-------|---------|
| `chroma_sync_queue` | Outbound queue for ChromaDB federation (with `stalled_since`, `attempts`) |
| `chroma_mirror` / `chroma_mirror_embeddings` | Local cache of remote ChromaDB data for offline reads |
| `sync_conflicts` | Conflict log for bi-directional sync resolution |
| `sync_state` / `sync_watermarks` | Watermark tracking for delta sync |
| `agent_retention_policies` | Per-agent: `max_memories`, `ttl_days`, `auto_archive` |
| `gdpr_requests` | GDPR request audit: `subject_id`, `request_type`, `status`, `items_affected` |
| `synchronized_secrets` | Encrypted credential vault: `service_name`, `encrypted_value`, `version`, `origin_device` |

### Indexes on `memory_items`

The hot paths drive the index set. Rebuild history is tracked through migrations; the current set is:

| Index | Definition | Hot path |
|---|---|---|
| `idx_mi_type` | `(type)` | Type-filtered search |
| `idx_mi_agent` | `(agent_id)` | Agent-scoped search |
| `idx_mi_model` | `(model_id)` | Provenance lookups |
| `idx_mi_change_agent` | `(change_agent)` | Audit |
| `idx_mi_created` | `(created_at)` | Recency ordering |
| `idx_mi_updated` | `(updated_at)` | Sync delta windows |
| `idx_mi_deleted` | `(is_deleted)` | Live-row filter |
| `idx_mi_deleted_type` | `(is_deleted, type)` | Common composite filter |
| `idx_mi_handoff_inbox` | `(agent_id, type, read_at, created_at)` | `memory_inbox` hot path |
| `idx_mi_importance` | `(importance)` | Decay + retention |
| `idx_mi_scope` | `(scope)` | Multi-tenant filter |
| `idx_mi_user_id` | `(user_id)` | GDPR lookups |
| `idx_mi_valid_from` | `(valid_from)` | Bitemporal as-of queries |
| `idx_mi_conversation_id` | `(conversation_id, created_at) WHERE is_deleted = 0` | Conversation-scoped search, composite partial (v015) |
| `idx_mi_refresh_on` | `(refresh_on) WHERE refresh_on IS NOT NULL` | Refresh backlog scan, partial (v014) |

Both new indexes are **partial indexes**: they only cover rows that actually need them (`conversation_id` non-null live rows; `refresh_on` non-null rows). This keeps the indexes small and the lookups O(flagged-rows) rather than O(table).

Query-plan verification used `EXPLAIN QUERY PLAN` against a synthetic 1000-row fixture to confirm the planner picks the intended index for each new hot path rather than falling back to a lower-selectivity index like `idx_mi_deleted`.

### Migrations

Schema migrations are managed by `bin/migrate_memory.py` — a subcommand-driven CLI that applies versioned SQL files from `memory/migrations/`:

```
python bin/migrate_memory.py status           # show current version + pending
python bin/migrate_memory.py up                # apply pending (interactive, prompts for backup dir + confirmation)
python bin/migrate_memory.py up -y             # apply pending non-interactively (CI/scripted)
python bin/migrate_memory.py down --to N       # roll back to version N
python bin/migrate_memory.py backup [--out DIR]  # standalone backup
python bin/migrate_memory.py restore <PATH>    # restore from backup
```

**File naming.** Each migration has a numeric prefix and now uses explicit direction suffixes:

- `NNN_name.up.sql` — forward migration (required)
- `NNN_name.down.sql` — rollback migration (optional; if absent the migration is irreversible)
- `NNN_name.sql` — **legacy** format (v001–v012). Treated as up-only. Any attempt to roll back past a legacy migration is refused with a clear error naming the lowest reversible target.

Migrations v013+ ship both `.up.sql` and `.down.sql` files.

**Version tracking.** The `schema_versions` table records `(version, filename, applied_at)` for every applied migration. Current version = `MAX(version)`. The runner applies each SQL script inside a transaction, then inserts the version row in the same transaction — a failure rolls back both. A legacy idempotency fallback is preserved: if an `up` fails with a `duplicate column name` / `already exists` error, the migration is marked applied anyway (so existing DBs that predate the migration runner catch up cleanly).

**File-level backups.** Before every `up` or `down`, the runner copies `memory/agent_memory.db` (plus any `-wal` / `-shm` sidecars) to a timestamped file like `agent_memory.v014.pre-up.20260412T145131Z.db`. The destination directory is chosen once via an interactive prompt on first run (default: `~/.m3-memory/backups/`, recommended out-of-repo even though `*.db` is gitignored) and persisted to `memory/.migrate_config.json`. In-DB transactions provide instant rollback on a failure mid-migration; the filesystem backup is the escape hatch for "I applied the wrong migration and want to undo it after the fact."

**User confirmation.** Interactive runs print the list of pending migrations (noting which have down files), ask for the backup directory, then require a final `y/N` before writing. The `-y` / `--yes` flag skips both prompts for CI and scripted use. `up` with no arguments also requires confirmation — the old zero-argument invocation still works, just interactively.

**Reversibility rules.** `down --to N` walks applied versions in reverse and runs each `.down.sql`. Pre-flight check refuses the whole batch if any version in the revert path lacks a down file. This prevents partially-rolled-back state where some versions rolled back cleanly and others couldn't.

---

## 🔍 Search Engine

> For the conceptual search pipeline overview, see [ARCHITECTURE.md](ARCHITECTURE.md).

### Implementation Details

**Stage 1 — FTS5 Keyword Matching**
- SQLite FTS5 with BM25 ranking
- Query sanitization via `_sanitize_fts()`: strips `OR`, `AND`, `NOT`, `NEAR` operators and special characters
- Prefix matching for single alphanumeric terms, exact match for quoted queries
- Falls back to semantic-only search if FTS returns no results or throws `OperationalError`

**Stage 2 — Vector Similarity**
- Cosine similarity via `numpy` batch operations (pure-Python `embedding_utils.cosine` fallback)
- Query vector generated by `_embed()` with 3-attempt retry, 30s semaphore timeout
- Result matrix capped at `SEARCH_ROW_CAP` (default 500) rows to bound memory usage
- Embedding cache: content hash lookup avoids re-embedding identical text

**Stage 3 — MMR Re-Ranking**
- Maximal Marginal Relevance with λ=0.7
- Pre-selects top `k × 3` candidates, then iteratively picks items that balance relevance (70%) against diversity (30%)
- Prevents near-duplicate results in top-k

**Score Formula:** `final = 0.7 × cosine_score + 0.3 × (1 / (1 + |bm25_score|))`

**Federated Fallback:** If local results < 3 and no type filter, ChromaDB is queried as an L3 fallback. Duplicate IDs are excluded.

**Explainability:** `explain=True` mode (via `memory_suggest` tool) returns per-result breakdowns: vector score, BM25 score, MMR penalty, and raw combined score.

### Embedding System

- **Model:** Configurable via `EMBED_MODEL` env var (default: `qwen3-embedding`)
- **Dimension:** All models across all devices must produce the same dimension (default 1024, configurable via `EMBED_DIM`). Mismatched dimensions break cosine similarity and ChromaDB upserts.
- **Auto-detection:** `embedding_utils.py` + `llm_failover.get_best_embed()` probe the local LLM server's `/v1/models` endpoint for loaded models, preferring names containing `embed`, `nomic`, or `jina`
- **Dimension validation:** First embedding call validates actual vs expected dimensions; logs warning on mismatch
- **Concurrency:** Bounded by `asyncio.Semaphore(4)` with 30s timeout to prevent deadlocks
- **Packing:** Embeddings stored as packed `float32` BLOB via `embedding_utils.pack()`/`unpack()`

---

## 🧠 Intelligence Features

### Contradiction Detection

On every `memory_write` (except `conversation`/`message` types):
1. Query existing same-type items by cosine similarity against the new content's embedding
2. If a near-duplicate is found (cosine > `CONTRADICTION_THRESHOLD`, default 0.85) with matching title but different content:
   - Old memory is soft-deleted (`is_deleted = 1`)
   - A `supersedes` relationship is created from new → old
   - History event recorded with `supersede` type

### Auto-Linking

After contradiction check, if no contradiction was found and related candidates exist (cosine > 0.7), the top candidate is linked via `related` relationship.

### LLM Auto-Classification

When `type="auto"` is passed to `memory_write`:
1. Local LLM is called via `llm_failover.get_best_llm()` with a prompt listing all 32 classifier-eligible types (canonical `VALID_MEMORY_TYPES` minus the `auto` sentinel)
2. Response is parsed, stripped, lowercased
3. If result matches a valid type, it's used; otherwise falls back to `"note"`
4. Results cached in `_CLASSIFY_CACHE` keyed by content hash

### Conversation Summarization

`conversation_summarize_impl(conversation_id, threshold=20)`:
1. Fetches all messages via `memory_relationships` join
2. If count < threshold, returns early
3. Concatenates as `role: content` pairs
4. Calls local LLM with summarization prompt
5. Stores summary as `type="summary"` memory, linked to conversation via `references`

### Multi-Layered Consolidation

`memory_consolidate_impl(type_filter, agent_filter, threshold)`:
1. Groups memories by `(type, agent_id)` where `is_deleted = 0`
2. For groups exceeding threshold, selects oldest excess items
3. Calls local LLM to generate consolidated summary
4. Stores summary, links to sources via `consolidates`, soft-deletes sources

---

## 🔒 Security

### Credential Resolution (`bin/auth_utils.py`)

Priority order:
1. **Environment variables** — checked first
2. **OS keyring** — macOS Keychain, Windows Credential Manager, Linux SecretService
3. **Encrypted vault** — `synchronized_secrets` table, AES-256 via Fernet/PBKDF2-HMAC-SHA256, 600K iterations

Master key (`AGENT_OS_MASTER_KEY`) must be in native OS keyring. Never stored in code or files. Legacy 100K-iteration secrets auto-migrate to 600K on first decryption.

### Content Integrity

- SHA-256 hash computed on every `memory_write` and stored in `content_hash` column
- `memory_verify` re-computes and compares — returns `Integrity OK` or `INTEGRITY VIOLATION`
- Embedding `content_hash` also stored for cache lookup validation

### Input Safety (Poisoning Prevention)

`_check_content_safety()` runs on every write, rejecting content matching:
| Pattern | Catches |
|---------|---------|
| `<script\b` | XSS injection |
| `(DROP\|DELETE\|ALTER)\s+TABLE` | SQL injection attempts |
| `__import__\|exec\s*(\|eval\s*(` | Python code injection |
| `(ignore\|disregard)\s+(all\s+)?(previous\|prior)\s+instructions` | Prompt injection |

### Runtime Hardening

- All logging to `stderr` only — token values never logged
- `httpx` with strict timeouts: connect 3s, read 10–30s
- Circuit breaker: 3-failure threshold, 60s cooldown
- Thread-safe HTTP client creation via double-check locking
- FTS5 query sanitization at search boundary
- Embedding batch operations bounded by semaphore

---

## 👥 Scoping & Multi-Tenancy

| Scope | Isolation | Behavior |
|-------|-----------|----------|
| `user` | Per-user | Persists across sessions and agents |
| `session` | Per-session | Auto-expires after 24 hours via `expires_at` |
| `agent` | Per-agent (default) | Standard agent-scoped memory |
| `org` | Organization-wide | Shared across all users and agents |

Every `memory_search` accepts `user_id`, `scope`, and `conversation_id` filters. Invalid scopes fall back to `"agent"`. `conversation_id` shares the same ID space as `conversation_start` — there is one concept of "conversation", not two.

---

## 🔄 Refresh Lifecycle

Memories can be flagged with `refresh_on` (ISO-8601 timestamp) and `refresh_reason` on write or update. When the timestamp has arrived, the memory enters the **refresh queue** — a read-only view exposed via the `memory_refresh_queue` tool. Maintenance never mutates these flags; actual refresh goes through `memory_update` and is recorded in `memory_history`.

### Data flow

```
memory_write(refresh_on=T)
       │
       ▼
memory_items.refresh_on = T          [indexed: idx_mi_refresh_on WHERE refresh_on IS NOT NULL]
       │
       ▼ (T arrives)
memory_maintenance runs
       │
       ├──► report: "Refresh queue: N memories due for review"
       │
       └──► notifications fan-out (one per distinct agent_id with due memories)
              │                      (deduped against existing unacked refresh_due)
              ▼
       notifications.kind = 'refresh_due'
       payload = {count, sample_ids[:3]}
```

Agents discover the backlog through three off-path channels:

1. **`memory_refresh_queue` pull** — always available, zero cost when not called. Parameters: `agent_id` (filter), `limit` (default 50, max 500), `include_future` (show not-yet-due memories too).
2. **Lifecycle hint on `agent_register` / `agent_offline`** — the response string is appended with `| N memories of yours due for refresh (see memory_refresh_queue)` when the backlog is non-empty. Helper: `memory_core._count_refresh_backlog(agent_id)` — one indexed `COUNT(*)` against the partial index.
3. **`refresh_due` notification** — emitted by `memory_maintenance`, one row per distinct owning agent with due memories. Dedup query checks for an existing unacked `refresh_due` notification for the same agent; if present, no new notification is inserted. This means repeated maintenance runs are idempotent. After the agent calls `notifications_ack`, the next maintenance run can re-notify if the backlog is still non-empty.

### Update semantics

`memory_update` accepts `refresh_on`, `refresh_reason`, and `conversation_id` as first-class fields. The sentinel value `"clear"` sets the column to `NULL`; an empty string means "no change". Each change is recorded in `memory_history` with the field name and both old and new values, so the refresh history is queryable via `memory_history(id)`.

### Why not a separate lifecycle table?

Early design considered soft-deleting old memories on refresh and inserting new ones. That would duplicate what `memory_history` already does (versioning via field-level audit trail) and create two parallel "this memory changed over time" mechanisms. Instead, `refresh_on` is a **signal** — a column that means "review me" — and the update path stays unified.

---

## 🔁 Sync System (`bin/pg_sync.py`)

### Delta Sync Protocol

1. **Acquire lock** — global sync lock via `sync_state` table (stale after 1 hour)
2. **Push local → remote** — SELECT changed rows since last `pg_push` watermark, UPSERT into PG
3. **Ensure tier tables** — auto-create `agent_retention_policies` and `gdpr_requests` in PG if missing
4. **Push relationships** — delta sync `memory_relationships` via `rel_push` watermark
5. **Push embeddings** — delta sync `memory_embeddings` via `emb_push` watermark (BYTEA conversion)
6. **Push secrets** — version-based conflict resolution (higher version wins)
7. **Pull** — reverse of push for each table, same watermark pattern
8. **Release lock**

Batch size: 100 rows per commit. All UPSERTs use `ON CONFLICT (id) DO UPDATE` with timestamp-based conflict resolution and `change_agent` priority (manual/system edits protected).

### Watermark Semantics

Watermark updates are NOT atomic with data writes. A crash between data write and watermark update causes duplicate rows on next sync. This is safe because all operations use UPSERT — at-least-once delivery.

---

## ⚙️ Configuration

### Environment Variables

| Variable | Default | Controls |
|----------|---------|----------|
| `DEDUP_LIMIT` | 1000 | Max items scanned during deduplication |
| `DEDUP_THRESHOLD` | 0.92 | Cosine threshold for duplicate detection |
| `CONTRADICTION_THRESHOLD` | 0.85 | Cosine threshold for contradiction detection |
| `SEARCH_ROW_CAP` | 500 | Max rows for cosine computation per search |
| `EMBED_MODEL` | qwen3-embedding | Embedding model name (must be loaded in your local LLM server) |
| `EMBED_DIM` | 1024 | Expected embedding dimensions |
| `DB_POOL_SIZE` | 5 | SQLite connection pool size |
| `DB_POOL_TIMEOUT` | 30 | Pool acquisition timeout (seconds) |
| `ORIGIN_DEVICE` | `platform.node()` | Device identifier for sync provenance |
| `CHROMA_BASE_URL` | (auto-detected) | ChromaDB endpoint override |
| `PG_URL` | (vault/env) | PostgreSQL connection string |
| `LLM_ENDPOINTS_CSV` | `http://localhost:1234/v1` | Comma-separated OpenAI-compatible LLM server endpoints |
| `MMR_LAMBDA` | 0.7 | MMR relevance vs. diversity balance |
| `M3_SPEAKER_IN_TITLE` | `1` | When a memory's `metadata.role` is a proper name, prepend `[Role]` to the title at write time so FTS5 can match speaker-scoped queries. Set to `0` to disable. |
| `M3_SHORT_TURN_THRESHOLD` | 20 | Character-length threshold below which the ranker applies a length penalty (floor 0.3×) to suppress filler turns like "ok cool". |
| `M3_TITLE_MATCH_BOOST` | 0.05 | Per-query-token-overlap boost applied when the title echoes query tokens. Set to 0 to disable. |
| `M3_IMPORTANCE_WEIGHT` | 0.05 | Weight of the caller-supplied `importance` field in final ranking. Set to 0 to ignore importance during ranking. |
| `M3_INGEST_WINDOW_CHUNKS` | 0 | Emit a rolling `type="summary"` row every `M3_INGEST_WINDOW_SIZE` turns of a conversation. Off by default. |
| `M3_INGEST_WINDOW_SIZE` | 3 | Turns combined into each window chunk when window chunks are enabled. |
| `M3_INGEST_GIST_ROWS` | 0 | Emit a heuristic per-conversation gist row once the turn count passes `M3_INGEST_GIST_MIN_TURNS`, and every `M3_INGEST_GIST_STRIDE` after. Deterministic; no LLM. |
| `M3_INGEST_GIST_MIN_TURNS` | 8 | Threshold before the first gist is written. |
| `M3_INGEST_GIST_STRIDE` | 8 | Stride between subsequent gist updates. |
| `M3_INGEST_EVENT_ROWS` | 0 | Regex-extract event sentences from each message and emit `type="event_extraction"` rows linked back via `references`. Deterministic; no LLM. |
| `M3_QUERY_TYPE_ROUTING` | 0 | When a query matches "when/what date/which day" + a proper noun, shift `vector_weight` to 0.3 (BM25-heavy) so named-entity signal isn't diluted. |

Always-on: when `metadata.temporal_anchors` is supplied, resolved ISO dates are prefixed to the embed text as `[YYYY-MM-DD] …` so absolute-date queries hit rows even when the source text says "yesterday". No flag; no-op when anchors are absent.

### Valid Memory Types (21)

`note`, `fact`, `decision`, `preference`, `conversation`, `message`, `task`, `code`, `config`, `observation`, `plan`, `summary`, `snippet`, `reference`, `log`, `home`, `user_fact`, `scratchpad`, `knowledge`, `event_extraction`, `auto` (triggers LLM classification)

### Valid Relationship Types (8)

`related`, `supports`, `contradicts`, `extends`, `supersedes`, `references`, `message`, `consolidates`

---

## 🧪 Testing

### End-to-End Test Suite (`bin/test_memory_bridge.py`)

193 tests across all feature categories (memory CRUD, search, contradictions, GDPR, sync, maintenance, orchestration, refresh lifecycle, multi-agent handoffs, tasks, notifications):

| Category | Tests | What's Verified |
|----------|-------|----------------|
| Memory CRUD | 1-5 | Write (embed/no-embed), get, update, scoping, session auto-expire |
| Search | 4, 20 | Hybrid FTS+semantic, scope filtering, semantic fallback |
| Conversations | 6-7 | Start, append, messages ordering, search, relationship creation |
| Delete | 8-9 | Soft-delete (recoverable), hard-delete (cascade to embeddings, relationships, sync queue) |
| Sync & Federation | 10, 12-14, 16-18 | ChromaDB sync, mirror fallback, stalled retry, conflict schema, sync_status |
| Integrity & Safety | 15, 25-28, 34-35 | Content hash (SHA-256), FTS sanitization, audit trail, tamper detection, poisoning rejection, schema validation |
| Knowledge Graph | 21-24, 26 | History events, link creation, duplicate rejection, graph traversal, contradiction detection |
| Tier 5 Features | 29-33, 37-41 | Retention policies, GDPR export/forget, cost report, bitemporal, explainability, export/import, consolidation, auto-classify, configurable thresholds |

### Retrieval Benchmarks (`bin/benchmark_memory.py`)

| Metric | Description | Pass Threshold |
|--------|-------------|---------------|
| Hit@1 | Expected item is top result | — |
| Hit@5 | Expected item in top 5 | — |
| MRR | Mean Reciprocal Rank | > 0.5 |
| Latency | p50/p95 per search (ms) | — |

Seeds 20 diverse test memories, runs 10 labeled queries, cleans up after. Gracefully skips when the local LLM server is offline.

---

## 🛠️ Developer Tooling

### M3 SDK (`bin/m3_sdk.py`)

- `M3Context` — manages SQLite connection pool, PostgreSQL connections (circuit breaker, 2-attempt retry, 10s connect timeout), and secret resolution
- `resolve_venv_python()` — cross-platform venv Python path resolution (Windows/macOS/Linux)
- `get_async_client()` — thread-safe shared `httpx.AsyncClient` with double-check locking

### Key Scripts

| Script | Purpose |
|--------|---------|
| `bin/migrate_memory.py` | Idempotent schema migration runner |
| `bin/generate_configs.py` | Auto-sync MCP bridge paths in `claude-settings.json` and `gemini-settings.json` |
| `bin/install_schedules.py` | Platform-agnostic scheduler: cron (macOS/Linux), Task Scheduler (Windows) |
| `bin/pg_sync.py` | Bi-directional PostgreSQL delta sync |
| `bin/mcp_check.sh` | MCP bridge connectivity health check |
| `bin/benchmark_memory.py` | Retrieval quality benchmarks |
| `bin/test_memory_bridge.py` | 41 end-to-end tests |

### LLM Engine (`bin/llm_failover.py`)

- `get_best_llm(client, token)` — probes endpoints in failover order, filters embedding models, returns `(base_url, model)` for the largest available model by parameter count
- `get_best_embed(client, token)` — same pattern, but selects embedding models
- **Failover chain:** `localhost:1234` → additional endpoints (configurable via `LLM_ENDPOINTS_CSV`)
- **Served by:** Any OpenAI-compatible server (e.g., LM Studio, Ollama, vLLM, LocalAI). Supports MLX, GGUF, GPTQ, and other model formats depending on server.
