"""chatlog_ingest.py — CLI that reads a host-agent transcript file and writes
canonical chat-log rows via chatlog_core.chatlog_write_bulk_impl.

Invoked by host-agent hooks (Claude Code PreCompact/Stop, Gemini SessionEnd, etc.),
which receive a JSON envelope from the host and forward the transcript path as
--transcript-path. Parsers target the real on-disk transcript schemas, not a
hypothetical canonical format.

CLI:
  python bin/chatlog_ingest.py --format {claude-code,gemini-cli}
                               --transcript-path FILE
                               [--session-id ID] [--variant LABEL]

A per-session cursor at memory/.chatlog_ingest_cursor.json records which
message ids / indices have been ingested so re-invoking on the same transcript
(e.g. Stop hook every turn) stays idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("chatlog_ingest")


def infer_provider(model_id: str) -> str:
    """Map model_id prefix to provider."""
    if not model_id:
        return "other"
    if model_id.startswith("claude-"):
        return "anthropic"
    if model_id.startswith(("gemini-", "palm-")):
        return "google"
    if model_id.startswith(("gpt-", "o1-", "o3-")):
        return "openai"
    if model_id.startswith("grok-"):
        return "xai"
    if model_id.startswith("deepseek-"):
        return "deepseek"
    if model_id.startswith(("llama-", "mistral-", "qwen-")):
        return "local"
    return "other"


# ─── Claude Code parser ───────────────────────────────────────────────────────
# Real on-disk schema (one JSONL record per line at
# ~/.claude/projects/<slug>/<session-uuid>.jsonl):
#   {"type": "user"|"assistant"|"system"|"attachment"|"permission-mode"|
#            "file-history-snapshot",
#    "uuid": "...", "parentUuid": "...", "sessionId": "...", "timestamp": "...",
#    "cwd": "...", "version": "...", "gitBranch": "...", "userType": "external",
#    "message": {"role": "user"|"assistant",
#                "content": "str" | [{"type":"text","text":"..."}, ...],
#                "model": "claude-...", "usage": {"input_tokens": N, ...}}}
# Only user/assistant records carry chat content; the rest are skipped.

def _claude_content_to_text(content: Any) -> str:
    """Flatten Claude Code message.content to plain text.

    String content is returned as-is. List content (assistant blocks) is filtered
    to text-type blocks and joined; non-text blocks (tool_use, tool_result) are
    skipped — they aren't chat material.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _parse_claude_code(raw: str) -> tuple[list[dict], Optional[str]]:
    """Parse Claude Code JSONL. Returns (items, sessionId)."""
    items: list[dict] = []
    session_id: Optional[str] = None
    if not raw.strip():
        return items, session_id
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning(f"Skipping malformed JSONL line: {e}")
            continue
        rec_type = obj.get("type")
        if rec_type not in ("user", "assistant"):
            continue
        msg = obj.get("message") or {}
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = _claude_content_to_text(msg.get("content"))
        if not content:
            continue
        model = msg.get("model") or ""
        usage = msg.get("usage") or {}
        if session_id is None:
            session_id = obj.get("sessionId")
        items.append({
            "content": content,
            "role": role,
            "model_id": model or "unknown",
            "conversation_id": obj.get("sessionId", ""),
            "provider": infer_provider(model),
            "tokens_in": usage.get("input_tokens"),
            "tokens_out": usage.get("output_tokens"),
            "timestamp": obj.get("timestamp", ""),
            "uuid": obj.get("uuid"),
        })
    return items, session_id


# ─── Gemini CLI parser ────────────────────────────────────────────────────────
# Real on-disk schema at ~/.gemini/tmp/<projectHash>/chats/session-<ISO>-<id>.jsonl:
# JSONL — one JSON object per line, NOT a single object with a messages[] array.
#
# Line 1: session header
#   {"sessionId":"...","projectHash":"...","startTime":"...","lastUpdated":"...","kind":"main"}
#
# Subsequent lines are either turn records or $set ops (which we ignore):
#   {"id":"...","timestamp":"...","type":"user"|"gemini"|"info",
#    "content":"str"|[{"text":"..."},...], "tokens":{...}, "model":"...", ...}
#   {"$set":{"lastUpdated":"..."}}           — ignore
#
# "info" messages (CLI chrome) are skipped. "gemini" → assistant role.
# Observed 2026-04-24 on Gemini CLI 0.39.1. The older .json format (single
# object with messages[]) is still handled as a fallback for historical files.

def _gemini_content_to_text(content: Any) -> str:
    """Flatten Gemini message.content to plain text.

    User content is typically [{"text": "..."}]; assistant content is typically a
    string. Accept both; join text parts from lists.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _parse_gemini_cli(raw: str) -> tuple[list[dict], Optional[str]]:
    """Parse a Gemini CLI session transcript. Returns (items, sessionId).

    Handles both formats:
      1. Current (Gemini CLI 0.39+): JSONL — one JSON object per line. Line 1
         is the session header; subsequent lines are turn records or $set ops.
      2. Historical: single JSON object with a messages[] array (kept as
         fallback for any older transcripts that still exist).
    """
    if not raw.strip():
        return [], None

    messages: list[dict] = []
    session_id: Optional[str] = None

    stripped = raw.lstrip()
    if stripped.startswith("{") and "\n{" in stripped:
        # Looks like JSONL (multiple top-level objects separated by newlines).
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"Malformed Gemini JSONL line, skipping: {e}")
                continue
            if not isinstance(obj, dict):
                continue
            # Session header is the first non-$set object carrying sessionId.
            if session_id is None and obj.get("sessionId"):
                session_id = obj.get("sessionId")
                continue
            # $set ops are internal state updates — ignore.
            if "$set" in obj:
                continue
            # Treat everything else as a candidate message record.
            messages.append(obj)
    else:
        # Try the legacy single-object format.
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning(f"Malformed Gemini session JSON: {e}")
            return [], None
        session_id = data.get("sessionId")
        messages_field = data.get("messages")
        if isinstance(messages_field, list):
            messages = messages_field

    items: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        rec_type = msg.get("type")
        if rec_type == "user":
            role = "user"
        elif rec_type == "gemini":
            role = "assistant"
        else:
            continue
        content = _gemini_content_to_text(msg.get("content"))
        if not content:
            continue
        model = msg.get("model") or ""
        tokens = msg.get("tokens") or {}
        items.append({
            "content": content,
            "role": role,
            "model_id": model or "unknown",
            "conversation_id": session_id or "",
            "provider": "google",
            "tokens_in": tokens.get("input"),
            "tokens_out": tokens.get("output"),
            "timestamp": msg.get("timestamp", ""),
            "uuid": msg.get("id"),
        })
    return items, session_id


# ─── Cursor (per-sessionId idempotency) ───────────────────────────────────────

def _cursor_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "memory", ".chatlog_ingest_cursor.json")


def _load_cursor() -> dict:
    try:
        with open(_cursor_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cursor(state: dict) -> None:
    path = _cursor_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def _filter_new_items(items: list[dict], session_id: str, cursor: dict) -> list[dict]:
    """Drop items whose uuid is already in the cursor's seen-set for this session.
    Items without a uuid fall back to positional index within this batch."""
    if not session_id:
        return items
    seen: set[str] = set(cursor.get("sessions", {}).get(session_id, {}).get("seen_uuids", []))
    new_items: list[dict] = []
    for item in items:
        uuid_key = item.get("uuid")
        if uuid_key and uuid_key in seen:
            continue
        new_items.append(item)
    return new_items


def _commit_cursor(items: list[dict], session_id: str, cursor: dict) -> None:
    if not session_id:
        return
    sessions = cursor.setdefault("sessions", {})
    entry = sessions.setdefault(session_id, {"seen_uuids": []})
    seen_uuids = set(entry.get("seen_uuids", []))
    for item in items:
        u = item.get("uuid")
        if u:
            seen_uuids.add(u)
    entry["seen_uuids"] = sorted(seen_uuids)
    entry["last_ingested_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _save_cursor(cursor)


# ─── Normalization ────────────────────────────────────────────────────────────

def _make_agent_id(host_agent: str) -> str:
    user = os.environ.get("USER") or os.environ.get("USERNAME", "unknown")
    host = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or platform.node()
    # Windows hostnames are case-insensitive and COMPUTERNAME is the canonical
    # uppercase form. Ingestion under shells that don't inherit COMPUTERNAME
    # (git-bash / WSL -> platform.node() returns mixed case, e.g. "HostPc") would
    # otherwise fork the agent_id away from the canonical "HOSTPC". Normalize on
    # Windows only; leave case-significant POSIX hostnames untouched.
    if os.name == "nt":
        host = host.upper()
    return f"{host_agent}:{user}@{host}"


def _normalize(items: list[dict], host_agent: str, variant: Optional[str],
               session_override: str) -> list[dict]:
    agent_id = _make_agent_id(host_agent)
    user_id = os.environ.get("USER") or os.environ.get("USERNAME", "unknown")
    out: list[dict] = []
    for item in items:
        if session_override and not item.get("conversation_id"):
            item["conversation_id"] = session_override
        if not item.get("conversation_id"):
            item["conversation_id"] = "unknown"
        item["host_agent"] = host_agent
        item["agent_id"] = agent_id
        item["user_id"] = user_id
        if variant:
            item["variant"] = variant
        item.pop("uuid", None)  # internal; not part of write schema
        out.append(item)
    return out


def _openclaw_content_to_text(content) -> str:
    """Flatten OpenClaw message content to text.

    `content` is either a plain string or a list of typed parts; only `text`
    parts carry conversation. Verified against a real transcript under
    ~/.openclaw/agents/<agent>/sessions/*.jsonl rather than assumed.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return "\n".join(t for t in parts if t).strip()
    return ""


def _parse_openclaw(raw: str) -> tuple[list[dict], Optional[str]]:
    """Parse an OpenClaw session transcript. Returns (items, sessionId).

    JSONL, one object per line, of several `type`s — `session` (the header),
    `model_change`, `thinking_level_change`, `custom`, and `message`. Only
    `message` lines carry conversation:

        {"id": ..., "parentId": ..., "timestamp": ..., "type": "message",
         "message": {"role": "user"|"assistant", "content": ..., "timestamp": ...}}

    ⚠ SLASH COMMANDS ARE SKIPPED, matching OpenClaw's own bundled session-memory
    handler: `/new`, `/reset` and friends are control input, not conversation,
    and capturing them would file the command that ENDED a session as its last
    user turn.

    Tolerant by line (§3 degrade, don't abort): a malformed line is logged and
    skipped so one bad record cannot cost the whole transcript.
    """
    if not raw.strip():
        return [], None

    items: list[dict] = []
    session_id: Optional[str] = None
    model_id = ""

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning(f"Malformed OpenClaw JSONL line, skipping: {e}")
            continue
        if not isinstance(obj, dict):
            continue

        rec_type = obj.get("type")
        if rec_type == "session":
            session_id = obj.get("sessionId") or obj.get("id") or session_id
            continue
        if rec_type == "model_change":
            # Carries the model in force for every message that FOLLOWS it, so
            # later turns are attributed to the model that actually produced
            # them rather than to the session's opening model.
            model_id = obj.get("model") or obj.get("modelId") or model_id
            continue
        if rec_type != "message":
            continue

        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = _openclaw_content_to_text(msg.get("content"))
        if not content or content.startswith("/"):
            continue

        items.append({
            "content": content,
            "role": role,
            "model_id": model_id or "unknown",
            "conversation_id": session_id or "",
            "provider": "other",
            "timestamp": msg.get("timestamp") or obj.get("timestamp", ""),
            "uuid": obj.get("id"),
        })

    return items, session_id


_AIDER_SESSION_RE = re.compile(r"^# aider chat started at (.+)$")


def _parse_aider(raw: str) -> tuple[list[dict], Optional[str]]:
    """Parse an Aider chat history. Returns (items, sessionId).

    Aider writes MARKDOWN, not JSON: `.aider.chat.history.md` in the repo root.
    Grammar, confirmed against a real 2,434-line history (Aider 0.86.1):

        # aider chat started at 2026-01-11 11:59:16   <- session boundary
        > Terminal does not support pretty output     <- TOOL/system output
        #### how do I enable the repo map?            <- USER turn
        Aider disables repo-map by default because:   <- ASSISTANT reply
        ```bash                                       <- fenced code, still reply
        aider --repo-map
        ```
        > Tokens: 617 sent, 8 received.               <- accounting, not content

    ⚠ `>` LINES ARE NOT CONVERSATION. They are Aider's own console output —
    warnings, the command line it was invoked with, token counts, and prompts.
    Capturing them would file a UnicodeDecodeError warning as a user turn. That
    they LOOK like markdown quotes is a coincidence of the format.

    ⚠ NO SESSION ID EXISTS. Aider stamps a start time, not a uuid, and appends
    every session to ONE file. The timestamp of the most recent
    `# aider chat started at` is used as the conversation id so turns from
    different runs do not merge into one conversation.

    Assistant text accumulates until the next `####`, `>` or session header, so
    multi-paragraph replies and fenced code survive intact.
    """
    if not raw.strip():
        return [], None

    items: list[dict] = []
    session_id: Optional[str] = None
    role: Optional[str] = None
    buf: list[str] = []

    def _flush() -> None:
        if role is None:
            return
        text = "\n".join(buf).strip()
        if text:
            items.append({
                "content": text,
                "role": role,
                "model_id": "unknown",
                "conversation_id": session_id or "",
                "provider": "other",
                "timestamp": session_id or "",
                # No per-turn id exists in the format; index within the session
                # keeps the cursor's idempotency working across re-ingests.
                "uuid": f"{session_id or 'aider'}#{len(items)}",
            })

    for line in raw.splitlines():
        m = _AIDER_SESSION_RE.match(line)
        if m:
            _flush()
            role, buf = None, []
            session_id = m.group(1).strip()
            continue
        if line.startswith("####"):
            # ⚠ A MULTI-LINE USER MESSAGE REPEATS THE MARKER. Aider joins the
            # lines of one message with "  \n#### " (verified in its io.py
            # append_chat_history on main), so consecutive #### lines are ONE
            # turn, not several. Treating each as a new turn would shatter a
            # pasted stack trace into a dozen one-line "messages". My local
            # history happened to contain none, so this comes from the writer's
            # source rather than from the sample.
            text = line[5:] if line.startswith("#### ") else line[4:]
            if role == "user":
                buf.append(text)
            else:
                _flush()
                role, buf = "user", [text]
            continue
        if line.startswith(">"):
            # Console output ends whatever turn was accumulating.
            _flush()
            role, buf = None, []
            continue
        if role == "user":
            # First non-#### line after a user turn: the reply starts here.
            _flush()
            role, buf = "assistant", ([line] if line.strip() else [])
            continue
        if role == "assistant":
            buf.append(line)
            continue
        if line.strip():
            role, buf = "assistant", [line]

    _flush()
    return items, session_id


def opencode_store_candidates() -> "list[Path]":
    """Where an OpenCode store may live, most-specific first.

    ⚠ §1 CROSS-PLATFORM: THREE OSes, NOT ONE CONVENTION. `~/.local/share` is the
    XDG default and is what OpenCode uses on Linux and (observed) on Windows,
    but macOS applications commonly use `~/Library/Application Support`, and a
    user can relocate either with an env var. Hardcoding the XDG path in a shell
    script — which is what this replaced — bakes one OS's convention into a
    tool that must run on all three.

    Probing rather than deciding also keeps this honest: m3 does not control
    OpenCode's layout and should not pretend to know it. A candidate that is not
    there simply is not returned.

    One implementation in Python instead of one per shell wrapper (§10a): the
    .sh and .ps1 hooks were each growing their own copy of this list, which is
    exactly how two platforms drift apart.
    """
    env = os.environ.get("OPENCODE_DATA_DIR")
    if env:
        return [Path(env)]

    home = Path.home()
    cands = []
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        cands.append(Path(xdg) / "opencode")
    cands.append(home / ".local" / "share" / "opencode")
    cands.append(home / "Library" / "Application Support" / "opencode")  # macOS
    appdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        cands.append(Path(appdata) / "opencode")
    return [c for c in cands if c.exists()]


def _opencode_item(msg: dict, msg_id: str, session_id: str,
                   texts: list, created) -> "dict | None":
    """One chatlog item from an OpenCode message payload + its text parts.

    Shared by the SQLite and legacy-JSON readers: the `data` payload is
    byte-identical between them, only the STORAGE LOCATOR changed, so the two
    readers must not each interpret it (§10a).

    ⚠ THE MODEL FIELD MOVES BY ROLE. A user message carries nested
    `model: {providerID, modelID}`; an assistant message carries flat
    `providerID` / `modelID` alongside `tokens`. Reading only one shape silently
    attributes half the turns to "unknown". Verified on both layouts.
    """
    role = msg.get("role")
    if role not in ("user", "assistant"):
        return None
    content = "\n".join(t for t in texts if t).strip()
    if not content:
        return None  # tool-only, reasoning-only, or errored turn

    nested = msg.get("model") or {}
    tokens = msg.get("tokens") or {}

    # ⚠ OpenCode's providerID IS NOT ALWAYS A MODEL VENDOR. It names the route,
    # so a session through OpenCode's own gateway reports providerID="opencode",
    # which the chatlog schema rejects (VALID_PROVIDERS is a vendor enum). Real
    # data surfaced this: every turn in a live database failed to write.
    # Normalise to the enum and fall back to "other", which is what that value
    # is for -- widening the enum with a router name would make `provider`
    # answer two different questions.
    from chatlog_config import VALID_PROVIDERS

    raw_provider = msg.get("providerID") or nested.get("providerID") or ""
    provider = raw_provider if raw_provider in VALID_PROVIDERS else "other"

    return {
        "content": content,
        "role": role,
        "model_id": msg.get("modelID") or nested.get("modelID") or "unknown",
        "conversation_id": session_id or "",
        "provider": provider,
        "tokens_in": tokens.get("input"),
        "tokens_out": tokens.get("output"),
        # `created` is epoch MILLISECONDS, not seconds.
        "timestamp": (
            datetime.fromtimestamp(created / 1000, timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ") if created else ""
        ),
        "uuid": msg_id,
    }


def _parse_opencode_sqlite(db_path: "Path", session_id: Optional[str] = None):
    """Read an OpenCode session from opencode.db. Returns (items, sessionId).

    ⚠ THE CURRENT LAYOUT. OpenCode moved sessions into a single SQLite database
    in v1.2.0 (February 2026); the per-file JSON tree below is the legacy path.
    Schema read off a real 1.18.0 database rather than from documentation:

        session(id, project_id, title, time_created, ...)
        message(id, session_id, time_created, data)   data = the JSON payload
        part(id, message_id, session_id, time_created, data)

    The payload inside `data` is the SAME json the legacy files hold, minus the
    id/sessionID fields, which are now SQL columns. A message's text is spread
    across its `type: "text"` parts and must be ordered by time.

    Opened READ-ONLY: OpenCode may be running, and a chatlog ingest has no
    business writing to another tool's database.
    """
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        logger.warning(f"Cannot open OpenCode database {db_path}: {e}")
        return [], None

    try:
        if session_id is None:
            row = con.execute(
                "SELECT id FROM session ORDER BY time_updated DESC, "
                "time_created DESC LIMIT 1"
            ).fetchone()
            if not row:
                return [], None
            session_id = row[0]

        # Parts first, grouped by message, so the join is one pass each.
        texts: dict = {}
        for mid, data in con.execute(
            "SELECT message_id, data FROM part WHERE session_id = ? "
            "ORDER BY time_created, id", (session_id,)
        ):
            try:
                part = json.loads(data)
            except (TypeError, json.JSONDecodeError):
                continue
            if part.get("type") == "text" and part.get("text"):
                texts.setdefault(mid, []).append(part["text"])

        items = []
        for mid, created, data in con.execute(
            "SELECT id, time_created, data FROM message WHERE session_id = ? "
            "ORDER BY time_created, id", (session_id,)
        ):
            try:
                msg = json.loads(data)
            except (TypeError, json.JSONDecodeError) as e:
                logger.warning(f"Malformed OpenCode message {mid}, skipping: {e}")
                continue
            item = _opencode_item(msg, mid, session_id, texts.get(mid, []),
                                  (msg.get("time") or {}).get("created") or created)
            if item:
                items.append(item)
        return items, session_id
    except sqlite3.Error as e:
        logger.warning(f"OpenCode database read failed: {e}")
        return [], None
    finally:
        con.close()


def _parse_opencode(transcript_path: str) -> tuple[list[dict], Optional[str]]:
    """Parse an OpenCode session. Returns (items, sessionId).

    ⚠ NOT A TRANSCRIPT FILE. OpenCode has two storage layouts and neither is a
    single transcript, so `transcript_path` names a STORE, not a document:

      * v1.2.0+ (Feb 2026): one SQLite database, `opencode.db`. Pass the .db
        file, or the directory containing it.
      * legacy: a JSON tree — storage/message/<sessionID>/<messageID>.json for
        role/model/time, storage/part/<messageID>/<partID>.json for content.
        Pass the session's message directory.

    SQLite is preferred whenever it resolves, matching how OpenCode itself and
    other readers detect the layout; the JSON tree is still read so an
    un-migrated install keeps working.
    """
    # "auto" means "find the store yourself". The hooks pass this rather than
    # each re-deriving a per-OS default, so the candidate list has one owner
    # (§1: three OSes; §10a: copies drift).
    if transcript_path in ("auto", "", "-"):
        for cand in opencode_store_candidates():
            items, sid = _parse_opencode(str(cand))
            if items or sid:
                return items, sid
        return [], None

    given = Path(transcript_path)

    # SQLite: the path itself, a sibling of a legacy tree, or inside a dir.
    # ⚠ AN EXPLICIT PATH WINS. Probing upward for opencode.db from a legacy
    # message directory hijacked the caller's choice: passing
    # storage/message/<session>/ returned the SQLITE session instead, silently
    # capturing a different conversation than the one asked for. Only look for
    # the database when the caller did NOT name a legacy tree.
    is_legacy_tree = given.is_dir() and given.parent.name == "message"
    if not is_legacy_tree:
        for cand in (given, given / "opencode.db", given.parent / "opencode.db"):
            try:
                if cand.is_file() and cand.suffix == ".db":
                    items, sid = _parse_opencode_sqlite(cand)
                    if items or sid:
                        return items, sid
            except OSError:
                continue

    msg_dir = given
    if msg_dir.is_file():
        msg_dir = msg_dir.parent
    if not msg_dir.is_dir():
        return [], None

    part_root = msg_dir.parent.parent / "part"
    items: list[dict] = []
    session_id: Optional[str] = None

    for msg_file in sorted(msg_dir.glob("*.json")):
        try:
            msg = json.loads(msg_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Malformed OpenCode message {msg_file.name}, skipping: {e}")
            continue
        session_id = msg.get("sessionID") or session_id

        texts: list[str] = []
        part_dir = part_root / str(msg.get("id", ""))
        if part_dir.is_dir():
            for part_file in sorted(part_dir.glob("*.json")):
                try:
                    part = json.loads(part_file.read_text(encoding="utf-8", errors="replace"))
                except (OSError, json.JSONDecodeError):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    texts.append(part["text"])

        # Same interpreter as the SQLite reader — the payload is identical
        # between the layouts, so only the locator differs.
        item = _opencode_item(msg, msg.get("id", ""), session_id or "", texts,
                              (msg.get("time") or {}).get("created"))
        if item:
            items.append(item)

    return items, session_id


# ─── Main ─────────────────────────────────────────────────────────────────────

PARSERS = {
    "claude-code": _parse_claude_code,
    "gemini-cli":  _parse_gemini_cli,
    "antigravity-cli": _parse_gemini_cli,
    "openclaw": _parse_openclaw,
    "opencode": _parse_opencode,
    "aider": _parse_aider,
}

# Formats whose parser receives the STORE PATH rather than decoded text,
# because the host does not write a single transcript file. Keep this in step
# with PARSERS; test_host_agent_capture_parity asserts each entry is registered.
_PATH_PARSERS = frozenset({"opencode"})


async def _ingest(format_name: str, transcript_path: str,
                  session_override: str, variant: Optional[str]) -> dict:
    parser = PARSERS.get(format_name)
    if parser is None:
        return {"written": 0, "skipped": 0, "failed": 1,
                "error": f"unknown format: {format_name}"}

    # ⚠ NOT EVERY HOST WRITES A TRANSCRIPT FILE. OpenCode keeps sessions in a
    # SQLite database (v1.2.0+) or a directory tree of per-message JSON, so its
    # parser takes the STORE PATH and does its own reading. Everything else is
    # handed the decoded text. Requiring os.path.isfile for all formats is what
    # made a store-based host unrepresentable here.
    if format_name in _PATH_PARSERS:
        # "auto" is a DIRECTIVE, not a path: the parser resolves the store from
        # the per-OS candidate list. Exempt it from the existence check rather
        # than teaching every hook to compute a path it cannot know portably.
        if transcript_path not in ("auto", "", "-") and not os.path.exists(transcript_path):
            logger.warning(f"Transcript path not found: {transcript_path}")
            return {"written": 0, "skipped": 0, "failed": 0,
                    "error": f"transcript not found: {transcript_path}"}
        items, parsed_session = parser(transcript_path)
    else:
        if not os.path.isfile(transcript_path):
            logger.warning(f"Transcript path not found: {transcript_path}")
            return {"written": 0, "skipped": 0, "failed": 0,
                    "error": f"transcript not found: {transcript_path}"}
        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            logger.error(f"Failed to read {transcript_path}: {e}")
            return {"written": 0, "skipped": 0, "failed": 1, "error": str(e)}
        items, parsed_session = parser(raw)
    # Transcript's self-identifying sessionId wins — the per-item conversation_id
    # comes from there, so the cursor must use the same value to stay coherent.
    # session_override is a fallback for transcripts that don't self-identify.
    if session_override and parsed_session and session_override != parsed_session:
        logger.warning(
            "session_id mismatch: envelope=%r, transcript=%r; using transcript value",
            session_override, parsed_session,
        )
    session_id = parsed_session or session_override or ""

    if not items:
        logger.info(f"No parseable items in {transcript_path}")
        return {"written": 0, "skipped": 0, "failed": 0, "session_id": session_id}

    cursor = _load_cursor()
    new_items = _filter_new_items(items, session_id, cursor)
    skipped = len(items) - len(new_items)

    if not new_items:
        logger.info(f"All {len(items)} items already ingested for session {session_id}")
        return {"written": 0, "skipped": skipped, "failed": 0, "session_id": session_id}

    # Capture uuids before _normalize strips them — the cursor needs them.
    uuids_to_commit = [it.get("uuid") for it in new_items if it.get("uuid")]
    normalized = _normalize(new_items, format_name, variant, session_id)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import chatlog_core
    result = await chatlog_core.chatlog_write_bulk_impl(normalized, embed=False)
    written = len(result.get("written_ids", []))
    failed = result.get("failed", 0)
    spilled = result.get("spilled", 0)

    if written > 0:
        _commit_cursor([{"uuid": u} for u in uuids_to_commit], session_id, cursor)

    logger.info(f"Ingested {transcript_path}: written={written}, skipped={skipped}, "
                f"spilled={spilled}, failed={failed}, session_id={session_id}")
    return {
        "written": written, "skipped": skipped, "spilled": spilled, "failed": failed,
        "errors": result.get("errors", []), "session_id": session_id,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest a host-agent transcript into the chat log subsystem.")
    parser.add_argument("--format", required=True, choices=sorted(PARSERS.keys()),
                        help="Transcript format / host agent")
    parser.add_argument("--transcript-path", required=True, help="Path to the transcript file on disk")
    parser.add_argument("--session-id", default="",
                        help="Override conversation_id (defaults to parsed sessionId)")
    parser.add_argument("--variant", default=None,
                        help="Provenance tag (e.g. pre_compact, stop, session_end, test)")
    parser.add_argument("--db", default=None,
                        help="Deprecated: chatlog-only override. Prefer --database. "
                             "Sets CHATLOG_DB_PATH for the duration of the process.")
    parser.add_argument("--spill-dir", default=None,
                        help="Override spill directory for this run (dev smoke tests). "
                             "Prevents stale spill files from polluting production.")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from m3_sdk import add_database_arg
    add_database_arg(parser)
    args = parser.parse_args()

    if args.database or args.db or args.spill_dir:
        sys.path.insert(0, os.path.dirname(__file__))
        import chatlog_config
        if args.database:
            # Unified model: --database sets M3_DATABASE so main + chatlog
            # resolution both see it. CHATLOG_DB_PATH overrides that for the
            # chatlog-only case (kept for the legacy --db flag).
            os.environ["M3_DATABASE"] = args.database
            logger.info("Overriding DB path via M3_DATABASE: %s", args.database)
        if args.db:
            # Set the namespaced var so the getenv_compat reader picks it up via
            # the new name (no self-inflicted deprecation warning for m3's own
            # internal plumbing). The deprecated CHATLOG_DB_PATH still resolves.
            os.environ["M3_CHATLOG_DB_PATH"] = args.db
            logger.info("Overriding chatlog DB path via M3_CHATLOG_DB_PATH: %s", args.db)
        if args.spill_dir:
            chatlog_config.SPILL_DIR = args.spill_dir
            logger.info("Overriding spill dir: %s", args.spill_dir)
        chatlog_config.invalidate_cache()

    result = await _ingest(args.format, args.transcript_path, args.session_id, args.variant)

    # Phase E1: Auto-enqueue this conversation for Mastra Observer enrichment
    # if M3_AUTO_ENRICH=1. The drainer (m3_enrich --drain-queue) consumes
    # observation_queue rows independently — this hook just signals "this
    # conversation has new content, consider enriching it."
    #
    # Debounce: only enqueue when this ingest wrote >= M3_AUTO_ENRICH_MIN_TURNS
    # turns (default 10). Single-turn pings, status checks, and short
    # acks shouldn't keep the drainer busy.
    if os.environ.get("M3_AUTO_ENRICH", "0").strip().lower() in ("1", "true", "yes", "on"):
        try:
            min_turns = int(os.environ.get("M3_AUTO_ENRICH_MIN_TURNS", "10"))
        except ValueError:
            min_turns = 10
        written = result.get("written", 0)
        sid = result.get("session_id") or args.session_id
        if sid and written >= min_turns:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            try:
                import memory_core as _mc
                # observation_enqueue_impl is idempotent: INSERT OR IGNORE on
                # conversation_id, so re-enqueuing the same conversation is a
                # no-op. user_id may be empty for some chatlog flows; that's
                # fine — the drainer scopes by conversation_id alone for
                # chatlog rows.
                enqueue_result = _mc.observation_enqueue_impl(
                    conversation_id=sid, user_id="",
                )
                logger.info(f"M3_AUTO_ENRICH: {enqueue_result} for {sid[:12]} "
                            f"({written} new turns)")
            except Exception as e:
                logger.warning(f"M3_AUTO_ENRICH enqueue failed (non-fatal): "
                               f"{type(e).__name__}: {e}")

    # Shutdown drain: chatlog_write_bulk_impl enqueues rows on an async
    # Queue drained by the _flush_loop background task. asyncio.run()
    # cancels tasks without awaiting their drain, so rows in flight when
    # main() returns get spilled to disk (or lost entirely if the executor
    # is torn down mid-insert, surfacing as "cannot schedule new futures
    # after interpreter shutdown"). Explicitly drain the queue and cancel
    # the loop before returning so every row either lands in the DB or
    # reaches the spill file cleanly.
    try:
        import chatlog_core as _cc
        if _cc._QUEUE is not None:
            # Drain remaining items. _flush_once is idempotent once the
            # queue is empty so calling in a loop is safe.
            while _cc._QUEUE.qsize() > 0:
                written = await _cc._flush_once()
                if written == 0:
                    break  # nothing flushable (likely already spilled)
        if _cc._FLUSH_TASK is not None and not _cc._FLUSH_TASK.done():
            _cc._FLUSH_TASK.cancel()
            try:
                await _cc._FLUSH_TASK
            except (asyncio.CancelledError, Exception):
                pass
    except Exception as e:
        logger.warning(f"Shutdown drain non-fatal error: {type(e).__name__}: {e}")

    print(json.dumps(result, indent=2))
    return 0 if result.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
