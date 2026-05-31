# 🏁 Project M3-v3: Milestone 1 Technical Walkthrough

We have successfully completed and validated **Milestone 1** of Project M3-v3. This represents a major system upgrade, focusing on directory decoupling, operational hardening, resource-responsive pacing, and modular code isolation.

Below is a detailed technical summary of what has been built, hardened, and verified.

---

## 🛠️ 1. Path Decoupling & Directory Migration
We decoupled configurations and engine databases to enforce clean separation of concerns and allow granular overrides:
*   **Directory Mapping:**
    *   **Configuration Directory:** Resolved via `get_m3_config_root()` with the precedence `M3_CONFIG_ROOT` > `M3_MEMORY_ROOT/config` > `~/.m3/config`.
    *   **Engine & Database Directory:** Resolved via `get_m3_engine_root()` with the precedence `M3_ENGINE_ROOT` > `M3_MEMORY_ROOT/engine` > `~/.m3/engine`.
*   **Decoupled Homecoming Script:** Upgraded [homecoming.py](file:///bin/homecoming.py) to automatically identify legacy databases, config files, and cryptographic salts in both repo-relative `memory/` folders and old `~/.m3-memory/` directories, and copy them safely to their new decoupled standard roots.

---

## 🚦 2. Adaptive Background Workload Governor
We built cooperative pacing gates inside the SDK and daemon loops:
*   **Interactive Pacing Checks:** Added `pre_execute_interactive_check()` to throttle interactive tools when system loads exceed user-selectable thresholds.
*   **User-Selectable Resource Thresholds:**
    *   `M3_GOVERNOR_INITIAL_THRESHOLD` (Default: `85%`): Load above which background tasks pace themselves with a **5s to 10s delay**.
    *   `M3_GOVERNOR_LIMIT_THRESHOLD` (Default: `95%`): Load above which interactive tasks throttle by **30s to 60s**. Supports 100% overrides (no stepbacks).
    *   *Constraint Rule:* $\text{Initial} < \text{Limit}$ is strictly enforced.
*   **Cognitive Loop Gating:** Refactored [m3_cognitive_loop.py](file:///bin/m3_cognitive_loop.py) to automatically pause and yield computing priority to the user's active session during active conversation windows.

---

## 🔒 3. Hardened Security & Resilience Guards
We implemented five technical safeguards to prevent deadlocks, data corruption, and bypasses:
1.  **Distributed Migration Lock:** Startup migrations now acquire an atomic exclusive lock file (`.migration.lock`) inside `bin/memory/db.py` to prevent concurrent setup threads from deadlocking or corrupting SQLite databases.
2.  **Decoupled Cohesion Validation:** Created a metadata validation table `m3_system_cohesion` in the engine. It verifies on boot that the active configuration salt matches the database salt hash, failing-loud if they drift.
3.  **`sqlglot` AST SQL Injection Guard:** Replaced basic regex injection matches in [util.py](file:///bin/memory/util.py) with full `sqlglot` AST parsing logic. Any write attempting `Drop`, `Delete`, or `Alter` SQL statements is blocked at the boundary.
4.  **Keyring D-Bus Circuit Breaker:** Wrapped standard OS keyring lookups in [auth_utils.py](file:///bin/auth_utils.py) with a strict **2-second thread timeout** and a **300s open-circuit cooldown**, falling back to local encrypted secrets on headless or misconfigured systems.
5.  **Curation Activity Semaphores:** Automated de-duplication and decay passes in [memory_maintenance.py](file:///bin/memory_maintenance.py) check active session timestamps and yield DB lock priority during active conversations.

---

## 🦀 4. Dynamic Plugin Architecture (Cold-Start Oxidation)
*   **The Leak:** Previously, heavy submodules (`chatlog_core`, `memory_core`, `files_memory.tools`, etc.) were eagerly imported at startup, leading to large context wire footprints and slow cold starts.
*   **The Fix:** Transitioned the MCP catalog inside [mcp_tool_catalog.py](file:///bin/mcp_tool_catalog.py) to a lazy proxy plugin architecture. Submodules are imported and registered on demand *only* when the agent requests them via `tools_load_domain` or if they are configured in the active profile.

---

## 🚀 5. Verification & Test Suite Success
All newly implemented changes have been validated against the extensive test suite. All tests are passing:
*   `test_content_safety.py`: **100% Passed** (validates the new `sqlglot` AST SQL injection guard).
*   `test_audit_trail.py`: **100% Passed** (validates the extracted event logging).
*   `test_embed_cascade_order.py` & `test_embed_key_enricher.py`: **100% Passed**.
*   `test_doctor.py` & `test_elbow_trim.py`: **100% Passed**.

> [!NOTE]
> Milestone 1 is completely implemented, hardened, and verified without any regressions. The architecture is fully set up for the next milestones (Milestone 2 FIPS backends, Milestone 3 Sovereign Cloud Failover).
