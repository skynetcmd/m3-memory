#!/usr/bin/env python3
"""Claude Code PreCompact / Stop hook -> m3-memory chatlog ingest.

Cross-platform (Windows, macOS, Linux). Reads the Claude Code hook envelope
from stdin, runs chatlog_ingest.py, and SCREAMS loudly if capture fails,
writing a fallback file to ~/.m3/unsaved_chats/ so no session is silently lost.

Handles: PreCompact, Stop

Envelope (stdin JSON):
  { "session_id": "...", "transcript_path": "...",
    "cwd": "...", "hook_event_name": "PreCompact" | "Stop" }
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

AGENT = "claude"
FORMAT = "claude-code"


# ── Shared core (parity across all m3 agent hooks) ───────────────────────────

def find_repo_root() -> Path:
    m3_home = os.environ.get("M3_HOME")
    if m3_home:
        return Path(m3_home)
    return Path(__file__).resolve().parents[3]


def find_python(repo: Path) -> str:
    for candidate in [
        repo / ".venv" / "Scripts" / "python.exe",
        repo / ".venv" / "bin" / "python",
    ]:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def write_fallback(transcript: str, session_id: str, agent: str,
                   event: str, variant: str, reason: str) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    sid = (session_id or "unknown")[:8]
    save_dir = Path.home() / ".m3" / "unsaved_chats"
    save_dir.mkdir(parents=True, exist_ok=True)
    fallback = save_dir / f"m3_unsaved_{agent}_{sid}_{ts}.md"
    content = f"""# M3 CHATLOG NOT SAVED - {ts}

**Agent:** {agent}
**Event:** {event}
**Session:** {session_id}
**Transcript path:** {transcript}
**Reason:** {reason}

This session's chatlog was NOT captured by m3-memory.

To recover manually:
    python bin/chatlog_ingest.py --format {FORMAT} \\
        --transcript-path "{transcript}" \\
        --variant {variant}
"""
    fallback.write_text(content, encoding="utf-8")
    return fallback


def scream(reason: str, fallback: Path, agent: str, event: str) -> None:
    border = "=" * 70
    try:
        display = "~/.m3/unsaved_chats/" + fallback.name
    except Exception:
        display = str(fallback)
    msg = f"""
{border}
  M3 CHATLOG NOT SAVED - SESSION CONTEXT WILL BE LOST

  Agent  : {agent} ({event})
  Reason : {reason}
  Saved  : {display}

  Fix    : restart m3 MCP, then re-run ingest on transcript above.
{border}
"""
    print(msg, file=sys.stderr, flush=True)


def announce(reason: str, fallback: Path, agent: str, event: str) -> None:
    """Surface the failure to the user via the harness, regardless of exit code.

    The stderr scream() alone is NOT enough: on a Stop event the harness only
    reliably shows stderr on a non-zero exit / under --debug, and ingest can
    exit 0 while writing 0 rows (the silent-capture-failure case). The hook
    contract parses stdout as JSON and always surfaces `systemMessage`, so we
    emit a red line there too — the scream can never be swallowed.

    stdout MUST stay JSON-only for the harness to parse it: this is the single
    print to stdout in the whole hook (everything else goes to stderr).
    """
    try:
        display = "~/.m3/unsaved_chats/" + fallback.name
    except Exception:  # noqa: BLE001
        display = str(fallback)
    msg = (
        f"\U0001f6a8 M3 CHATLOG NOT SAVED ({agent}/{event}): {reason}. "
        f"Fallback written to {display}. Restart the m3 MCP server, then re-run "
        "ingest on the transcript. Session context is at risk until you do."
    )
    print(json.dumps({"systemMessage": msg}), flush=True)


def _extract_last_json_object(output: str):
    """Return the last balanced top-level {...} JSON object in `output`, or None.

    ingest interleaves log lines with a final pretty-printed JSON object. We scan
    from the end for the last '}', then walk backwards tracking brace depth
    (ignoring braces inside strings) to find its matching '{', and json.loads the
    slice. Handles single-line compact JSON and multi-line pretty JSON alike.
    """
    end = output.rfind("}")
    if end == -1:
        return None
    depth = 0
    in_str = False
    for i in range(end, -1, -1):
        ch = output[i]
        if in_str:
            # walking backwards: a quote not preceded by an (odd run of) backslash
            # closes the string. Simplest robust check: count preceding backslashes.
            if ch == '"':
                bs = 0
                j = i - 1
                while j >= 0 and output[j] == "\\":
                    bs += 1
                    j -= 1
                if bs % 2 == 0:
                    in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "}":
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(output[i:end + 1])
                except json.JSONDecodeError:
                    return None
    return None


def run_ingest(py: str, ingest: Path, extra_args: list) -> tuple:
    """Run ingest, return (written, error, returncode)."""
    try:
        result = subprocess.run(
            [py, str(ingest)] + extra_args,
            capture_output=True, text=True, timeout=60)
        output = result.stdout.strip()
        # ingest emits PRETTY-PRINTED (multi-line) JSON after its log lines, e.g.
        #   {\n  "written": 9,\n  ...\n}
        # The old parser scanned for a line *starting with* "{" and json.loads'd
        # that single line — which is just "{" for pretty JSON, so it always
        # raised and reported written=0. That false-failure wrote 1485 bogus
        # fallback files over 9 days (2026-06-04..13) while ingest was succeeding.
        # Parse the LAST balanced {...} block from the end of stdout instead.
        result_json = _extract_last_json_object(output)
        written = int(result_json.get("written", 0)) if result_json else 0
        error = result_json.get("error") if result_json else "m3 ingest failed or unreachable"
        return written, error, result.returncode
    except subprocess.TimeoutExpired:
        return 0, "ingest timed out after 60s — m3 unreachable", 1
    except Exception as e:
        return 0, f"ingest failed: {e}", 1


# ── Agent-specific main ───────────────────────────────────────────────────────

def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        print(f"{AGENT} hook: empty stdin envelope", file=sys.stderr)
        return 1

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"{AGENT} hook: malformed JSON envelope: {e}", file=sys.stderr)
        return 1

    transcript = envelope.get("transcript_path", "")
    session_id = envelope.get("session_id", "")
    event_name = envelope.get("hook_event_name", "")

    if not transcript:
        print(f"{AGENT} hook: envelope missing transcript_path", file=sys.stderr)
        return 1

    variant = {"PreCompact": "pre_compact", "Stop": "stop"}.get(event_name, "claude_code")

    repo = find_repo_root()
    ingest = repo / "bin" / "chatlog_ingest.py"
    if not ingest.exists():
        reason = f"chatlog_ingest.py not found under {repo} — set M3_HOME"
        fallback = write_fallback(transcript, session_id, AGENT, event_name, variant, reason)
        scream(reason, fallback, AGENT, event_name)
        announce(reason, fallback, AGENT, event_name)
        return 1

    py = find_python(repo)
    args = ["--format", FORMAT, "--transcript-path", transcript, "--variant", variant]
    if session_id:
        args += ["--session-id", session_id]

    written, error, rc = run_ingest(py, ingest, args)

    if written == 0 or error:
        reason = error if error else "0 rows written — m3 may be down or unreachable"
        fallback = write_fallback(transcript, session_id, AGENT, event_name, variant, reason)
        scream(reason, fallback, AGENT, event_name)
        announce(reason, fallback, AGENT, event_name)
        # Force non-zero even if ingest exited 0: a 0-rows "success" is the exact
        # silent-failure the harness would otherwise swallow. Non-zero makes the
        # harness surface the hook, complementing the stdout systemMessage.
        return rc if rc != 0 else 1

    return rc


if __name__ == "__main__":
    sys.exit(main())
