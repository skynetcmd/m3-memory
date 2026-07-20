#!/usr/bin/env python3
"""OpenCode session-end hook -> m3-memory chatlog ingest.

Cross-platform (Windows, macOS, Linux). Reads the OpenCode hook envelope
from stdin, runs chatlog_ingest.py, and SCREAMS loudly if capture fails,
writing a fallback file to ~/.m3/unsaved_chats/ so no session is silently lost.

Handles: SessionEnd

Envelope (stdin JSON, optional):
  { "session_id": "...", "transcript_path": "...",
    "cwd": "...", "hook_event_name": "SessionEnd" }
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

AGENT = "opencode"
FORMAT = "opencode"


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


def _extract_last_json_object(output: str):
    """Return the last balanced top-level {...} JSON object in `output`, or None.

    ingest interleaves log lines with a final PRETTY-PRINTED JSON object. The old
    parser scanned for a line *starting with* '{' and json.loads'd that single
    line — which is just '{' for pretty JSON, so it always raised and reported
    written=0 (the 1485-bogus-files bug). Scan from the end for the last '}', walk
    backwards tracking brace depth (ignoring braces in strings), json.loads the
    slice. Handles compact single-line and multi-line pretty JSON alike.
    """
    end = output.rfind("}")
    if end == -1:
        return None
    depth = 0
    in_str = False
    for i in range(end, -1, -1):
        ch = output[i]
        if in_str:
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
    """Run ingest, return (written, skipped, failed, error, returncode)."""
    try:
        # CREATE_NO_WINDOW: this hook fires in the background at session end; a
        # bare python.exe child would flash a console window. getattr keeps the
        # attribute reference valid on non-Windows (0 = default, no-op).
        _flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        result = subprocess.run(
            [py, str(ingest)] + extra_args,
            capture_output=True, text=True, timeout=60, creationflags=_flags)
        output = result.stdout.strip()
        result_json = _extract_last_json_object(output)
        if not result_json:
            return 0, 0, 1, "m3 ingest failed or unreachable", result.returncode or 1
        written = int(result_json.get("written", 0))
        skipped = int(result_json.get("skipped", 0))
        failed = int(result_json.get("failed", 0))
        error = result_json.get("error")
        return written, skipped, failed, error, result.returncode
    except subprocess.TimeoutExpired:
        return 0, 0, 1, "ingest timed out after 60s — m3 unreachable", 1
    except Exception as e:
        return 0, 0, 1, f"ingest failed: {e}", 1


# ── Agent-specific main ───────────────────────────────────────────────────────

def main() -> int:
    raw = sys.stdin.read().strip()
    session_id = ""
    transcript = ""
    event_name = "SessionEnd"

    # OpenCode envelope is optional
    if raw:
        try:
            envelope = json.loads(raw)
            session_id = envelope.get("session_id", "")
            transcript = envelope.get("transcript_path", "")
            event_name = envelope.get("hook_event_name", "SessionEnd")
        except json.JSONDecodeError:
            pass  # OpenCode may not send JSON — proceed with defaults

    variant = "session_end"

    # chatlog_ingest.py requires --transcript-path (argparse required=True). A
    # transcript-less SessionEnd would otherwise shell into an argparse exit-2
    # (empty stdout), which run_ingest misreads as "m3 unreachable" — a FALSE
    # alarm + bogus fallback on every such event even when m3 is healthy. A
    # SessionEnd with no transcript is a clean no-op, not an m3 failure: return 0
    # without screaming or writing a fallback. (Mirrors the Claude/Gemini hooks'
    # missing-transcript guard.)
    if not transcript:
        print(f"{AGENT} hook: SessionEnd envelope has no transcript_path — "
              f"nothing to ingest (no-op).", file=sys.stderr)
        return 0

    repo = find_repo_root()
    ingest = repo / "bin" / "chatlog_ingest.py"
    if not ingest.exists():
        reason = f"chatlog_ingest.py not found under {repo} — set M3_HOME"
        fallback = write_fallback(transcript, session_id, AGENT, event_name, variant, reason)
        scream(reason, fallback, AGENT, event_name)
        return 1

    py = find_python(repo)
    args = ["--format", FORMAT, "--variant", variant]
    if session_id:
        args += ["--session-id", session_id]
    if transcript:
        args += ["--transcript-path", transcript]

    written, skipped, failed, error, rc = run_ingest(py, ingest, args)

    # Success is reachability, NOT written>0. The live MCP server captures turns
    # itself; by the time this exit hook ingests, they are usually already in the
    # DB, so ingest reports written=0, skipped=N — a SUCCESS, not a loss. A real
    # failure is: error set, failed>0, or nothing seen at all (written==skipped==0).
    captured_nothing = (written == 0 and skipped == 0)
    if error or failed > 0 or captured_nothing:
        if error:
            reason = error
        elif failed > 0:
            reason = f"{failed} turn(s) failed to write — m3 may be degraded"
        else:
            reason = "0 rows seen — transcript empty or m3 unreachable"
        fallback = write_fallback(transcript, session_id, AGENT, event_name, variant, reason)
        scream(reason, fallback, AGENT, event_name)
        return rc if rc != 0 else 1

    return rc


if __name__ == "__main__":
    sys.exit(main())
