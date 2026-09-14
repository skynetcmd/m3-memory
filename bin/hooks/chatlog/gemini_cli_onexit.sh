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

# See claude_code_precompact.sh for the rationale behind this fallback order,
# and for why each candidate is PROBED rather than merely tested with -x.
m3_usable() {
    [ -n "$1" ] && [ -x "$1" ] && "$1" -c "import httpx" >/dev/null 2>&1
}

# WINDOWS: pythonw.exe BEFORE python.exe at every location.
# Both are the same interpreter; python.exe is the CONSOLE build, so each
# invocation from a hook allocates a conhost and FLASHES A WINDOW that steals
# focus. This wrapper runs up to three interpreters per hook fire (the
# usable-probe, the envelope parse, and the ingest exec), and the Stop hook
# fires on every turn - so the console build produced a visible flash every
# few seconds (observed 2026-09-13).
# The Python hooks already pass CREATE_NO_WINDOW for exactly this reason; a
# shell script has no such flag, so the fix is choosing the windowless binary.
# Verified 2026-09-13 that pythonw.exe is correct for all three call shapes
# here: exit-code probes, stdout through a pipe, and stderr through a pipe all
# behave identically (the "pythonw has no stdout" caveat applies to an
# inherited console, not to redirected handles). python.exe is KEPT as the
# next candidate at each location so a layout without pythonw still resolves.
PY=""
for _cand in \
    "$BASE/.venv/bin/python" \
    "$BASE/.venv/Scripts/pythonw.exe" \
    "$BASE/.venv/Scripts/python.exe" \
    "$HOME/.local/share/pipx/venvs/m3-memory/bin/python" \
    "$HOME/.local/share/pipx/venvs/m3-memory/Scripts/pythonw.exe" \
    "$HOME/.local/share/pipx/venvs/m3-memory/Scripts/python.exe" \
    "$HOME/.local/pipx/venvs/m3-memory/bin/python" \
    "$HOME/.local/pipx/venvs/m3-memory/Scripts/pythonw.exe" \
    "$HOME/.local/pipx/venvs/m3-memory/Scripts/python.exe" \
    "${PIPX_HOME:+$PIPX_HOME/venvs/m3-memory/bin/python}" \
    "${PIPX_HOME:+$PIPX_HOME/venvs/m3-memory/Scripts/pythonw.exe}" \
    "${PIPX_HOME:+$PIPX_HOME/venvs/m3-memory/Scripts/python.exe}" \
    "$M3_PYTHON"
do
    if m3_usable "$_cand"; then PY="$_cand"; break; fi
done

if [ -z "$PY" ]; then
    PY="python3"
    m3_usable "$PY" || echo "gemini_cli_onexit: no python with httpx found; trying '$PY' anyway" >&2
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
