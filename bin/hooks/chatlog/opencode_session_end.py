#!/usr/bin/env python3
"""OpenCode session-end hook -> m3-memory chatlog ingest.

Cross-platform (Windows, macOS, Linux). Reads the OpenCode hook envelope
from stdin, runs chatlog_ingest.py, and SCREAMS loudly if capture fails,
writing a fallback file to ~/.m3/unsaved_chats/ so no session is silently lost.
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
    return Path(__file__).resolve().parents[3]


def find_python(repo: Path) -> str:
    for candidate in [
        repo / ".venv" / "Scripts" / "python.exe",
        repo / ".venv" / "bin" / "python",
    ]:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def write_fallback(transcript: str, session_id: str, event: str,
                   reason: str) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    sid = (session_id or "unknown")[:8]
    save_dir = Path.home() / ".m3" / "unsaved_chats"
    save_dir.mkdir(parents=True, exist_ok=True)
    fallback = save_dir / f"m3_unsaved_opencode_{sid}_{ts}.md"
    content = f"""# M3 CHATLOG NOT SAVED - {ts}

**Agent:** opencode
**Event:** {event}
**Session:** {session_id}
**Transcript path:** {transcript}
**Reason:** {reason}

This session's chatlog was NOT captured by m3-memory.

To recover manually:
    python bin/chatlog_ingest.py --format opencode
"""
    fallback.write_text(content, encoding="utf-8")
    return fallback


def scream(reason: str, fallback: Path, event: str) -> None:
    border = "=" * 70
    try:
        display = "~/.m3/unsaved_chats/" + fallback.name
    except Exception:
        display = str(fallback)
    msg = f"""
{border}
  M3 CHATLOG NOT SAVED - SESSION CONTEXT WILL BE LOST

  Agent  : opencode ({event})
  Reason : {reason}
  Saved  : {display}

  Fix    : restart m3 MCP, then re-run ingest on transcript above.
{border}
"""
    print(msg, file=sys.stderr, flush=True)


def main() -> int:
    raw = sys.stdin.read().strip()
    session_id = ""
    transcript = ""
    event_name = "SessionEnd"

    # OpenCode may or may not send a JSON envelope
    if raw:
        try:
            envelope = json.loads(raw)
            session_id = envelope.get("session_id", "")
            transcript = envelope.get("transcript_path", "")
            event_name = envelope.get("hook_event_name", "SessionEnd")
        except json.JSONDecodeError:
            pass

    repo = find_repo_root()
    ingest = repo / "bin" / "chatlog_ingest.py"
    if not ingest.exists():
        reason = f"chatlog_ingest.py not found under {repo} — set M3_HOME"
        fallback = write_fallback(transcript, session_id, event_name, reason)
        scream(reason, fallback, event_name)
        return 1

    py = find_python(repo)
    cmd = [py, str(ingest), "--format", "opencode"]
    if session_id:
        cmd += ["--session-id", session_id]
    if transcript:
        cmd += ["--transcript-path", transcript]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        output = result.stdout.strip()

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
        error = result_json.get("error") if result_json else "m3 ingest failed or unreachable"

        if written == 0 or error:
            reason = error if error else "0 rows written — m3 may be down or unreachable"
            fallback = write_fallback(transcript, session_id, event_name, reason)
            scream(reason, fallback, event_name)

        return result.returncode

    except subprocess.TimeoutExpired:
        reason = "ingest timed out after 60s — m3 unreachable"
        fallback = write_fallback(transcript, session_id, event_name, reason)
        scream(reason, fallback, event_name)
        return 1
    except Exception as e:
        reason = f"ingest failed: {e}"
        fallback = write_fallback(transcript, session_id, event_name, reason)
        scream(reason, fallback, event_name)
        return 1


if __name__ == "__main__":
    sys.exit(main())
