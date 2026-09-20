#!/usr/bin/env python3
"""OpenClaw session-end hook -> m3-memory chatlog ingest.

Cross-platform (Windows, macOS, Linux). Finds the OpenClaw session transcript,
runs chatlog_ingest.py, and SCREAMS loudly if capture fails, writing a fallback
file to ~/.m3/unsaved_chats/ so no session is silently lost.

Handles: command:new, command:reset (OpenClaw's own session-memory hook fires on
the same events -- those are the points at which a session is retired).

⚠ NO STDIN ENVELOPE. Unlike the Claude/Gemini/OpenCode hooks, OpenClaw does not
hand the hook a transcript path, so this discovers the newest session JSONL
under the agent's state dir itself. An envelope IS accepted if one is ever
supplied ({"session_id": ..., "transcript_path": ...}), and takes precedence --
so this keeps working if OpenClaw grows one.

Transcript layout, verified against a live install (OpenClaw 2026.3.28):
  ~/.openclaw/agents/<agent>/sessions/<session-uuid>.jsonl
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

AGENT = "openclaw"
FORMAT = "openclaw"


# ── Shared core (parity across all m3 agent hooks) ───────────────────────────
# ── Shared core (parity across all m3 agent hooks) ───────────────────────────

def find_repo_root() -> Path:
    m3_home = os.environ.get("M3_HOME")
    if m3_home:
        return Path(m3_home)
    return Path(__file__).resolve().parents[3]


def usable_interpreter(candidate: Path) -> bool:
    """True only if `candidate` can import the chatlog_ingest dependencies.

    Existence is NOT usability. A bare `python -m venv` satisfies .exists() but
    dies with ModuleNotFoundError when chatlog_ingest imports m3_sdk -> httpx,
    producing no parseable JSON and a generic "ingest failed or unreachable".
    See claude_code_precompact.py for the 2026-08-09..11 regression this
    guards against. Probe the import instead of trusting the path.
    """
    try:
        return subprocess.run(
            [str(candidate), "-c", "import httpx"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt" else 0,
        ).returncode == 0
    except Exception:
        return False


def find_python(repo: Path) -> str:
    for candidate in [
        repo / ".venv" / "Scripts" / "python.exe",
        repo / ".venv" / "bin" / "python",
    ]:
        if candidate.exists() and usable_interpreter(candidate):
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


# ── OpenClaw-specific: find the transcript ───────────────────────────────────

def openclaw_state_dir() -> Path:
    """OpenClaw's state root. OPENCLAW_STATE_DIR wins if set."""
    env = os.environ.get("OPENCLAW_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".openclaw"


def newest_transcript() -> "tuple[str, str]":
    """(transcript_path, session_id) for the most recently modified session.

    Returns ("", "") when there is nothing to ingest, which is a clean no-op
    rather than a failure -- a fresh install with no sessions yet is normal.

    Scans every agent, not just `main`: OpenClaw supports multiple agents under
    agents/<name>/sessions/ and hooks fire per gateway, not per agent.
    """
    sessions_root = openclaw_state_dir() / "agents"
    if not sessions_root.is_dir():
        return "", ""
    newest = None
    newest_mtime = -1.0
    try:
        for agent_dir in sessions_root.iterdir():
            sess_dir = agent_dir / "sessions"
            if not sess_dir.is_dir():
                continue
            for path in sess_dir.glob("*.jsonl"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime > newest_mtime:
                    newest, newest_mtime = path, mtime
    except OSError:
        return "", ""
    if newest is None:
        return "", ""
    # The filename IS the session id (verified on a live install); the parser
    # also recovers it from the header line, and an explicit --session-id keeps
    # the cursor keyed correctly even if a transcript is missing its header.
    return str(newest), newest.stem


def main() -> int:
    raw = ""
    try:
        if not sys.stdin.isatty():
            raw = sys.stdin.read().strip()
    except (OSError, ValueError):
        raw = ""  # no stdin attached — normal for OpenClaw

    session_id = ""
    transcript = ""
    event_name = "command:reset"

    # Accept an envelope if one is ever provided, so this hook does not need
    # rewriting the day OpenClaw starts sending one.
    if raw:
        try:
            envelope = json.loads(raw)
            if isinstance(envelope, dict):
                session_id = envelope.get("session_id", "") or ""
                transcript = envelope.get("transcript_path", "") or ""
                event_name = envelope.get("hook_event_name", event_name)
        except json.JSONDecodeError:
            pass  # OpenClaw sends nothing today — discover below

    if not transcript:
        transcript, discovered_id = newest_transcript()
        session_id = session_id or discovered_id

    variant = "session_end"

    # No transcript is a clean no-op, not an m3 failure. Screaming here would
    # fire on every /new in a fresh workspace and train the user to ignore the
    # one that matters (§3: a false alarm is a violation in its own right).
    if not transcript:
        print(f"{AGENT} hook: no OpenClaw session transcript found under "
              f"{openclaw_state_dir()} — nothing to ingest (no-op).",
              file=sys.stderr)
        return 0

    repo = find_repo_root()
    ingest = repo / "bin" / "chatlog_ingest.py"
    if not ingest.exists():
        reason = f"chatlog_ingest.py not found under {repo} — set M3_HOME"
        fallback = write_fallback(transcript, session_id, AGENT, event_name, variant, reason)
        scream(reason, fallback, AGENT, event_name)
        return 1

    py = find_python(repo)
    args = ["--format", FORMAT, "--variant", variant,
            "--transcript-path", transcript]
    if session_id:
        args += ["--session-id", session_id]

    written, skipped, failed, error, rc = run_ingest(py, ingest, args)

    # Success is reachability, NOT written>0 — see the OpenCode hook for the
    # full rationale: the live MCP server usually captured the turns already, so
    # skipped=N with written=0 is a success.
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
