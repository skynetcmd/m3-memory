# M3 Memory: Troubleshooting

## Database Issues

### "database is locked" (SQLite)
- **Cause**: Multiple agents writing simultaneously.
- **Solution**: M3 uses WAL mode and busy timeouts. If the error persists, check for orphaned Python processes:
  - `ps aux | grep python` (Linux/Mac)
  - `tasklist | findstr python` (Windows)

### PostgreSQL sync failures
- **Check**: Verify `PG_URL` is set correctly (environment variable or OS keyring).
- **Check**: Confirm the PostgreSQL server is reachable from this machine.
- **Note**: Sync is optional. M3 Memory works fully without PostgreSQL.

---

## Embedding Issues

### "Embedding failed" or "Connection refused"
- **Cause**: Your local embedding server isn't running.
- **Solution**: Start Ollama (`ollama serve`) or verify LM Studio is running on its configured port.

### Semantic search returning poor results
- **Solution**: Run `memory_maintenance` to decay importance of stale items.
- **Solution**: Verify the correct embedding model is loaded (e.g., `nomic-embed-text` for Ollama, or check your LM Studio model list).
- **Solution**: Ensure all devices use the same embedding model and dimension (`EMBED_DIM`, default 1024). Mismatched dimensions break cosine similarity.

## Scheduled Task Visibility

### Focus-stealing command prompt windows (Windows)
- **Cause**: Older installs registered the `AgentOS_*` scheduled tasks to run
  through `cmd.exe`, which draws a console window on screen every time a task
  fires (every 15-30 minutes for the busy ones).
- **Fix**: Run the fix script — it self-elevates (accept the UAC prompt), so
  you can start it from a normal terminal:
  ```powershell
  powershell -ExecutionPolicy Bypass -File bin\fix_scheduled_tasks.ps1
  ```
  It re-registers all tasks with `pythonw.exe` (no console subsystem → no
  window) and prints a before/after summary.
- **Equivalent manual fix**: in an **Administrator** terminal, run the
  installer directly:
  ```powershell
  python bin/install_schedules.py --repair
  ```
- **Note**: the older `-Hidden` / `Set-ScheduledTask ... Hidden` trick does
  **not** fix this — it only hides the task's entry in the Task Scheduler UI,
  not the console window. Use the fix above instead.
- **macOS / Linux**: not affected — cron jobs never draw a window. Just run
  `python bin/install_schedules.py --add all` normally.

---

## Installation Issues

### "m3: command not found"
- **Cause**: The package isn't installed or isn't on your PATH.
- **Solution**:
  ```bash
  pip install m3-memory
  which m3  # should return a path (the older `mcp-memory` alias also works)
  ```

### Memory server doesn't appear in agent
- Verify the JSON in your agent's config file is valid.
- Make sure the key is `"mcpServers"` (case-sensitive).
- Restart the agent completely (not just a new session).

### Agent can't find previous memories
- Memories are stored in `~/.m3-memory/memory/agent_memory.db` by default
  (override with `M3_MEMORY_ROOT`).
- The bridge resolves the DB from `M3_MEMORY_ROOT` regardless of the
  directory `m3` was launched from.

---

## ChromaDB Issues

### "ChromaDB unreachable"
- Verify `CHROMA_BASE_URL` is set to the correct endpoint.
- Check that the ChromaDB server is running on the target host.
- M3 falls back to a local `chroma_mirror` table when ChromaDB is unreachable. Memories are queued and synced when the connection is restored.
