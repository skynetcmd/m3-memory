#!/usr/bin/env sh
# Aider chat-history watcher → m3-memory chat log ingest.
# Aider writes to .aider.chat.history.md in the repo root; we tail it.
# Usage: aider_chat_watcher.sh [<repo_root>]
# Default repo_root = current working directory.

HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="$(cd "$HERE/../../.." && pwd)"
REPO="${1:-.}"

if [ -x "$BASE/.venv/bin/python" ]; then
    PY="$BASE/.venv/bin/python"
else
    PY="python3"
fi

exec "$PY" "$BASE/bin/chatlog_ingest.py" --format aider --watch "$REPO"
