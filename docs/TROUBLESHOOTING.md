# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory: Troubleshooting

## Installation & Upgrade Issues

### "I ran `pipx upgrade m3-memory` and nothing changed"
- **Cause**: M3 was installed via standard `pip`, `pip --user`, or virtualenv rather than `pipx`. `pipx upgrade` exits with status 0 without modifying environments it does not manage.
- **Solution**: Run `python bin/m3_upgrade.py` (or `python bin/m3_upgrade.py --dry-run` to inspect). The orchestrator automatically detects the active installation method (`pip`, `pipx`, `pip --user`, or plugin) and executes the correct upgrade steps.

---

## Database Issues

### "database is locked" (SQLite)
- **Cause**: Multiple agents writing simultaneously.
- **Solution**: M3 uses WAL mode and busy timeouts. If the error persists, check for orphaned Python processes:
  - `ps aux | grep python` (Linux/Mac)
  - `tasklist | findstr python` (Windows)

### PostgreSQL sync failures
- **Check**: Verify `M3_CDW_PG_URL` is set correctly (environment variable or OS keyring). `PG_URL` still works but is deprecated. If your *local* store is PostgreSQL too, `M3_PRIMARY_PG_URL` must point at the primary, not the warehouse — see [SYNC_PG_TO_PG.md](SYNC_PG_TO_PG.md).
- **Check**: Confirm the PostgreSQL server is reachable from this machine.
- **Note**: Sync is optional. M3 Memory works fully without PostgreSQL.

---

## Embedding Issues

### "Embedding failed" or "Connection refused"
- **Cause**: The shared local embed server isn't running. m3 ships its own (`m3-embed-server`, from the `m3-core-rs` wheel) and uses it by default on `127.0.0.1:8082` — Ollama and LM Studio are optional alternatives, not the default.
- **Solution**: Check it with `m3 embedder status`, then `m3 embedder start`. If you deliberately route to an external provider, verify `M3_EMBED_URL` points at a live OpenAI-compatible `/v1/embeddings` endpoint.

### Semantic search returning poor results
- **Solution**: Run `memory_maintenance` to decay importance of stale items.
- **Solution**: Verify the correct embedding model is loaded (e.g., `nomic-embed-text` for Ollama, or check your LM Studio model list).
- **Solution**: Ensure all devices use the same embedding model and dimension (`EMBED_DIM`, default 1024). Mismatched dimensions break cosine similarity.

## FIPS Crypto Issues

### Crash / RuntimeError: "FIPS mode enabled but … wolfSSL … could not be loaded"
- **Cause**: `M3_FIPS_MODE` (or `M3_FIPS_STRICT`) is set, but the wolfSSL library
  isn't installed in a trusted path. FIPS mode **fails closed** by design — it
  will not silently fall back to non-wolfCrypt crypto.
- **Solution** — install wolfSSL, then retry:
  ```bash
  m3 fips install-wolfssl      # builds open-source wolfSSL into ~/.m3/lib
  m3 doctor                    # confirm the "crypto (FIPS)" section shows it loaded
  ```
- **Or** disable FIPS for now: `unset M3_FIPS_MODE M3_FIPS_STRICT` (Windows:
  `setx M3_FIPS_MODE ""`).

### "M3_FIPS_STRICT=1 requires the CMVP-validated wolfCrypt FIPS module … OPEN-SOURCE build"
- **Cause**: `M3_FIPS_STRICT` requires the **commercial, CMVP-validated** wolfCrypt
  FIPS module, but you have the free **open-source** wolfSSL build.
- **Solution**: For real FIPS 140-3, obtain the validated module via wolfSSL's
  commercial channel. For homelab/dev (hardened wolfCrypt without the license),
  use `M3_FIPS_MODE=1` **without** `M3_FIPS_STRICT`.

### "wolfSSL … failed integrity pin (M3_WOLFSSL_SHA256)"
- **Cause**: The wolfSSL library's SHA-256 doesn't match your pinned
  `M3_WOLFSSL_SHA256`. Expected after you intentionally rebuild/upgrade wolfSSL;
  **investigate** if you didn't.
- **Solution**: If you trust the new build, re-pin: `m3 doctor` prints the loaded
  library's SHA-256 — copy it into `M3_WOLFSSL_SHA256`.

See [FIPS_MODULE_BOUNDARY.md](FIPS_MODULE_BOUNDARY.md) for the full model.

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
  `python3 bin/install_schedules.py --add all` normally.

---

## Installation Issues

### "m3: command not found"
- **Cause**: The package isn't installed or isn't on your PATH.
- **Solution**:
  ```bash
  pip install m3-memory
  which m3  # should return a path (the older `mcp-memory` alias also works)
  ```

### Memory tools vanish mid-session ("m3 is unreachable", tools disappear)

The server was working and then the tools disappeared partway through a session
— often after a long build or test run.

**Run `/mcp` in that session.** The tools come back immediately. This is a
low-risk, routine action: ~0.9 s including cold start, idempotent, and it
cannot lose data — the bridge is a stateless adapter in front of the database,
so a fresh one opens the same store.

**Your memory is not lost.** Chatlog capture writes to the database directly and
does not travel over the MCP connection, so a dropped connection costs you
searching and writing for a few seconds, not captured turns. Confirm with:

```bash
m3 chatlog doctor          # capture.healthy + a recent last_write_at = nothing lost
```

This is a client-side MCP lifecycle limitation, not an m3 failure — the client
owns the pipe to the server process, so nothing on the m3 side can reconnect it.
**Any agent that reaches m3 over stdio is exposed to this** (Claude Code,
Antigravity, Gemini CLI, OpenCode all are); we have confirmed it in Claude Code
and not yet verified the others.
**[MCP_DISCONNECTS.md](MCP_DISCONNECTS.md)** has the full explanation: the
mechanism, the measured evidence that no data is lost, the upstream issue
references, and why we deliberately did not switch the default transport.

(Distinct from the entry below, which covers a server that never appeared at all.)

### Memory server doesn't appear in agent
- Verify the JSON in your agent's config file is valid.
- Make sure the key is `"mcpServers"` (case-sensitive).
- Restart the agent completely (not just a new session).

### Agent can't find previous memories
- Memories live under the **engine root**: `~/.m3/engine/agent_memory.db` by
  default. Resolution is `M3_ENGINE_ROOT` > `M3_MEMORY_ROOT/engine` >
  `~/.m3/engine`. Ask m3 rather than guessing: `m3 chatlog status --json`
  reports the resolved backend and roots.
- ⚠ `~/.m3-memory/` is the **retired** unified root. If it still exists it
  holds only backups and stale config — a DB found there is not your live
  store. `bin/homecoming.py` migrates a legacy layout to the split roots.
- The bridge resolves the DB from those roots regardless of the directory
  `m3` was launched from.
