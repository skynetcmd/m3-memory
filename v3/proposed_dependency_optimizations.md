# 🚀 Proposed Dependency Optimizations for M3 Memory

To optimize the M3 Memory system more thoroughly, we can introduce specific C/Rust-accelerated Python libraries into `requirements.txt` and `pyproject.toml`. These libraries directly address bottlenecks in **JSON parsing**, **async event loop overhead**, **vector math integration**, and **FIPS-validated cryptographic primitives**.

---

## 📊 Summary of Optimization Candidates

| Library | Category | What it Optimizes | Estimated Gain | Alignment with Tenets |
| :--- | :--- | :--- | :--- | :--- |
| **`orjson`** | JSON Serialization | Metadata parsing, `chatlog.db` ingestion, configuration loading, and JSON IPC in the MCP server. | **3× – 10× faster** JSON serialization/deserialization. | **Efficiency & Perf:** Cuts CPU overhead on JSON streaming paths. |
| **`uvloop`** | Async Event Loop | Replaces `asyncio` event loop with a `libuv`-based loop. Speeds up the MCP Stdio/HTTP bridge and proxy. | **2× – 4× faster** async event loop dispatching. | **Performance:** Improves throughput and reduces tail latency under high load. |
| **`sqlite-vec`** | Vector Database | Direct vector cosine distance computations inside SQLite queries via a lightweight C extension. | **50× – 100× faster** candidate scoring than NumPy/pure-Python. | **Performance & Efficiency:** Prevents pulling 500+ rows to Python. |
| **`apsw`** | Database Wrapper | Direct SQLite C API access, custom memory allocators, and direct virtual table mapping. | **1.2× – 1.5× faster** database transactions. | **Robustness & Performance:** Strict error handling + low FFI overhead. |
| **`tokenizers`** | Text Processing | Fast, exact byte-pair tokenization for sliding window chunking and exact billing/cost tracking. | **100× faster** than regex-based token estimation. | **Effectiveness:** Guarantees token counting accuracy offline. |

---

## 🛠️ Detailed Integration Plan

### 1. `orjson` — High-Performance JSON Engine
Currently, M3 parses JSON metadata and log collections using the standard library `json` module. When dealing with bulk turn curation (`curate_chatlog_apply`) or deep directory indexing (`files_ingest`), JSON serialization represents a measurable slice of CPU runtime.

*   **Why `orjson`?** It is written in Rust, parses UTF-8 directly, and natively handles `dataclasses`, `datetime` objects, and `numpy` arrays (which are standard across our vectors).
*   **Integration:**
    ```python
    # In bin/memory/util.py or as a global utility
    try:
        import orjson
        json_dumps = lambda x: orjson.dumps(x).decode('utf-8')
        json_loads = orjson.loads
    except ImportError:
        import json
        json_dumps = json.dumps
        json_loads = json.loads
    ```

### 2. `uvloop` — Ultra-Fast Event Loop for Asyncio
The MCP stdio connection and `mcp_proxy.py` run continuously in an asynchronous event loop. 

*   **Why `uvloop`?** Written in Cython and wrapping `libuv`, it makes `asyncio` performance match that of Node.js and Go.
*   **Integration:**
    Simply inject the loop initialization at the entry points of `bin/memory_bridge.py` and `bin/mcp_proxy.py`:
    ```python
    import sys

    if sys.platform != "win32":
        try:
            import uvloop
            uvloop.install()
        except ImportError:
            pass
    ```
    *(Note: Windows is excluded as `uvloop` does not support Windows natively; standard `asyncio.ProactorEventLoop` remains the Windows default).*

### 3. `sqlite-vec` — Native SQLite Vector Operations
Currently, M3 performs vector similarity by reading packed float32 BLOBs out of SQLite and computing cosine distances in Python (`numpy` or `numpy` FFI fallback). This forces us to cap the search rows (`SEARCH_ROW_CAP = 500`) to bound memory and CPU overhead.

*   **Why `sqlite-vec`?** It is an extremely lightweight, zero-dependency SQLite extension written in C. It introduces native vector types and distance functions (`vec_distance_cosine`) directly into the SQLite SQL dialect.
*   **Integration:**
    We load the extension at SQLite connection time in `bin/sqlite_pragmas.py` / `apply_pragmas`:
    ```python
    import sqlite_vec
    
    def apply_pragmas(conn):
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    ```
    Then, the vector query in `bin/memory/search.py` simplifies to a single SQL query:
    ```sql
    SELECT memory_id, vec_distance_cosine(embedding, ?) AS score 
    FROM memory_embeddings 
    ORDER BY score ASC 
    LIMIT ?;
    ```
    *This eliminates NumPy and the entire intermediate memory-allocation layer entirely, keeping vector scanning 100% within the C-boundary.*

### 4. `apsw` (Another Python SQLite Wrapper)
If the project needs to go even deeper into SQLite performance than standard `sqlite3` allows, `apsw` provides a thin, complete C wrapper around SQLite.

*   **Why `apsw`?** It maps SQLite error codes perfectly (supporting **Robustness / Fail Loud**), executes statements up to 15% faster than `sqlite3` due to minimal FFI layer, and allows Python to define custom virtual tables and collations with zero boilerplate.
*   **Integration:**
    Can be used as a backend in `bin/memory/db.py` for high-throughput batch writes.

---

## 🔒 Security & FIPS Compliance Implications

1.  **FIPS Validated Cryptography:** We must declare `wolfssl` as a **required** dependency under FIPS environments, ensuring that `crypto_provider.py` doesn't fall back silently to non-validated libraries.
2.  **Supply-Chain Hygiene:** All added libraries must be pinned to stable major/minor versions with exact SHA-256 hashes matching our `pyproject.toml` security policies.
3.  **Local-First Verification:** All proposed libraries (`orjson`, `uvloop`, `sqlite-vec`, `rapidjason`) compile to native binary wheels, running **100% offline** without calling any external networks or cloud telemetry.
