#!/usr/bin/env sh
# OpenCode session-end hook → m3-memory chat log ingest.
# Installed by running chatlog_init.py.

HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="$(cd "$HERE/../../.." && pwd)"

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
    m3_usable "$PY" || echo "opencode_session_end: no python with httpx found; trying '$PY' anyway" >&2
fi

exec "$PY" "$BASE/bin/chatlog_ingest.py" --format opencode
