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
import sys
from datetime import datetime, timezone
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
    host = platform.node()
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


# ─── Main ─────────────────────────────────────────────────────────────────────

PARSERS = {
    "claude-code": _parse_claude_code,
    "gemini-cli":  _parse_gemini_cli,
}


async def _ingest(format_name: str, transcript_path: str,
                  session_override: str, variant: Optional[str]) -> dict:
    if not os.path.isfile(transcript_path):
        logger.warning(f"Transcript path not found: {transcript_path}")
        return {"written": 0, "skipped": 0, "failed": 0,
                "error": f"transcript not found: {transcript_path}"}

    parser = PARSERS.get(format_name)
    if parser is None:
        return {"written": 0, "skipped": 0, "failed": 1,
                "error": f"unknown format: {format_name}"}

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
            os.environ["CHATLOG_DB_PATH"] = args.db
            logger.info("Overriding chatlog DB path via CHATLOG_DB_PATH: %s", args.db)
        if args.spill_dir:
            chatlog_config.SPILL_DIR = args.spill_dir
            logger.info("Overriding spill dir: %s", args.spill_dir)
        chatlog_config.invalidate_cache()

    result = await _ingest(args.format, args.transcript_path, args.session_id, args.variant)

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
