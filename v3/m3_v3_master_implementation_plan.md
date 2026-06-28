# 🚀 Project M3-v3: Hardened Master Implementation Plan

This is the master engineering plan for **Project M3-v3 (Multi-Session Implementation)**. The primary goals are to:
1.  **Relocate system configuration and data structures** to new standard roots (`~/.m3/config` and `~/.m3/engine`).
2.  **Harden the FIPS 140-3 boundary** (wolfSSL integration in `crypto_provider.py`).
3.  **Introduce hybrid cloud capabilities** with local PII-redacted failover.
4.  **Extend Project Oxidation** (Rust graph traversal, query tokenization, directory ingestion).
5.  **Inject pre-compiled optimization layers** (GHA Wheel automation and SQLite custom loadable C/Rust extensions).
6.  **Realign and Oxidize the M3 SDK** (Boundary cleanup, platform telemetry unification, and fast Rust logging/circuits).
7.  **Enforce Hardened Operational Guardrails** (Adversary-mitigated startup locking, strict compliance fail-closed rules, and system cohesion tables).
8.  **Inject Advanced Optimizations** (CTE pre-filtering, atomic syncs, schema auto-heal, dynamic plugins, sqlglot AST protection, and the m3 setup wizard).
9.  **Enforce Resiliency & Concurrency Guards** (File descriptor semaphores, ChromaDB sync circuit breakers, OS Keyring DBus fail-safes, and Curation session pauses).
10. **Implement the Adaptive Background Workload Governor** (Cooperative task halting, active session cooldown gating, atomic unit checkpointing, **User-Selectable Resource Thresholds**, and pacing controls).
11. **Migrate Legacy Scheduled / Cron Tasks** (Transition `pg_sync.py`, `chatlog_embed_sweeper.py`, and `files_watch_once.py` to Governor-tracked loops).

---

## 🗺️ 1. Path Relocation & Directory Specifications

We are moving away from the single root (`~/.m3-memory/`) to a decoupled, clean standard structure supporting granular environment overrides.

### Directory Mapping & Overrides
```
                        [ Path Resolution ]
                         ↙              ↘
    [ Configuration files ]            [ Engine & Databases ]
    - Default: ~/.m3/config            - Default: ~/.m3/engine
    - Env Override: M3_CONFIG_ROOT     - Env Override: M3_ENGINE_ROOT
    - Master Override: M3_MEMORY_ROOT  - Master Override: M3_MEMORY_ROOT
```

*   **Config Root Resolution:**
    1.  Check `M3_CONFIG_ROOT` env var (absolute path).
    2.  Check `M3_MEMORY_ROOT` env var (resolve as `M3_MEMORY_ROOT + "/config"`).
    3.  Default to `~/.m3/config`.
*   **Engine Root Resolution:**
    1.  Check `M3_ENGINE_ROOT` env var (absolute path).
    2.  Check `M3_MEMORY_ROOT` env var (resolve as `M3_MEMORY_ROOT + "/engine"`).
    3.  Default to `~/.m3/engine`.

### Files to Migrate

| Legacy Path (`~/.m3-memory/`) | New Target Path | Type | Resolving Function in `m3_sdk.py` |
| :--- | :--- | :--- | :--- |
| `memory/.chatlog_config.json` | `~/.m3/config/.chatlog_config.json` | Configuration | `get_m3_config_root()` |
| `memory/.chatlog_state.json` | `~/.m3/engine/.chatlog_state.json` | State | `get_m3_engine_root()` |
| `memory/.chatlog_ingest_cursor.json`| `~/.m3/engine/.chatlog_cursor.json` | State | `get_m3_engine_root()` |
| `memory/.migrate_config.json` | `~/.m3/config/.migrate_config.json` | Configuration | `get_m3_config_root()` |
| `memory/agent_memory.db` | `~/.m3/engine/agent_memory.db` | Database | `resolve_db_path()` |
| `memory/agent_chatlog.db` | `~/.m3/engine/agent_chatlog.db` | Database | `resolve_db_path()` |
| `memory/chatlog_spill/` | `~/.m3/engine/chatlog_spill/` | Spill Directory | `get_m3_engine_root()` |
| `.agent_os_salt` | `~/.m3/config/.agent_os_salt` | Security | `get_m3_config_root()` |

---

## 🛠️ 2. M3 SDK Boundary Realignment & Oxidation Plan

To preserve the system-agnostic adapter architecture, we must clean up high-level leaks in `m3_sdk.py` and move core infrastructure tasks into it.

### 1. Elements to Remove from SDK (Out of Scope Gaps)
*   **`log_event()` Method:** Currently handles direct insertion of events into specific database tables like `activity_logs` and `project_decisions`.
    *   *Problem:* This leaks high-level tool knowledge (database table schemas) into the SDK.
    *   *Fix:* Move `log_event()` to a dedicated audit logging component (`bin/audit_trail.py`) or tool log manager. The SDK should only expose standard `get_sqlite_conn()` to allow writing.

### 2. Elements to Move TO the SDK (Infrastructural Unification)
*   **System Telemetry Unification:** Platform diagnostics (checking thermal load, CPU throttling, free RAM bounds, etc.) are currently scattered across maintenance tasks and doctor scripts. Unify these checks as standardized methods in the SDK (`M3Context.get_system_telemetry()`).
*   **Connection Timeout & Concurrency Controls:** All default HTTP client timeouts, semaphores (embedding concurrency caps), and PG pool thresholds must be managed centrally inside the SDK configuration, removing raw defaults from `embed.py` or `llm_failover.py`.

### 3. SDK Components to Oxidize (Rust Integration)
*   **`StructuredLogger` Formatting:** Formatting logging strings (`event | k=v`) represents a high-frequency Python string slicing action. Moving this to a Rust-backed logger provides zero-allocation format outputs.
*   **HTTP Circuit Breaker:** Moving the internal circuits tracker (`_CIRCUITS`) and timeout backoff calculations into Rust guarantees fast, thread-resilient, memory-safe connections under high concurrency.

---

## 🔒 3. Adversary-Hardened & Optimized Operational Guardrails

To mitigate vulnerabilities and boost performance across the board, we enforce six strict technical controls:

1.  **FIPS 140-3 Lockout Rule (Milestone 2):**
    If `M3_FIPS_MODE=1` is enabled but the `WOLFSSL` ctypes backend fails to load or fails its startup POST verification, the system **must fail-closed and abort execution**. Silent fallback to standard non-validated libraries is strictly prohibited.
2.  **Distributed Migration Lock File (Milestone 1):**
    Startup auto-migration must acquire an atomic exclusive lock file (`.migration.lock`) before performing SQLite copy operations. Concurrent startup threads must block-wait for the lock owner to complete the migration safely to avoid database corruptions.
3.  **Cohesion Table Validation (Milestone 1):**
    The engine database will record a system cohesion hash (representing the cryptographic salt and configuration metadata) inside a `m3_system_cohesion` metadata table. At startup, the SDK will re-verify the active configuration salt against this stored database hash, preventing silent mismatch when config and engine directories are decoupled.
4.  **SQLite CTE Pre-Filtering (Milestone 5):**
    Speed up hybrid search by pre-filtering candidate IDs (`user_id`, `scope`, `is_deleted`) inside SQLite using a Common Table Expression *before* computing vector similarity on the remaining subset (reducing candidates to `<50` rows, drop search P50 to `<1ms`).
5.  **Dynamic Plugin Architecture (Milestone 1):**
    Separate and eagerly load submodules (chatlog, files, entities) only on demand when registered via `tools_load_domain` or configured in the plugin profile, speeding up cold starts.
6.  **`sqlglot` AST SQL Injection Guard (Milestone 1):**
    Replace regex-based injection guards with the `sqlglot` parser. If content parsing reveals executable SQL AST nodes (like `Drop`, `Alter`, or `Delete`), the write is blocked with 100% precision.

---

## 🛡️ 4. Concurrency & Resiliency Guards

To prevent resource exhaustion and D-Bus blocking under server loads, we establish the following guards:

1.  **Ingestion FD Semaphore (Milestone 1):**
    Limit concurrent file-read operations during large directory walks to 32 parallel operations using `asyncio.Semaphore(32)` to avoid running out of OS file descriptors. Limit parallel chunk fact extractions using `asyncio.Semaphore(2)` to protect GPU memory.
2.  **ChromaDB Vector Sync Circuit Breaker (Milestone 3):**
    Implement a circuit breaker on `chroma_sync` batch uploads (threshold = 3 failures, cooldown = 120s). If ChromaDB goes down, the system immediately records pending uploads in the offline queue and fails-fast, preventing background worker thread hangs.
3.  **Keyring D-Bus Circuit Breaker (Milestone 1):**
    Querying the native OS vault can block indefinitely on headless systems with broken D-Bus contexts. We wrap keyring queries in a single-concurrency lock with a strict 2s timeout. If lookups timeout, open the Keyring Circuit Breaker for 300s and fall back directly to the local encrypted AES-256 vault.
4.  **Curation Activity Semaphore (Milestone 1):**
    Auto-maintenance (e.g., vector deduplication or db sweeps) will periodically probe the shared volatile `_LAST_ACTIVE_QUERY_TIME`. If an active query occurred within the last 15 seconds, the maintenance thread will sleep and yield resource and database lock priority to the user's active session.

---

## 🧠 5. Adaptive Background Workload Governor & Task Migrations

To continuously run curation, coalescing, and cognitive loops without impacting user interaction, we implement the **Adaptive Governor**:

1.  **Cooperative Task Halting (Milestone 1):**
    Jobs are split into small, discrete, checkpointed **Stateful Work Units** (e.g. processing exactly 5 items). Every worker loops checks `cancellation_event.is_set()` before beginning the next unit. Worst-case latency to halt background jobs is `<150ms`.
2.  **Active Session Cooldown (Milestone 1):**
    Any user interaction registers a volatile timestamp `_LAST_USER_INTERACTION_TIME` in the SDK.
    *   **Active Window (0-30s):** Halted Mode (background workers suspend completely).
    *   **Grace Window (30-60s):** Tapered Mode (workers run a single unit and sleep 5s, pacing CPU).
    *   **Idle Window (60s+):** Continuous Mode (workers run sequential units with a minor 100ms throttle).
3.  **User-Selectable Resource Thresholds (Milestone 1):**
    Allows user tuning of resource limits via environment variables:
    *   `M3_GOVERNOR_INITIAL_THRESHOLD` (Default: `85%`): Load at which **background tasks** step back by injecting a **5s to 10s sleep delay** between atomic units (allowing the host to breathe).
    *   `M3_GOVERNOR_LIMIT_THRESHOLD` (Default: `95%`): Load at which **interactive single-processes** step back by injecting a **30s to 60s sleep delay** between tool runs to cool down the GPU and system.
    *   *Rule Constraint:* $\text{Initial} < \text{Limit}$.
    *   *Override:* If `M3_GOVERNOR_LIMIT_THRESHOLD = 100`, interactive processes never throttle.
    *   *General Bounds:* Stop background tasks if telemetry reports CPU > 50%, RAM > 80%, or thermal loads in `Serious`/`Critical` states.
4.  **Schedules Adaptation (Milestone 1):**
    Remove static time-based crons for resource-heavy operations (`bin/pg_sync.sh`, `chatlog_embed_sweeper.py`, `files_watch_once.py`). Register them as Governor-tracked loops:
    *   *PgSync:* Syncs in atomic 100-row chunks inside SQLite-to-PG transaction blocks.
    *   *ChatLog Sweeper:* Embeds in atomic 5-row chunks only when GPU status is `Nominal`.
    *   *Filesystem Watcher:* Scans folders incrementally (10 file nodes per unit), saving cursors to `files.db`.

---

## 👥 6. Sub-Agent Execution Guide (Parallel Workstreams)

To optimize implementation speed and guarantee maximum code quality, we will delegate parts of this plan to specialized sub-agents. Below are the **hardened, step-by-step instructions** for each sub-agent.

```
                   ┌──────────────────────────────────────┐
                   │    M3-v3 Master Orchestrator (Parent)│
                   └──────────────────┬───────────────────┘
          ┌───────────────────────────┼───────────────────────────┐
┌─────────▼─────────┐       ┌─────────▼─────────┐       ┌─────────▼─────────┐
│Path Engineer (Sub)│       │FIPS Specialist(Sub)       │Rust Oxidizer (Sub)│
│- Path Decoupling  │       │- strict wolfCrypt │       │- Traversal Crate  │
│- Lock Files & Cohesion│   │- Abort on Fallback│       │- Fast SDK Logging │
└───────────────────┘       └───────────────────┘       └───────────────────┘
```

---

### 🛡️ Sub-Agent A: FIPS Security Specialist
*   **Role:** Security Engineer focused strictly on FIPS 140-3 cryptography boundaries.
*   **Prompt Task:** Implement and validate the full `wolfCrypt` encryption, decryption, and hashing bindings in `bin/crypto_provider.py`.

#### Hardened Execution Steps:
1.  **Study Abstractions:** Read [crypto_provider.py](file:///bin/crypto_provider.py) and [test_fips_integrity.py](file:///bin/test_fips_integrity.py).
2.  **wolfCrypt bindings Implementation:**
    *   Initialize the `ctypes` mappings for `wc_AesGcmSetKey`, `wc_AesGcmEncrypt`, and `wc_AesGcmDecrypt` from the loaded `libwolfssl` handle.
    *   Replace the `encrypt` and `decrypt` placeholder methods in the `CryptoProvider` class. Ensure they execute FIPS AES-256-GCM authenticated encryption and raise `RuntimeError` on failure (rather than falling back to standard `cryptography`).
3.  **Strict compliance Enforcement:**
    *   Harden `sha256` to raise an error if `M3_FIPS_MODE=1` is set but the validated `wolfcrypt.sha256` module cannot be loaded (preventing silent fallback to standard library `hashlib` under strict FIPS mode).
    *   If `M3_FIPS_MODE=1` is configured but `WOLFSSL` failed to initialize, raise a fatal `RuntimeError` at startup to abort execution immediately.
4.  **Integrity Checks:**
    *   Run `python bin/test_fips_integrity.py` with `M3_CRYPTO_BACKEND=WOLFSSL` and `M3_FIPS_MODE=1` enabled. Verify that all tests pass without error.
5.  **Exit Criteria:** `test_fips_integrity.py` passes 100% using the `WOLFSSL` backend, and no active stubs remain in `crypto_provider.py`.

---

### 📂 Sub-Agent B: Path, SDK, Resiliency & Governor Engineer
*   **Role:** Storage Infrastructure, Database Migration, Resiliency Guard and Governor Engineer.
*   **Prompt Task:** Refactor paths, implement lock files, build cohesion tables, add AST guards, dynamic plugins, design keyring/ingestion guards, build the Adaptive Governor (with user-selectable thresholds) and task migrations, and upgrade `homecoming.py`.

#### Hardened Execution Steps:
1.  **Path Resolution Refactor:**
    *   Open [m3_sdk.py](file:///bin/m3_sdk.py).
    *   Add `get_m3_config_root()` and `get_m3_engine_root()` functions using the lookup precedence specified in Section 1.
    *   Refactor `resolve_db_path()` and `M3Context` to default to these new root folders.
2.  **SDK Boundary Realignment:**
    *   Identify the `log_event()` method in `m3_sdk.py`.
    *   Extract it entirely to [bin/audit_trail.py](file:///bin/audit_trail.py), replacing call sites across the codebase with the new audit-module wrapper.
    *   Unify system-level telemetry (checking thermal logs, RAM, and CPU throttling) inside `m3_sdk.py` as a centralized helper function.
3.  **Locks, Cohesion, AST & Resiliency/Governor Implementations:**
    *   Implement an **exclusive atomic lock file** (`.migration.lock`) logic inside `m3_sdk.py` during startup auto-migrations. Block concurrent initialization threads until migration is complete.
    *   Create the `m3_system_cohesion` database table and implement the salt/schema validation check at startup. Abort boot-up if a mismatch is detected, preventing configuration drift across decoupled directories.
    *   Implement the **`sqlglot` AST injection guard** inside `util.py` / `_check_content_safety()`, replacing regex checks.
    *   Decouple eagerly loaded domains into a **Dynamic Plugin Architecture** loaded only when domains are requested.
    *   Integrate **Ingestion FD Semaphores** (`asyncio.Semaphore(32)`) in `files_memory/ingest.py`.
    *   Wire the **Keyring D-Bus Circuit Breaker** with a 2-second timeout inside `m3_sdk.py` vault resolutions, falling back to local `synchronized_secrets` on timeout.
    *   Implement **Curation Activity Semaphores** in `memory_maintenance.py` to yield priority to queries.
    *   Write the **Adaptive Background Workload Governor** (active user detection, user-selectable `M3_GOVERNOR_INITIAL_THRESHOLD` and `M3_GOVERNOR_LIMIT_THRESHOLD` checks with 5-10s / 30-60s pacing delays, and task migrations for `pg_sync.py`, `chatlog_embed_sweeper.py`, and `files_watch_once.py`) in `m3_sdk.py` and `memory_maintenance.py`.
4.  **Homecoming Upgrade:**
    *   Modify [homecoming.py](file:///bin/homecoming.py) to read from the old `~/.m3-memory/` root as the source and copy databases/configurations to `~/.m3/config` and `~/.m3/engine` cleanly.
5.  **Verification:**
    *   Run `python bin/homecoming.py` manually to verify the dry-run and actual migration copy.
6.  **Exit Criteria:** All database connections (`M3Context.for_db`) and settings file reads resolve to the new directories. Existing tests pass cleanly.

---

### 🦀 Sub-Agent C: Rust Oxidizer
*   **Role:** Rust Systems performance developer.
*   **Prompt Task:** Expand the Rust core (`m3-core-rs`) with multi-hop graph traversals, token sanitization, and structured fast logging.

#### Hardened Execution Steps:
1.  **Graph Oxidation (`m3-graph-rs`):**
    *   Establish a new crate `m3-graph-rs` inside the Rust workspace.
    *   Implement BFS traversal logic using the `petgraph` crate. The function must take an adjacency matrix or list of relationships and perform depth searches up to 3 hops, returning a flat list of node IDs.
    *   Bind this structure to Python using PyO3.
2.  **Lexical Tokenizer (`m3-fts-rs`):**
    *   Create a simple query string sanitizer in Rust. Strip operators (`AND`, `OR`, `NOT`, `NEAR`), parse quotes, and format speaker tokens (e.g. prepending `[Role]` keys).
    *   Bind this sanitizer to PyO3 to replace `_sanitize_fts` in `bin/memory/fts.py`.
3.  **SDK Logger & PyO3 In-Place Slicing:**
    *   Oxidize the `StructuredLogger.format()` string formatting routine into a fast, zero-allocation Rust logging utility inside `m3-core-py`.
    *   Enable PyO3 `&PyString` slicing on the Python heap in the `Redactor` block to avoid copy-allocating memory during massive log redactions.
4.  **Compilation & Integration:**
    *   Compile the Rust crate locally using `maturin develop`.
    *   Update [bin/memory/search.py](file:///bin/memory/search.py) to import and call these Rust bindings when `M3_CORE_RS_DISABLE` is false.
5.  **Exit Criteria:** `tests/bench_oxidation.py` runs successfully, proving that graph traversal and lexical tokenization are at least **5× faster** than their legacy Python implementations.

---

## 📅 7. Consolidated To-Do List & Session Roadmap

This project represents the **M3-v3 Lifecycle**. It is organized into 5 developmental milestones across multiple sessions.

### Milestone 1: Path Decoupling, SDK Realignment & Hardened Startup ✅ COMPLETE
- [x] Create `get_m3_config_root()` and `get_m3_engine_root()` in `bin/m3_sdk.py`
- [x] Extract `log_event()` out of `m3_sdk.py` into `bin/audit_trail.py`
- [x] Unify hardware telemetry checks in `m3_sdk.py`
- [x] Implement atomic lock file (`.migration.lock`) for safe startup auto-migration
- [x] Create the `m3_system_cohesion` validation table and enforce salt check at system boot
- [x] Replace regex injection checks in `util.py` with `sqlglot` AST parsing logic
- [ ] Implement the Setup Wizard terminal interface (`m3 setup`)
- [ ] Refactor eagerly loaded tool submodules into the Dynamic Plugin Architecture
- [x] Implement Ingestion FD Semaphores (`asyncio.Semaphore(32)`) in the ingest engine
- [x] Implement Keyring D-Bus Circuit Breaker (2-second timeout) inside vault resolution checks
- [x] Implement Curation Activity Semaphores in `memory_maintenance.py` to yield database lock priority
- [x] Add interaction hook `register_user_interaction()`, system telemetry metrics, and `get_governor_pacing()` (with user-selectable `M3_GOVERNOR_INITIAL_THRESHOLD` and `M3_GOVERNOR_LIMIT_THRESHOLD` checks, 5-10s / 30-60s pacing delays, and 100% limit overrides) to `m3_sdk.py` for Adaptive Governor control
- [x] Refactor `bin/install_schedules.py` to migrate time-based crons (`pg_sync.py`, `chatlog_embed_sweeper.py`, `files_watch_once.py`) to Governor-tracked loops
- [x] Update database and config resolutions in `m3_sdk.py`, `chatlog_core.py`, and `sqlite_pragmas.py`
- [x] Refactor `bin/homecoming.py` to support `~/.m3/config` and `~/.m3/engine` migration path
- [x] Integrate startup auto-migration check in `m3_sdk.py`
- [x] Run test suite to verify no regressions in basic setups

### Milestone 2: Hardening the FIPS Boundary ✅ COMPLETE
- [x] Implement full ctypes bindings to wolfSSL (`wc_AesGcmEncrypt`/`wc_AesGcmDecrypt`) in `crypto_provider.py`
- [x] Standardize the SHA-256 verification to fail-closed under `M3_FIPS_MODE=1` if wolfCrypt is missing
- [x] Enforce the FIPS Abort Lockout startup check to abort execution if initialization fails
- [x] Verify bindings using `tests/test_fips_integrity.py` (14/14 tests pass)
- [x] Integrate FIPS-hardened TLS 1.3 Context as the primary client context in `m3_sdk.py`

### Milestone 3: Sovereign Cloud Failover, ChromaDB Circuit Breakers & PII Redaction ✅ COMPLETE
- [x] Create Tier 4 Cloud endpoint configurations in `bin/memory/config.py` and `bin/memory/embed.py`
- [x] Write integration block for `chatlog_redaction.py` to scrub parameters before sending to cloud
- [x] Implement ChromaDB Sync Endpoint Circuit Breaker (3 failures, 120s cooldown) in vector pushes
- [x] Wire the local semantic/Ollama cache fallback if the cloud failover experiences high load or latency spikes

### Milestone 4: Rust Crate Expansion & SDK Oxidation 🚧 IN PROGRESS
- [x] Set up `m3-graph` and `m3-fts` crates with full implementations and unit tests
- [x] Expose `GraphIndex`, `CircuitBreaker`, `RetryPolicy` as PyO3 bindings in `m3-core-py`
- [x] Expose `sanitize_fts`, `compile_fts_query` as PyO3 bindings in `m3-core-py`
- [x] Oxidize the `StructuredLogger` helper block to achieve zero-allocation log printing (`format_log`)
- [x] Enable in-place heap slicing on `&PyString` in the compiled Rust `Redactor` block
- [x] Implement Fast-BFS traversal in Rust via `m3-graph` and link to `graph.py` in memory search
- [x] Implement FTS5 sanitizer in Rust via `m3-fts` and replace python fts parser in `fts.py`
- [x] Add rayon data-parallel acceleration to `mmr_rerank` and `mmr_rerank_scored` in `m3-vector`
- [x] Oxidize the Adaptive Governor cooldown state and user thresholds checks inside `m3-core-py` (crate `m3-governor`, `m3_core_rs.Governor`)
- [x] Oxidize the incremental directory walk and hash checks of the Filesystem Watcher in `m3-ingest` (`m3_core_rs.fs_walk` / `hash_files`)
- [~] Add async batch writing queues in `db.py` to scale concurrent write performance — **REVERTED after benchmarking.** An in-process `WriteQueueDaemon` was prototyped and reverted: its 100ms aggregation window only adds latency to the intra-process path (SQLite WAL on the single pooled `_db()` connection already commits 200 rows in ~16ms), and the `database is locked` storm it targeted is a *multi-process* phenomenon an in-process queue cannot coordinate. m3 already handles that case: `PRAGMA busy_timeout=30000` drives lock-retries to zero, and the existing `memory_write_bulk_impl` / `memory_write_batch_impl` batch commits (~50× faster than per-row under contention). See `v3/m3_v3_phase_c_rust_oxidation_plan.md` benchmark note.
- [~] Rebuild Rust wheel and publish — **local CUDA rebuild + install DONE.** `m3-core-rs-windows-cuda` 3.6.22 (cp314, `backend=cuda`, with `Governor`/`fs_walk`/`hash_files`) built via `build_local.py cuda` and installed into system Python, upgrading from 3.6.6; native governor parity test now runs+passes live. **PyPI/GitHub-release publish still pending user go-ahead** (outward-facing).

### Milestone 5: Pre-Compiled Infrastructure & CTE Filters 🚧 IN PROGRESS
- [x] Implement CTE pre-filtering in `bin/memory/search.py` semantic mode (drops to <50 before vector calc)
- [ ] Integrate `m3 doctor --fix` quick repair hooks
- [ ] Create GitHub Actions workflow to build and publish wheel binaries (`m3-core-py`)
- [ ] Compile and distribute local SQLite extensions (`sqlite-vec`) for native SQLite vector computation
- [ ] Re-run the entire retrieval baseline (`tests/capture_retrieval_baseline.py`) and benchmarks (`bench_memory.py`) to verify optimization budgets

