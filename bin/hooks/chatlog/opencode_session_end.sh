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
    m3_usable "$PY" || echo "opencode_session_end: no python with httpx found; trying '$PY' anyway" >&2
fi

# --transcript-path IS REQUIRED and was missing, so this exited 2 on every fire
# and OpenCode capture never ran. OpenCode writes no transcript FILE: it keeps
# sessions in opencode.db (v1.2.0+) or a legacy per-message JSON tree.
#
# "auto" asks chatlog_ingest to locate the store. The per-OS candidate list
# lives there, in ONE place: XDG on Linux, ~/Library/Application Support on
# macOS, LOCALAPPDATA on Windows, all overridable by OPENCODE_DATA_DIR. Each
# shell wrapper deriving its own default is how two platforms drift apart, and
# the sh copy hardcoded the Linux convention (DESIGN §1, §10a).
exec "$PY" "$BASE/bin/chatlog_ingest.py" --format opencode --transcript-path auto --variant session_end
