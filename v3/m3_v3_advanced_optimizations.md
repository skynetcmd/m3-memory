# 🚀 Project M3-v3: Advanced System Optimizations

This document presents a comprehensive review of the **M3-v3 Master Implementation Plan** and proposes deep optimizations to maximize the system's **Speed**, **Reliability**, **Robustness**, **Modularity**, **Security**, and **User Experience (UX)**.

---

## ⚡ 1. Speed (Low-Latency Engineering)

```
                            [ Query Search ]
                                   │
                                   ▼
                   [ CTE Pre-Filtering (user/scope) ]
                   - Done inside SQLite Index first
                                   │
                                   ▼
                       [ Shrink Candidate Pool ]
                       - Reductions to <50 rows
                                   │
                                   ▼
                   [ Compute Vector Similarity ]
                   ⚠️ ELIMINATES SCANNING 500+ BLOBs
```

*   **SQLite CTE Pre-Filtering:** Currently, the search engine computes vector similarity across all rows (`SEARCH_ROW_CAP = 500`) and then applies filters (`user_id`, `scope`, `is_deleted`).
    *   *Optimization:* Implement **Index Pre-filtering as a Common Table Expression (CTE)**. Filter the candidate pool down to matching scopes and users *first* using SQLite partial indices, and then execute vector cosine similarity on the remaining subset (typically `<50` rows). This slashes P50 retrieval latency to **<1ms**.
*   **In-Place PyO3 Slicing:**
    *   *Optimization:* When passing massive text chunks to the Rust `Redactor` block for scrubbing, avoid copy-allocating standard Python strings into native Rust `String` objects (which triggers dynamic UTF-8 validations). Instead, leverage **PyO3 `PyString` references (`&PyString`)** to read and slice data directly on the Python heap, achieving zero-allocation token scrubbing.

---

## 🛡️ 2. Reliability (Resiliency & Self-Healing)

*   **Atomic Sync Transactions:**
    *   *Optimization:* The PgSync delta synchronization protocol (`pg_sync.py`) currently updates watermarks non-atomically with data writes, which can lead to duplicates or drift on network crashes. We will wrap the data write, delta verification, and watermark updates in a **single atomic transaction** spanning both SQLite and PostgreSQL. If any step fails, the entire sync delta rolls back safely.
*   **Heuristic TF-IDF Embedding Fallback:**
    *   *Optimization:* If the local GGUF embedder (Tier 1) and local servers (Tier 2) are completely offline, the system typically crashes. We will introduce a local **Heuristic TF-IDF / BM25 Bag-of-Words fallback embedder**. During total service outages, the system degrades gracefully into keyword-similarity vector approximations rather than failing entirely.

---

## 🏗️ 3. Robustness (Contract & Schema Safety)

*   **CI Typing Enforcement:**
    *   *Optimization:* Enforce strict typing compilation inside the CI pipeline (`.github/workflows/ci.yml`) using `mypy --disallow-untyped-defs` specifically for the new `bin/memory/` and `bin/crypto_provider.py` modules. This ensures signature drifting is caught before merging PRs.
*   **Schema Auto-Healing Upgrade:**
    *   *Optimization:* During system startup, if the cohesion validation checks identify missing columns or support structures that were added in minor version updates (non-destructive changes), the SDK will **auto-heal** the database by running silent upgrades, removing the need for manual CLI migration commands on minor updates.

---

## 🧩 4. Modularity (Plugin Architecture)

*   **Dynamic Domain Plugin Loading:**
    *   *Optimization:* Currently, `mcp_tool_catalog.py` eagerly imports all tool domains (such as `files`, `chatlog`, and `entity`) at module evaluation. If a user only needs basic memory features, they still pay the cold-start and memory costs of loading every module.
    *   *Design:* Transition the catalog to a **Dynamic Plugin Architecture**. Submodules are only imported and registered when requested via the `tools_load_domain` tool or configured in `m3-server.json`. This minimizes memory footprint and isolates errors to active plugins.

---

## 🔒 5. Security (AST Guardrails & Rotation)

*   **AST-Layer SQL Injection Prevention:**
    *   *Optimization:* Instead of executing standard regex patterns to prevent SQL injection in `_check_content_safety()` (which can be bypassed by creative whitespace or comments), we will parse incoming content using the **`sqlglot` AST parser** (which is already pinned in our dependencies). If `sqlglot` identifies executable SQL nodes (like `Drop`, `Alter`, or `Delete` branches), the write is rejected with 100% accuracy.
*   **Graceful Salt Key Rotation:**
    *   *Optimization:* Cryptographic salts (`.agent_os_salt`) currently live indefinitely. We will implement a **Multi-Key Salt Rotation Protocol**. When a new salt is generated, subsequent writes are encrypted with the new key, while the `m3_system_cohesion` table retains older salt hashes on a key-ring stack to decrypt legacy secrets on-the-fly.

---

## 🎨 6. User Experience (UX & Developer Tooling)

```
                            [ m3 setup ]
                                 │
                 ┌───────────────┼───────────────┐
                 ▼               ▼               ▼
          [ Auto-Probe ]  [ Keyring Setup ]  [ Path Config ]
          - LLM / GGUF    - AES-256 Vault    - config / engine
                 └───────────────┬───────────────┘
                                 ▼
                    [ Ready in <10 seconds! ]
```

*   **Interactive Setup Wizard (`m3 setup`):**
    *   *Optimization:* Create an interactive, terminal Setup Wizard that automates environment setup. It will:
        *   Auto-probe local LLM servers and GGUF backends.
        *   Test OS Keyring support.
        *   Generate the default `~/.m3/config/.env` environment file.
        This provides a secure, fully-validated installation state in under 10 seconds.
*   **Doctor Quick-Fix Command (`m3 doctor --fix`):**
    *   *Optimization:* When the diagnostics suite (`m3 doctor` or `memory_doctor`) identifies a degraded tier (such as an offline Tier-2 embedder server), it will provide a direct **Quick-Fix Command** (e.g. `m3 doctor --fix`) to automatically repair, rebuild indexes, or spin up local services on-the-fly.
