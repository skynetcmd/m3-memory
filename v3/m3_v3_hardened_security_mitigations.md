# 🛡️ M3-v3: Adversarial Vulnerability Audit & Hardening Plan

An adversarial review of the **M3-v3** implementation plan has identified three critical weaknesses that could compromise **security certification**, **data integrity**, or **system stability** in high-throughput or compliant production environments. 

This document analyzes each vulnerability and defines strict, non-negotiable mitigations to harden the master plan.

---

## 🔍 Vulnerability 1: Silent FIPS Fallback Bypass

```
                                [ M3_FIPS_MODE=1 ]
                                        │
                                        ▼
                          [ Attempt to Load wolfSSL ]
                                 /             \
                       (Success) /             \ (Failure / Missing dll)
                                /               \
                               ▼                 ▼
                     [ Run FIPS Mode ]   [ SILENT FALLBACK TO DEFAULT ]
                                                  ⚠️ SECURITY COMPLIANCE VIOLATION!
```

### 🚨 The Scenario & Risk
*   **The Loophole:** In the legacy `crypto_provider.py` flow, if `M3_CRYPTO_BACKEND="WOLFSSL"` is selected but `libwolfssl.so` or `wolfssl.dll` cannot be located by `ctypes.CDLL()`, or if the Python `wolfssl` module is uninstalled, the code prints a warning and falls back to standard Python `hashlib` and `cryptography` libraries.
*   **The Risk:** In a highly compliant FIPS 140-3 environment, a silent fallback to unvalidated cryptographic implementations is a **critical security violation**. An administrator might assume the system is running FIPS-validated algorithms, while in reality, it has silently reverted to default non-validated implementations due to a library path or loader misconfiguration.

### 🛡️ Hardening Mitigation
We will enforce a strict **FIPS Lockout Rule** inside `crypto_provider.py`. If `M3_FIPS_MODE=1` is set, any loading, initialization, or self-test (POST) failure of the wolfSSL/wolfCrypt backend must immediately raise a fatal, un-swallowed `RuntimeError` and terminate the system execution. **Silent failover is strictly forbidden under FIPS enforcement.**

```python
# Hardened logic in crypto_provider.py
if os.environ.get("M3_FIPS_MODE") == "1":
    if not self._initialized or self.backend != "WOLFSSL":
        raise RuntimeError(
            "FATAL: FIPS 140-3 Compliance Mode Enforced, but FIPS-validated "
            "wolfSSL/wolfCrypt backend failed to initialize. Terminating to prevent "
            "unsafe cryptographic fallback."
        )
```

---

## 🔍 Vulnerability 2: Startup Auto-Migration Race Condition

```
                    [ High Concurrency Startup ]
                     /           |           \
             Process 1       Process 2       Process 3
                    \            |            /
                     ▼           ▼           ▼
                   [ Check legacy ~/.m3-memory/ ]
                                 │
                         (Empty Target Engine)
                                 │
                     ⚠️ MULTIPLE PARALLEL COPIES!
                (SQLite Corruptions & Partial Overwrites)
```

### 🚨 The Scenario & Risk
*   **The Loophole:** The plan schedules an auto-migration check during `M3Context` initialization. If the system starts under high concurrency (e.g. multiple MCP agents launching simultaneously, or several background tools running in parallel), all starting processes will simultaneously detect that `~/.m3/engine` is empty and that `~/.m3-memory/` holds databases.
*   **The Risk:** They will all attempt to copy `agent_memory.db` and `agent_chatlog.db` at the exact same time. SQLite database files copied concurrently by different processes will result in **file corruptions**, **partial overrides**, or **deadlocks** as they compete for filesystem locks.

### 🛡️ Hardening Mitigation
We must implement a **Distributed Advisory Lock File** during the auto-migration phase. Before copying files, the initialization routine must acquire an exclusive file lock (e.g. creating `.m3_migration.lock` using atomic filesystem operations like `os.open` with `O_CREAT | O_EXCL` or OS-native `fcntl`/`msvcrt` locks).
*   If a process fails to acquire the lock, it must wait-loop (up to 10s) checking if the target database files are populated and valid.
*   Once the lock-owning process completes the migration, it deletes the lock file, and all other threads/processes safely proceed using the newly migrated databases.

```python
# Hardened lock creation logic in homecoming / m3_sdk
lock_path = os.path.join(get_m3_config_root(), ".migration.lock")
try:
    # Atomic creation of lock file
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        # Perform SQLite backup API migration safely...
        execute_migration()
    finally:
        os.close(fd)
        os.unlink(lock_path)
except FileExistsError:
    # Another process is migrating; block-wait until databases exist and lock is gone
    wait_for_migration_completion(lock_path)
```

---

## 🔍 Vulnerability 3: Decoupled Root Cohesion Drift

### 🚨 The Scenario & Risk
*   **The Loophole:** Splitting configurations to `~/.m3/config` and databases to `~/.m3/engine` introduces a bimodal directory scheme. If a user sets `M3_CONFIG_ROOT` to point to a custom deployment folder but forgets to set `M3_ENGINE_ROOT` (or vice versa), the system will pair an old/outdated configuration folder with a new database.
*   **The Risk:**
    1.  **Salt Mismatch:** The AES-256-GCM encryption key relies on `.agent_os_salt` residing in the config directory. If an old salt file is paired with a database encrypted with a newer salt, all vault secrets will decrypt into garbage data or crash.
    2.  **Migration Drift:** The schema migration history configuration (`.migrate_config.json`) will be out of sync with the actual database file, leading to double-applied migrations or locked tables.

### 🛡️ Hardening Mitigation
We will enforce a **Cohesion Lock & Keyring Hash Validation** at startup.
1.  During database initialization, the engine will compute a SHA-256 hash of the currently loaded `.agent_os_salt` and schema version metadata.
2.  It will save this hash inside a dedicated system metadata table `m3_system_cohesion` in the engine database.
3.  On every subsequent boot, the SDK will re-verify the active salt hash against the stored database value. If there is a mismatch (indicating the database was paired with a different config directory or salt), the system will **fail-loud**, refuse to boot, and prompt the user to reconcile their environment paths.

```sql
-- Hardened Cohesion Table
CREATE TABLE IF NOT EXISTS m3_system_cohesion (
    key TEXT PRIMARY KEY,
    value TEXT,
    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
