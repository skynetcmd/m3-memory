#!/usr/bin/env sh
# Claude Code PreCompact / Stop hook → m3-memory chat log ingest.
#
# Envelope on stdin (per https://code.claude.com/docs/en/hooks.md):
#   { "session_id": "...", "transcript_path": "...",
#     "cwd": "...", "hook_event_name": "PreCompact" | "Stop", ... }

# Resolve repo root: $M3_HOME wins, else script-relative (../../..).
if [ -n "$M3_HOME" ]; then
    BASE="$M3_HOME"
else
    BASE="$(cd "$(dirname "$0")/../../.." && pwd)"
fi

if [ ! -f "$BASE/bin/chatlog_ingest.py" ]; then
    echo "claude_code_precompact: could not find bin/chatlog_ingest.py under '$BASE'. Set M3_HOME to the m3-memory repo root." >&2
    exit 1
fi

# Pick a python that has the chatlog_ingest deps (httpx, etc).
#
# EXECUTABLE IS NOT USABLE. A bare `python -m venv` (no packages) passes -x but
# dies at `import httpx` before chatlog_ingest prints any JSON, so the caller
# sees a generic "ingest failed or unreachable" and capture is silently lost.
# That is the 2026-08-09..11 regression: a stub .venv at candidate 1 shadowed
# all eight working fallbacks below it for ~2 days. Probe each candidate and
# take the first that actually imports the dependency.
#
# Priority:
#   1. Repo-local venv at $BASE/.venv (dev checkouts)
#   2. pipx venv for m3-memory:
#      a. ~/.local/share/pipx/venvs/m3-memory  (pipx >= 1.4, XDG-compliant — Debian 13, Ubuntu 24.04+)
#      b. ~/.local/pipx/venvs/m3-memory        (pipx < 1.4 — Debian 12, older distros)
#   3. $PIPX_HOME/venvs/m3-memory if user set a custom pipx home
#   4. Env override $M3_PYTHON (last resort for odd layouts)
#   5. system python3 (works only if the host happens to have httpx installed)
m3_usable() {
    [ -n "$1" ] && [ -x "$1" ] && "$1" -c "import httpx" >/dev/null 2>&1
}

PY=""
for _cand in \
    "$BASE/.venv/bin/python" \
    "$BASE/.venv/Scripts/python.exe" \
    "$HOME/.local/share/pipx/venvs/m3-memory/bin/python" \
    "$HOME/.local/share/pipx/venvs/m3-memory/Scripts/python.exe" \
    "$HOME/.local/pipx/venvs/m3-memory/bin/python" \
    "$HOME/.local/pipx/venvs/m3-memory/Scripts/python.exe" \
    "${PIPX_HOME:+$PIPX_HOME/venvs/m3-memory/bin/python}" \
    "${PIPX_HOME:+$PIPX_HOME/venvs/m3-memory/Scripts/python.exe}" \
    "$M3_PYTHON"
do
    if m3_usable "$_cand"; then PY="$_cand"; break; fi
done

# Last resort: system python3. Probe it too, but fall back to it regardless —
# a broken python3 yields a loud ingest error, which beats exiting silently.
if [ -z "$PY" ]; then
    PY="python3"
    m3_usable "$PY" || echo "claude_code_precompact: no python with httpx found; trying '$PY' anyway" >&2
fi

# Read stdin once, then parse all fields in a single python call.
ENV_JSON=$(cat)

if [ -z "$ENV_JSON" ]; then
    echo "claude_code_precompact: empty stdin envelope" >&2
    exit 1
fi

# One python invocation: emit three newline-separated fields.
# (Field values cannot contain newlines — they're an event name, a UUID, and a file path.)
FIELDS=$(printf '%s' "$ENV_JSON" | "$PY" -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as e:
    sys.stderr.write('malformed JSON envelope: %s\n' % e)
    sys.exit(2)
for k in ('transcript_path', 'session_id', 'hook_event_name'):
    print(d.get(k, '') or '')
") || { echo "claude_code_precompact: failed to parse envelope" >&2; exit 1; }

TRANSCRIPT=$(printf '%s' "$FIELDS" | sed -n '1p')
SESSION_ID=$(printf '%s' "$FIELDS" | sed -n '2p')
EVENT_NAME=$(printf '%s' "$FIELDS" | sed -n '3p')

if [ -z "$TRANSCRIPT" ]; then
    echo "claude_code_precompact: envelope missing transcript_path" >&2
    exit 1
fi

# Determine variant
case "$EVENT_NAME" in
    "PreCompact") VARIANT="pre_compact" ;;
    "Stop")       VARIANT="stop" ;;
    *)            VARIANT="claude_code" ;;
esac

# Exec into ingest
exec "$PY" "$BASE/bin/chatlog_ingest.py" \
    --format claude-code \
    --transcript-path "$TRANSCRIPT" \
    --session-id "$SESSION_ID" \
    --variant "$VARIANT"
