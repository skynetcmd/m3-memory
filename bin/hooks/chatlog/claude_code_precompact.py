#!/usr/bin/env python3
"""Claude Code PreCompact / Stop hook -> m3-memory chatlog ingest.

Cross-platform (Windows, macOS, Linux). Reads the Claude Code hook envelope
from stdin, runs chatlog_ingest.py, and SCREAMS loudly if capture fails,
writing a fallback file to ~/.m3/unsaved_chats/ so no session is silently lost.

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


def find_repo_root() -> Path:
    m3_home = os.environ.get("M3_HOME")
    if m3_home:
        return Path(m3_home)
    # Script lives at <repo>/bin/hooks/chatlog/claude_code_precompact.py
    return Path(__file__).resolve().parents[3]


def find_python(repo: Path) -> str:
    for candidate in [
        repo / ".venv" / "Scripts" / "python.exe",   # Windows venv
        repo / ".venv" / "bin" / "python",            # Unix venv
    ]:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def write_fallback(transcript: str, session_id: str, event: str,
                   variant: str, reason: str) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    sid = (session_id or "unknown")[:8]
    save_dir = Path.home() / ".m3" / "unsaved_chats"
    save_dir.mkdir(parents=True, exist_ok=True)
    fallback = save_dir / f"m3_unsaved_claude_{sid}_{ts}.md"
    content = f"""# M3 CHATLOG NOT SAVED - {ts}

**Event:** {event}
**Session:** {session_id}
**Transcript path:** {transcript}
**Reason:** {reason}

This session's chatlog was NOT captured by m3-memory.

To recover manually:
    python bin/chatlog_ingest.py --format claude-code \\
        --transcript-path "{transcript}" \\
        --variant {variant}
"""
    fallback.write_text(content, encoding="utf-8")
    return fallback


def scream(reason: str, fallback: Path) -> None:
    border = "=" * 70
    # Show ~ instead of expanded home path to avoid leaking username
    try:
        display = "~/.m3/unsaved_chats/" + fallback.name
    except Exception:
        display = str(fallback)
    msg = f"""
{border}
  M3 CHATLOG NOT SAVED - SESSION CONTEXT WILL BE LOST

  Reason : {reason}
  Saved  : {display}

  Fix    : restart m3 MCP, then re-run ingest on transcript above.
{border}
"""
    print(msg, file=sys.stderr, flush=True)


def main() -> int:
    # Read stdin
    raw = sys.stdin.read().strip()
    if not raw:
        print("claude_code_precompact: empty stdin envelope", file=sys.stderr)
        return 1

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"claude_code_precompact: malformed JSON envelope: {e}", file=sys.stderr)
        return 1

    transcript = envelope.get("transcript_path", "")
    session_id = envelope.get("session_id", "")
    event_name = envelope.get("hook_event_name", "")

    if not transcript:
        print("claude_code_precompact: envelope missing transcript_path", file=sys.stderr)
        return 1

    variant = {"PreCompact": "pre_compact", "Stop": "stop"}.get(event_name, "claude_code")

    repo = find_repo_root()
    ingest = repo / "bin" / "chatlog_ingest.py"
    if not ingest.exists():
        reason = f"chatlog_ingest.py not found under {repo} — set M3_HOME"
        fallback = write_fallback(transcript, session_id, event_name, variant, reason)
        scream(reason, fallback)
        return 1

    py = find_python(repo)
    cmd = [py, str(ingest), "--format", "claude-code",
           "--transcript-path", transcript, "--variant", variant]
    if session_id:
        cmd += ["--session-id", session_id]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        output = result.stdout.strip()
        # Extract JSON result (ingest mixes log lines + JSON)
        result_json = None
        for line in reversed(output.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    result_json = json.loads(line)
                    break
                except json.JSONDecodeError:
                    pass

        written = int(result_json.get("written", 0)) if result_json else 0
        error = result_json.get("error") if result_json else "could not parse ingest output"

        if written == 0 or error:
            reason = error if error else "0 rows written — m3 may be down or unreachable"
            fallback = write_fallback(transcript, session_id, event_name, variant, reason)
            scream(reason, fallback)

        return result.returncode

    except subprocess.TimeoutExpired:
        reason = "ingest timed out after 60s — m3 unreachable"
        fallback = write_fallback(transcript, session_id, event_name, variant, reason)
        scream(reason, fallback)
        return 1
    except Exception as e:
        reason = f"ingest failed: {e}"
        fallback = write_fallback(transcript, session_id, event_name, variant, reason)
        scream(reason, fallback)
        return 1


if __name__ == "__main__":
    sys.exit(main())
