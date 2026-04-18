"""chatlog_status.py — single-call summary of the chat log subsystem state.

Exports:
- chatlog_status_impl() -> str : returns JSON summary
- CLI: python bin/chatlog_status.py [--json]

Returns row counts from SQLite; everything else from state file + config.
Cold call <50ms (no full table scans).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

import chatlog_config

logger = logging.getLogger("chatlog_status")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_state_file() -> dict[str, Any]:
    state_path = chatlog_config.STATE_FILE
    if not os.path.exists(state_path):
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _get_row_counts(config: chatlog_config.ChatlogConfig) -> dict[str, int]:
    counts = {
        "main_chat_log_rows": 0,
        "chatlog_rows": 0,
        "chatlog_without_embed": 0,
    }

    try:
        main_db = config.effective_db_path() if config.mode == "integrated" else chatlog_config.MAIN_DB_PATH

        if config.mode in ("integrated", "hybrid"):
            try:
                conn = sqlite3.connect(main_db, timeout=5)
                conn.row_factory = sqlite3.Row
                try:
                    row = conn.execute(
                        "SELECT COUNT(*) as cnt FROM memory_items WHERE type='chat_log'"
                    ).fetchone()
                    counts["main_chat_log_rows"] = row["cnt"] if row else 0
                finally:
                    conn.close()
            except sqlite3.Error:
                pass

        if config.mode in ("separate", "hybrid"):
            chatlog_db = config.db_path
            if os.path.exists(chatlog_db):
                try:
                    conn = sqlite3.connect(chatlog_db, timeout=5)
                    conn.row_factory = sqlite3.Row
                    try:
                        row = conn.execute(
                            "SELECT COUNT(*) as cnt FROM memory_items"
                        ).fetchone()
                        counts["chatlog_rows"] = row["cnt"] if row else 0

                        row = conn.execute(
                            "SELECT COUNT(*) as cnt FROM memory_items mi "
                            "WHERE mi.type='chat_log' "
                            "AND mi.id NOT IN (SELECT memory_id FROM memory_embeddings)"
                        ).fetchone()
                        counts["chatlog_without_embed"] = row["cnt"] if row else 0
                    finally:
                        conn.close()
                except sqlite3.Error:
                    pass

    except Exception as e:
        logger.warning(f"Error fetching row counts: {e}")

    return counts


def _compute_warnings(
    state: dict[str, Any],
    config: chatlog_config.ChatlogConfig,
    row_counts: dict[str, int],
) -> list[str]:
    warnings = []

    if config.redaction.enabled:
        regex_errors = state.get("redaction", {}).get("regex_errors", [])
        if regex_errors:
            warnings.append("redaction regex compilation failed")

    spill = state.get("spill", {})
    if spill.get("bytes", 0) > 0:
        oldest_ms = spill.get("oldest_ms_ago", 0)
        if oldest_ms > 3_600_000:
            warnings.append("spill files older than 1h")

    queue_state = state.get("queue", {})
    depth = queue_state.get("depth", 0)
    max_depth = queue_state.get("max", config.queue_max_depth)
    if max_depth > 0 and depth / max_depth > 0.8:
        warnings.append(f"queue at {depth}/{max_depth}")

    hooks = config.host_agents
    for hook_name, hook_spec in hooks.items():
        if hook_spec.enabled:
            last_write_ms = state.get("hooks", {}).get(hook_name, {}).get("last_write_ms_ago", float("inf"))
            if last_write_ms > 86_400_000:
                warnings.append(f"{hook_name} silent 24h+")

    if "chatlog_without_embed" in row_counts:
        embed_backlog = row_counts["chatlog_without_embed"]
        if embed_backlog > 10_000:
            warnings.append(f"embed backlog {embed_backlog}")

    return warnings


def chatlog_status_impl() -> str:
    config = chatlog_config.resolve_config()
    state = _load_state_file()
    row_counts = _get_row_counts(config)

    result = {
        "mode": config.mode,
        "db_paths": {
            "main": chatlog_config.MAIN_DB_PATH,
            "chatlog": config.db_path,
        },
        "row_counts": row_counts,
        "queue": {
            "depth": state.get("queue", {}).get("depth", 0),
            "max": state.get("queue", {}).get("max", config.queue_max_depth),
            "last_flush_ms_ago": state.get("queue", {}).get("last_flush_ms_ago"),
        },
        "spill": {
            "files": len([f for f in os.listdir(chatlog_config.SPILL_DIR) if f.endswith(".jsonl")])
            if os.path.exists(chatlog_config.SPILL_DIR)
            else 0,
            "bytes": state.get("spill", {}).get("bytes", 0),
            "oldest_ms_ago": state.get("spill", {}).get("oldest_ms_ago"),
        },
        "hooks": {
            name: {
                "enabled": spec.enabled,
                "last_write_ms_ago": state.get("hooks", {}).get(name, {}).get("last_write_ms_ago"),
            }
            for name, spec in config.host_agents.items()
        },
        "redaction": {
            "enabled": config.redaction.enabled,
            "groups": config.redaction.patterns,
            "regex_errors": state.get("redaction", {}).get("regex_errors", []),
        },
        "last_write_at": state.get("last_write_at"),
        "warnings": _compute_warnings(state, config, row_counts),
    }

    return json.dumps(result, indent=2)


def _format_table(data: dict[str, Any]) -> str:
    lines = []
    lines.append("=== Chat Log Status ===")
    lines.append(f"Mode: {data['mode']}")
    lines.append(f"Chatlog DB: {data['db_paths']['chatlog']}")
    lines.append(f"Rows (main/chatlog/unembedded): {data['row_counts']['main_chat_log_rows']}/{data['row_counts']['chatlog_rows']}/{data['row_counts']['chatlog_without_embed']}")
    lines.append(f"Queue: {data['queue']['depth']}/{data['queue']['max']} (last flush {data['queue']['last_flush_ms_ago']}ms ago)")
    lines.append(f"Spill: {data['spill']['files']} files, {data['spill']['bytes']} bytes")
    lines.append(f"Last write: {data['last_write_at']}")
    if data["warnings"]:
        lines.append("Warnings:")
        for w in data["warnings"]:
            lines.append(f"  - {w}")
    else:
        lines.append("Status: healthy")
    return "\n".join(lines)


def main():
    if "--json" in sys.argv:
        print(chatlog_status_impl())
    else:
        status_json = chatlog_status_impl()
        data = json.loads(status_json)
        print(_format_table(data))


if __name__ == "__main__":
    main()
