# M3 Memory: "Homecoming" Migration Plan

This plan outlines the process for consolidating existing m3-memory installations into a unified, cross-platform default root directory: `~/.m3-memory`.

## 🎯 Goals
- Standardize the installation root to `~/.m3-memory` across Windows, macOS, and Linux.
- Ensure 100% data integrity for Core and Chatlog databases during relocation.
- Automatically re-wire client tools (Claude, Gemini, Aider, OpenCode, OpenClaw) to the new root.
- Provide a high-clarity, informed user experience (UX) during the migration.

---

## 🏗️ Phase 1: Discovery & Impact Analysis
1.  **Asset Location**:
    *   **Databases**: Resolve paths for Core (`agent_memory.db`) and Chatlog (`agent_chatlog.db`) using `M3_DATABASE` and `CHATLOG_DB_PATH`.
    *   **Models**: Locate Rust/Oxidation `.gguf` files via `M3_EMBED_GGUF`.
    *   **Client Configs**:
        *   Claude: `~/.claude/settings.json`
        *   Gemini: `~/.gemini/settings.json`
        *   Aider: `.aider.conf.yml` (local) or global config paths.
        *   OpenCode/OpenClaw: Search for MCP proxy settings.
2.  **Impact Preview**:
    *   Calculate total size of all databases and model assets.
    *   Check available disk space on the home partition (`~`).
    *   Estimate times for "Move" (instant) vs "Copy" (based on disk speed).

## 🏗️ Phase 2: The Interactive Migration Offer
Present the user with a cross-platform data-driven choice:
> *M3 is moving to a unified home: `~/.m3-memory`.*
> 
> **Impact Summary:**
> - Core Data: [Size] MB found at [Path]
> - Rust Models: [Size] MB found at [Path]
> - Target Home: [~ Path] ([Space] GB free)
> 
> **How would you like to proceed?**
> 1. [FAST] Move: Instant relocation (Only available if source and target are on the same drive/partition).
> 2. [SAFE] Copy: Uses SQLite Backup API for 100% data integrity even if the DB is active.
> 3. [STAY]: Keep your current configuration.

## 🏗️ Phase 3: Atomic & Secure Execution
1.  **Attempt Atomic Move (os.rename)**:
    *   If rename fails (due to cross-device link error or permissions):
        *   **Alert**: "Rename failed (cross-drive move). Falling back to Secure Copy."
        *   **Confirm**: Ask permission to proceed with the copy operation.
2.  **Secure Copy (SQLite Backup API)**:
    *   Use `sqlite3.Connection.backup()` to stream database content.
    *   Ensures a consistent snapshot even if background tasks are pulsing.
3.  **Model Re-homing**: Relocate `.gguf` models to `~/.m3-memory/models/`.
4.  **Verification**: Execute `PRAGMA integrity_check` on new database files.

## 🏗️ Phase 4: 5-Tool Client Orchestration
With explicit user permission, update the following configurations:
- **Claude**: Update `mcpServers` in `~/.claude/settings.json`.
- **Gemini**: Update `mcpServers` in `~/.gemini/settings.json`.
- **Aider**: Update `.aider.conf.yml` and environment settings.
- **OpenCode & OpenClaw**: Update `mcp_proxy` wiring and model paths.
- **Shell Integration**:
    *   **Windows**: Update PowerShell `$PROFILE`.
    *   **macOS/Linux**: Update `~/.zshrc` or `~/.bashrc` with `export M3_MEMORY_ROOT="~/.m3-memory"`.

## 🏗️ Phase 5: Cleanup & Finalization
1.  **Authorized Cleanup**: If the "Copy" method was used, ask permission to delete original legacy files.
2.  **Summary Report**:
    *   "Successfully migrated [X] GB to ~/.m3-memory."
    *   "Re-wired 5 tools (Claude, Gemini, Aider, OpenCode, OpenClaw)."
    *   "Integrity Verified: [OK]"
