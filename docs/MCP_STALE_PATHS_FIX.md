# MCP Stale Paths Fix (May 23, 2026)

## Issue
Several MCP-related scripts and documentation entries contained stale, hardcoded paths pointing to `~/m3-memory/bin/`. These paths were incorrect for deployments where the repository is cloned into a subdirectory (e.g., `~/m3-memory/repo/bin/`).

Additionally, `start_mcp_proxy.sh` was using the system `python3`, which might lack required dependencies like `httpx` and `fastapi` that are present in the project's virtual environment.

## Fixes Applied

1.  **`repo/bin/start_mcp_proxy.sh`**:
    *   Updated to dynamically resolve the script's directory using `$(dirname "${BASH_SOURCE[0]}")`.
    *   Updated to automatically detect and use the project's virtual environment (`.venv`) if available, ensuring all dependencies are met.
    *   Updated usage comments.
2.  **`repo/bin/aider`**:
    *   Updated comments to suggest the correct `PATH` entry: `~/m3-memory/repo/bin`.
3.  **`repo/bin/test_mcp_proxy.py`**:
    *   Updated usage comments to point to the correct script locations.
4.  **`~/.gemini/settings.json`**:
    *   Verified that stale paths were previously updated to `/Users/username/m3-memory/repo/bin/`.

## Manual Verification
The MCP proxy was successfully started and verified using the health check endpoint:
```bash
bash repo/bin/start_mcp_proxy.sh --background
curl http://localhost:9000/health
```
Result: `{"status":"ok", ... "total":87}`

## Recommendation for Users
If you encounter "command not found" or "module not found" errors when running MCP tools, ensure your `~/.gemini/settings.json` or `~/.claude/settings.json` points to the `repo/bin` directory inside your `M3_MEMORY_ROOT`.

Example for Gemini CLI:
```json
"mcpServers": {
  "memory": {
    "command": "python3",
    "args": ["/Users/username/m3-memory/repo/bin/memory_bridge.py"]
  }
}
```
