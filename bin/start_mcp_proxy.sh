#!/usr/bin/env bash
# start_mcp_proxy.sh — Launch the MCP Tool Execution Proxy on localhost:9000
# Usage: bash ~/m3-memory/bin/start_mcp_proxy.sh [--background]

set -euo pipefail

PROXY_SCRIPT="$HOME/m3-memory/bin/mcp_proxy.py"
PORT=9000
PID_FILE="${TMPDIR:-${HOME}/.cache}/mcp_proxy.pid"
LOG_FILE="${TMPDIR:-${HOME}/.cache}/mcp_proxy.log"

# ── Check port ────────────────────────────────────────────────────────────────
# Port check (lsof is Unix-only; on Windows use: python -c "import socket; s=socket.create_connection(('127.0.0.1', PORT), 1)" )
if lsof -ti ":$PORT" >/dev/null 2>&1; then
  echo "⚠️  Port $PORT already in use." >&2
  EXISTING_PID=$(lsof -ti ":$PORT" | head -1)
  echo "   PID $EXISTING_PID is listening. Run: kill $EXISTING_PID" >&2
  exit 1
fi

# ── Background mode ───────────────────────────────────────────────────────────
if [[ "${1:-}" == "--background" ]]; then
  echo "Starting MCP proxy in background (log: $LOG_FILE) ..."
  nohup python3 "$PROXY_SCRIPT" > "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"
  sleep 1
  if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "✅  MCP proxy running  PID=$(cat "$PID_FILE")  http://localhost:$PORT"
    echo "   Stop: kill \$(cat $PID_FILE)"
    echo "   Logs: tail -f $LOG_FILE"
  else
    echo "❌  MCP proxy failed to start — check $LOG_FILE"
    exit 1
  fi
else
  echo "MCP Tool Execution Proxy  →  http://localhost:$PORT"
  echo "Press Ctrl-C to stop."
  exec python3 "$PROXY_SCRIPT"
fi
