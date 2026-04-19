#!/usr/bin/env sh
# Gemini CLI session-end hook → m3-memory chat log ingest.
#
# Envelope on stdin (per gemini-cli docs/hooks/reference.md, Base input schema):
#   { "session_id": "...", "transcript_path": "...",
#     "cwd": "...", "hook_event_name": "SessionEnd", "timestamp": "...",
#     "reason": "exit" | "clear" | "logout" | "prompt_input_exit" | "other" }

# Resolve repo root: $M3_HOME wins, else script-relative (../../..).
if [ -n "$M3_HOME" ]; then
    BASE="$M3_HOME"
else
    BASE="$(cd "$(dirname "$0")/../../.." && pwd)"
fi

if [ ! -f "$BASE/bin/chatlog_ingest.py" ]; then
    echo "gemini_cli_onexit: could not find bin/chatlog_ingest.py under '$BASE'. Set M3_HOME to the m3-memory repo root." >&2
    exit 1
fi

if [ -x "$BASE/.venv/bin/python" ]; then
    PY="$BASE/.venv/bin/python"
elif [ -x "$BASE/.venv/Scripts/python.exe" ]; then
    # Support Windows Git Bash / Cygwin paths
    PY="$BASE/.venv/Scripts/python.exe"
else
    PY="python3"
fi

# Read stdin once, then parse all fields in a single python call.
ENV_JSON=$(cat)

if [ -z "$ENV_JSON" ]; then
    echo "gemini_cli_onexit: empty stdin envelope" >&2
    exit 1
fi

# One python invocation: emit three newline-separated fields.
FIELDS=$(printf '%s' "$ENV_JSON" | "$PY" -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as e:
    sys.stderr.write('malformed JSON envelope: %s\n' % e)
    sys.exit(2)
for k in ('transcript_path', 'session_id', 'reason'):
    print(d.get(k, '') or '')
") || { echo "gemini_cli_onexit: failed to parse envelope" >&2; exit 1; }

TRANSCRIPT=$(printf '%s' "$FIELDS" | sed -n '1p')
SESSION_ID=$(printf '%s' "$FIELDS" | sed -n '2p')
REASON=$(printf '%s' "$FIELDS" | sed -n '3p')

if [ -z "$TRANSCRIPT" ]; then
    echo "gemini_cli_onexit: envelope missing transcript_path" >&2
    exit 1
fi

# Determine variant
if [ -z "$REASON" ]; then
    VARIANT="session_end"
else
    VARIANT="session_end_$REASON"
fi

# Exec into ingest
exec "$PY" "$BASE/bin/chatlog_ingest.py" \
    --format gemini-cli \
    --transcript-path "$TRANSCRIPT" \
    --session-id "$SESSION_ID" \
    --variant "$VARIANT"
