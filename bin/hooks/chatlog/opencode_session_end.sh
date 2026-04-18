#!/usr/bin/env sh
# OpenCode session-end hook → m3-memory chat log ingest.
# Installed by running chatlog_init.py.

HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="$(cd "$HERE/../../.." && pwd)"

if [ -x "$BASE/.venv/bin/python" ]; then
    PY="$BASE/.venv/bin/python"
else
    PY="python3"
fi

exec "$PY" "$BASE/bin/chatlog_ingest.py" --format opencode
