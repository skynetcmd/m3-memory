# M3 Max Agentic OS: Troubleshooting

## 🗄️ Database Issues

### "database is locked" (SQLite)
- **Cause**: Multiple agents (Claude + Gemini + OpenClaw) writing simultaneously.
- **Solution**: The system uses WAL mode and busy_timeouts, but if it persists, check for orphaned python processes:
  - `ps aux | grep python` (Linux/Mac)
  - `tasklist | findstr python` (Windows)

### PG Sync Failures
- **Check**: `PG_URL` in `.env` or encrypted vault.
- **Check**: Network connectivity to `10.x.x.x`.

## 🌐 MCP Proxy Issues

### "401 Unauthorized"
- **Cause**: `MCP_PROXY_KEY` missing or mismatched.
- **Solution**: Ensure `Authorization: Bearer <key>` header is sent by Aider/OpenClaw.

### "503 Backend Unreachable"
- **Cause**: LM Studio or cloud API endpoint is down.
- **Check**: `lms server status` or Perplexity/Anthropic status pages.

## 🧠 Memory Issues

### Semantic search returning poor results
- **Solution**: Run `memory_maintenance` to decay importance of stale items.
- **Solution**: Check if the correct embedding model is loaded in LM Studio (`text-embedding-nomic-embed-text-v1.5`).
