#!/bin/bash
set -euo pipefail
# pg_sync.sh — macOS/Linux hourly sync wrapper.
# Delegates to sync_all.py which handles both pg_sync and chroma_sync.
# Registered via cron: 0 * * * * /path/to/bin/pg_sync.sh

WORKSPACE="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$WORKSPACE/logs/sync_all.log"
PYTHON="$WORKSPACE/.venv/bin/python"

mkdir -p "$WORKSPACE/logs"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] pg_sync.sh triggered sync_all.py" >> "$LOG_FILE"

exec "$PYTHON" "$WORKSPACE/bin/sync_all.py"
