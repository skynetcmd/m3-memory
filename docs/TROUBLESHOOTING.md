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

---

## Installation Issues

### "mcp-memory: command not found"
- **Cause**: The package isn't installed or isn't on your PATH.
- **Solution**:
  ```bash
  pip install m3-memory
  which mcp-memory  # should return a path
  ```

### Memory server doesn't appear in agent
- Verify the JSON in your agent's config file is valid.
- Make sure the key is `"mcpServers"` (case-sensitive).
- Restart the agent completely (not just a new session).

### Agent can't find previous memories
- Memories are stored in `memory/agent_memory.db` relative to where `mcp-memory` runs.
- Check that you're running from the same directory, or set `M3_MEMORY_ROOT`.

---

## ChromaDB Issues

### "ChromaDB unreachable"
- Verify `CHROMA_BASE_URL` is set to the correct endpoint.
- Check that the ChromaDB server is running on the target host.
- M3 falls back to a local `chroma_mirror` table when ChromaDB is unreachable. Memories are queued and synced when the connection is restored.
