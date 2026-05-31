# 🛡️ Project M3-v3: Resiliency & Concurrency Guards

This document evaluates the M3 Memory operational architecture and identifies four critical processes that would benefit from **Resource Semaphores** (concurrency constraints) and **Circuit Breakers** (outage fail-fast patterns) to prevent system deadlocks, CPU thrashing, and OS-level descriptor exhaustion.

---

## 📊 Summary of Proposed Resiliency Guards

| Target Process | Guard Type | Resource it Protects | Action on Threshold Breach |
| :--- | :--- | :--- | :--- |
| **1. File Ingestion Pipeline** | OS Descriptor Semaphore | Host system file descriptors & local GPU memory during large folder sweeps. | Caps concurrent file reads at 32; queues LLM facts extraction at 2. |
| **2. ChromaDB Sync Engine** | Vector Sync Circuit Breaker | Background worker threads and database pools from connection timeouts during ChromaDB outages. | Opens circuit after 3 failed sync batches; immediately writes to local fallback mirrors for 120s. |
| **3. Keyring Vault Resolution** | Keyring Lock & D-Bus Circuit Breaker | Blocks threads from freezing on unresponsive native OS Keyring daemons (e.g. headless Linux D-Bus). | Sets a 2s timeout gate; opens the circuit to fall back to the local AES-256 database vault directly. |
| **4. Auto-Curation Engine** | Maintenance Execution Semaphore | database lock priority and CPU cycles during active user query sessions. | Automatically suspends resource-heavy vector de-duplication if active query threads are executing. |

---

## 🛠️ Detailed Implementation Designs

### 1. Ingestion File Descriptor Semaphore (`asyncio.Semaphore`)
When walking massive codebases or folder libraries during `files_ingest`, spawning hundreds of concurrent file reads inside an async event loop can exhaust the operating system's native **file descriptors** (triggering a fatal `OSError: [Errno 24] Too many open files`).

*   **Design:** Implement a dual-semaphore gate inside `files_memory/ingest.py`:
    *   `_INGEST_FD_SEM = asyncio.Semaphore(32)`: Restricts parallel raw file-read tasks on disk.
    *   `_INGEST_LLM_SEM = asyncio.Semaphore(2)`: Restricts concurrent chunk-fact extractions to prevent local LLM server saturation.
*   **Integration:**
    ```python
    async def process_file_node(path):
        async with _INGEST_FD_SEM:
            content = await read_file_async(path)
            
        if extract_mode_enabled:
            async with _INGEST_LLM_SEM:
                await extract_facts_via_llm(content)
    ```

### 2. ChromaDB Vector Sync Circuit Breaker
The system synchronization protocol (`chroma_sync`) pushes bulk batches of local SQLite vector embeddings to a remote ChromaDB server. If the remote server goes offline or has network issues, repeated push attempts block background sync threads, clogging CPU cycles and logging repetitive tracebacks.

*   **Design:** Introduce a dedicated **ChromaDB Endpoint Circuit Breaker** inside `chroma.py`.
    *   **Threshold:** 3 consecutive push request failures.
    *   **Cooldown:** 120 seconds.
*   **Integration:**
    When the circuit is **OPEN**, the sync engine immediately halts active network attempts, writes pending items directly to `chroma_sync_queue` with a status of `stalled`, and reads from the local `chroma_mirror` cache. This preserves offline, sovereign capabilities without blocking system performance.

---

## 🔑 3. Keyring D-Bus Circuit Breaker
To load FIPS master keys, `auth_utils.py` uses the standard Python `keyring` library to query native OS vaults. In headless Linux environments (like Docker containers or SSH-only servers), the underlying **D-Bus / SecretService daemon** is often unconfigured or locked.
*   *The Problem:* Calling `keyring.get_password()` on an unresponsive D-Bus connection blocks the calling thread for a massive, unconfigurable **30-second timeout** per call, locking up the entire agent bridge.

*   **Design:** Wrap keyring lookups in a single-concurrency lock with a strict **2-second timeout** and a dedicated circuit breaker.
*   **Integration:**
    If a lookup times out or fails twice:
    1. Open the Keyring Circuit Breaker for 300 seconds.
    2. Fall back directly to resolving credentials using the local AES-256 vault (`synchronized_secrets` table) with the local decryption salt.
    3. Prevents unresponsive system daemons from halting execution.

---

## 🧹 4. Curation Activity Semaphore (Active Session Detection)
Automated database maintenance (`memory_maintenance.py`), such as running heavy vector de-duplications (`memory_dedup`) or database vacuums, consumes significant CPU cycles and creates database write locks. If these jobs execute while an agent is performing high-speed retrieval, the user experience suffers from latency spikes.

*   **Design:** Implement a **Cooperative Activity Semaphore / Lock** inside `memory_maintenance.py`.
    *   **Mechanism:** Maintain a volatile, atomic timestamp counter `_LAST_ACTIVE_QUERY_TIME` inside the SDK shared state.
    *   **Integration:**
        Before executing any chunk of a vector curation pass, the background worker checks:
        ```python
        # Yield to user if an active query occurred in the last 15 seconds
        if time.time() - M3Context.get_last_query_time() < 15.0:
            logger.info("Active query session detected. Suspending curation pass to yield resources.")
            await asyncio.sleep(5.0) # Yield execution window
        ```
        *This guarantees database lock priority and computing resources are reserved entirely for active agent conversations.*
