"""chatlog_ingest.py — single-entry-point CLI for ingesting host-agent chat logs.

Normalizes logs from claude-code, gemini-cli, opencode, and aider into canonical
format and writes to the chat log subsystem via chatlog_core.chatlog_write_bulk_impl.

Usage:
  python bin/chatlog_ingest.py --format {claude-code,gemini-cli,opencode,aider,auto} [--watch DIR] [--conversation-id ID] [input-file]
  - Reads stdin when no input file given.
  - --watch mode: polls a directory for new/updated log files, keeps a cursor at
    memory/.chatlog_ingest_cursor.json (atomic rename on update), debounces 500ms.
    Exits cleanly on SIGTERM or KeyboardInterrupt.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import platform
import signal
import sys
import time

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


def _parse_claude_code(raw: str) -> list[dict]:
    """Parse claude-code JSONL: lines with type=message, role, content, model, conversation_id, usage."""
    out = []
    if not raw.strip():
        return out
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning(f"Skipping malformed JSON line: {e}")
            continue
        if obj.get("type") != "message":
            continue
        role = obj.get("role", "")
        content = obj.get("content", "")
        model = obj.get("model", "unknown")
        conversation_id = obj.get("conversation_id", "")
        usage = obj.get("usage", {})
        tokens_in = usage.get("input_tokens")
        tokens_out = usage.get("output_tokens")
        if not content or not role:
            continue
        provider = infer_provider(model)
        out.append({
            "content": content,
            "role": role,
            "model_id": model,
            "conversation_id": conversation_id,
            "provider": provider,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        })
    return out


def _parse_gemini_cli(raw: str) -> list[dict]:
    """Parse gemini-cli JSON: {history: [{role, parts, model}, ...]}. Flatten parts to content."""
    out = []
    if not raw.strip():
        return out
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"Malformed JSON: {e}")
        return out
    history = obj.get("history", [])
    if not isinstance(history, list):
        return out
    for msg in history:
        role = msg.get("role", "")
        parts = msg.get("parts", [])
        model = msg.get("model", "gemini-unknown")
        if not role or not parts:
            continue
        content_parts = []
        for part in parts:
            if isinstance(part, dict):
                content_parts.append(part.get("text", ""))
            elif isinstance(part, str):
                content_parts.append(part)
        content = "".join(content_parts).strip()
        if not content:
            continue
        out.append({
            "content": content,
            "role": role,
            "model_id": model,
            "provider": "google",
        })
    return out


def _parse_opencode(raw: str) -> list[dict]:
    """Parse opencode JSONL: {role, parts, model, session_id, ...}."""
    out = []
    if not raw.strip():
        return out
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning(f"Skipping malformed JSON line: {e}")
            continue
        role = obj.get("role", "")
        parts = obj.get("parts", [])
        model = obj.get("model", "unknown")
        if not role or not parts:
            continue
        content_parts = []
        for part in parts:
            if isinstance(part, dict):
                content_parts.append(part.get("text", ""))
            elif isinstance(part, str):
                content_parts.append(part)
        content = "".join(content_parts).strip()
        if not content:
            continue
        provider = infer_provider(model)
        out.append({
            "content": content,
            "role": role,
            "model_id": model,
            "provider": provider,
        })
    return out


def _parse_aider(raw: str) -> list[dict]:
    """Parse aider markdown: turns separated by #### headers, with > USER: and assistant blocks."""
    out = []
    if not raw.strip():
        return out
    lines = raw.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#### "):
            i += 1
            role = None
            content_lines = []
            while i < len(lines) and not lines[i].startswith("#### "):
                if lines[i].startswith("> USER:"):
                    role = "user"
                    rest = lines[i][7:].strip()
                    if rest:
                        content_lines.append(rest)
                elif role and lines[i].startswith(">"):
                    content_lines.append(lines[i][1:].strip())
                elif role and not lines[i].startswith(">"):
                    if content_lines or lines[i].strip():
                        content_lines.append(lines[i])
                else:
                    content_lines.append(lines[i])
                i += 1
            if role and content_lines:
                content = "\n".join(content_lines).strip()
                if content:
                    out.append({
                        "content": content,
                        "role": role,
                        "model_id": "unknown",
                        "provider": "other",
                        "host_agent": "aider",
                    })
        else:
            i += 1
    return out


def _sniff_format(data: str) -> str:
    """Sniff format from first 4KB: claude-code, gemini, opencode, aider."""
    head = data[:4096]
    if head.strip().startswith("{") and '"type":"message"' in head:
        return "claude-code"
    if '{"history":' in head or '"history":[' in head:
        return "gemini-cli"
    if '"parts":' in head and '"role":' in head:
        return "opencode"
    if "#### " in head or "> USER:" in head:
        return "aider"
    return "auto"


def normalize_items(items: list[dict], format_name: str, conversation_id: str = "",
                   host_agent: str = "", model_id_override: str = "") -> list[dict]:
    """Enrich items with required fields: host_agent, conversation_id, agent_id."""
    out = []
    for item in items:
        if not item.get("conversation_id") and conversation_id:
            item["conversation_id"] = conversation_id
        if not item.get("host_agent"):
            if host_agent:
                item["host_agent"] = host_agent
            else:
                item["host_agent"] = format_name
        if model_id_override and (not item.get("model_id") or item["model_id"] == "unknown"):
            item["model_id"] = model_id_override
        if not item.get("conversation_id"):
            item["conversation_id"] = "unknown"
        if not item.get("model_id"):
            item["model_id"] = "unknown"
        out.append(item)
    return out


def derive_conversation_id(filename: str) -> str:
    """Hash filename to 16-char conversation_id."""
    return hashlib.blake2b(filename.encode(), digest_size=8).hexdigest()


def make_agent_id() -> str:
    """Build agent_id: host_agent:username@hostname."""
    user = os.environ.get("USER") or os.environ.get("USERNAME", "unknown")
    host = platform.node()
    return f"ingest:{user}@{host}"


def read_cursor() -> dict:
    """Load cursor state from INGEST_CURSOR."""
    cursor_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memory", ".chatlog_ingest_cursor.json")
    try:
        with open(cursor_path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_cursor(state: dict) -> None:
    """Atomic save of cursor state."""
    cursor_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memory", ".chatlog_ingest_cursor.json")
    os.makedirs(os.path.dirname(cursor_path), exist_ok=True)
    tmp = cursor_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, cursor_path)


async def ingest_file(filepath: str, format_name: str, conversation_id: str = "",
                     model_id_override: str = "") -> dict:
    """Read, parse, and ingest a single file. Returns summary dict."""
    sys.path.insert(0, os.path.dirname(__file__))
    import chatlog_core

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        logger.error(f"Failed to read {filepath}: {e}")
        return {"written": 0, "spilled": 0, "failed": 1, "errors": [str(e)]}

    if not raw.strip():
        return {"written": 0, "spilled": 0, "failed": 0, "errors": []}

    if format_name == "auto":
        format_name = _sniff_format(raw)
        logger.info(f"Auto-detected format: {format_name}")

    if format_name == "claude-code":
        items = _parse_claude_code(raw)
    elif format_name == "gemini-cli":
        items = _parse_gemini_cli(raw)
    elif format_name == "opencode":
        items = _parse_opencode(raw)
    elif format_name == "aider":
        items = _parse_aider(raw)
    else:
        logger.error(f"Unknown format: {format_name}")
        return {"written": 0, "spilled": 0, "failed": 1, "errors": [f"Unknown format: {format_name}"]}

    if not conversation_id:
        conversation_id = derive_conversation_id(os.path.basename(filepath))

    items = normalize_items(items, format_name, conversation_id, format_name, model_id_override)

    if not items:
        logger.warning(f"No items parsed from {filepath}")
        return {"written": 0, "spilled": 0, "failed": 0, "errors": []}

    agent_id = make_agent_id()
    for item in items:
        item["agent_id"] = agent_id
        item["user_id"] = os.environ.get("USER") or os.environ.get("USERNAME", "unknown")

    result = await chatlog_core.chatlog_write_bulk_impl(items, embed=False)

    written = len(result.get("written_ids", []))
    spilled = result.get("spilled", 0)
    failed = result.get("failed", 0)
    errors = result.get("errors", [])

    logger.info(f"Ingested {filepath}: written={written}, spilled={spilled}, failed={failed}")
    return {"written": written, "spilled": spilled, "failed": failed, "errors": errors}


async def watch_directory(watch_dir: str, format_name: str, conversation_id: str = "",
                         model_id_override: str = "") -> None:
    """Poll directory for new/updated log files. Debounce 500ms. Exit on SIGTERM/CTRL-C."""
    logger.info(f"Watching {watch_dir} for {format_name} logs...")
    cursor = read_cursor()
    tracked = cursor.get("tracked_files", {})

    shutdown_event = asyncio.Event()

    def handle_signal(signum, frame):
        logger.info("Received signal, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    last_scan = 0.0
    while not shutdown_event.is_set():
        now = time.time()
        if now - last_scan < 0.5:
            await asyncio.sleep(0.1)
            continue
        last_scan = now

        if not os.path.isdir(watch_dir):
            logger.warning(f"Watch directory {watch_dir} does not exist")
            await asyncio.sleep(1.0)
            continue

        try:
            for entry in os.scandir(watch_dir):
                if entry.is_file() and (entry.name.endswith(".jsonl") or entry.name.endswith(".md") or entry.name.endswith(".json")):
                    file_path = entry.path
                    mtime = entry.stat().st_mtime
                    tracked_mtime = tracked.get(file_path, 0)
                    if mtime > tracked_mtime:
                        logger.info(f"Processing {file_path}")
                        result = await ingest_file(file_path, format_name, conversation_id, model_id_override)
                        tracked[file_path] = mtime
                        logger.info(f"Result: {result}")
        except OSError as e:
            logger.error(f"Error scanning directory: {e}")

        cursor["tracked_files"] = tracked
        write_cursor(cursor)
        await asyncio.sleep(0.5)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest chat logs into the chat log subsystem.")
    parser.add_argument("--format", choices=["claude-code", "gemini-cli", "opencode", "aider", "auto"],
                       default="auto", help="Log format")
    parser.add_argument("--watch", type=str, help="Poll directory for new log files")
    parser.add_argument("--conversation-id", type=str, default="", help="Override conversation_id")
    parser.add_argument("--model", type=str, default="", help="Override model_id (aider)")
    parser.add_argument("input_file", nargs="?", help="Input file (stdin if omitted)")

    args = parser.parse_args()

    if args.watch:
        await watch_directory(args.watch, args.format, args.conversation_id, args.model)
        return

    if args.input_file:
        result = await ingest_file(args.input_file, args.format, args.conversation_id, args.model)
        print(json.dumps(result, indent=2))
    else:
        raw = sys.stdin.read()
        sys.path.insert(0, os.path.dirname(__file__))
        import chatlog_core

        if args.format == "auto":
            args.format = _sniff_format(raw)

        if args.format == "claude-code":
            items = _parse_claude_code(raw)
        elif args.format == "gemini-cli":
            items = _parse_gemini_cli(raw)
        elif args.format == "opencode":
            items = _parse_opencode(raw)
        elif args.format == "aider":
            items = _parse_aider(raw)
        else:
            print(json.dumps({"error": f"Unknown format: {args.format}"}))
            return

        conversation_id = args.conversation_id or derive_conversation_id("stdin")
        items = normalize_items(items, args.format, conversation_id, args.format, args.model)

        agent_id = make_agent_id()
        for item in items:
            item["agent_id"] = agent_id
            item["user_id"] = os.environ.get("USER") or os.environ.get("USERNAME", "unknown")

        result = await chatlog_core.chatlog_write_bulk_impl(items, embed=False)
        print(json.dumps({
            "written": len(result.get("written_ids", [])),
            "spilled": result.get("spilled", 0),
            "failed": result.get("failed", 0),
            "errors": result.get("errors", []),
        }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
