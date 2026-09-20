#!/usr/bin/env sh
# OpenClaw session-end hook → m3-memory chat log ingest.
# Installed by running chatlog_init.py.

HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="$(cd "$HERE/../../.." && pwd)"

# -x is not enough: a dependency-less venv passes it and then dies at
# `import httpx`. See claude_code_precompact.sh for the full rationale.
m3_usable() {
    [ -n "$1" ] && [ -x "$1" ] && "$1" -c "import httpx" >/dev/null 2>&1
}

# pythonw.exe first on Windows: python.exe is the CONSOLE build and
# flashes a focus-stealing window on every hook fire. Same interpreter,
# no console. python.exe is kept next so a layout without pythonw still
# resolves. (2026-09-13; the .py hooks use CREATE_NO_WINDOW for this.)
if m3_usable "$BASE/.venv/bin/python"; then
    PY="$BASE/.venv/bin/python"
elif m3_usable "$BASE/.venv/Scripts/pythonw.exe"; then
    PY="$BASE/.venv/Scripts/pythonw.exe"
elif m3_usable "$BASE/.venv/Scripts/python.exe"; then
    PY="$BASE/.venv/Scripts/python.exe"
elif m3_usable "$M3_PYTHON"; then
    PY="$M3_PYTHON"
else
    PY="python3"
    m3_usable "$PY" || echo "openclaw_session_end: no python with httpx found; trying '$PY' anyway" >&2
fi

# The .py hook discovers the newest OpenClaw transcript itself (OpenClaw
# sends no envelope and no path), so go through it rather than calling
# chatlog_ingest directly as the other wrappers do.
exec "$PY" "$BASE/bin/hooks/chatlog/openclaw_session_end.py"
