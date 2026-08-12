#!/usr/bin/env sh
# Aider chat-history watcher → m3-memory chat log ingest.
# Aider writes to .aider.chat.history.md in the repo root; we tail it.
# Usage: aider_chat_watcher.sh [<repo_root>]
# Default repo_root = current working directory.

HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="$(cd "$HERE/../../.." && pwd)"
REPO="${1:-.}"

# -x is not enough: a dependency-less venv passes it and then dies at
# `import httpx`. See claude_code_precompact.sh for the full rationale.
m3_usable() {
    [ -n "$1" ] && [ -x "$1" ] && "$1" -c "import httpx" >/dev/null 2>&1
}

if m3_usable "$BASE/.venv/bin/python"; then
    PY="$BASE/.venv/bin/python"
elif m3_usable "$BASE/.venv/Scripts/python.exe"; then
    PY="$BASE/.venv/Scripts/python.exe"
elif m3_usable "$M3_PYTHON"; then
    PY="$M3_PYTHON"
else
    PY="python3"
    m3_usable "$PY" || echo "aider_chat_watcher: no python with httpx found; trying '$PY' anyway" >&2
fi

exec "$PY" "$BASE/bin/chatlog_ingest.py" --format aider --watch "$REPO"
