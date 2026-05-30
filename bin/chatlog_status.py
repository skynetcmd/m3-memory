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
    """Count chat_log rows plus unembedded rows on the chatlog DB.

    When the chatlog DB and the main DB are the same file, both counts reflect
    that single file. When they differ, the chatlog file is queried on its own
    and the main file is also polled for any chat_log rows that were promoted
    into it.
    """
    from m3_sdk import resolve_db_path

    counts = {
        "main_chat_log_rows": 0,
        "chatlog_rows": 0,
        "chatlog_without_embed": 0,
    }

    chatlog_db = os.path.abspath(config.db_path)
    main_db = os.path.abspath(resolve_db_path(None))
    unified = chatlog_db == main_db

    try:
        if os.path.exists(main_db):
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

        if not unified and os.path.exists(chatlog_db):
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
        elif unified:
            # Single file — report the same number in both slots for compat.
            counts["chatlog_rows"] = counts["main_chat_log_rows"]

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
    from m3_sdk import resolve_db_path

    config = chatlog_config.resolve_config()
    state = _load_state_file()
    row_counts = _get_row_counts(config)

    main_db = os.path.abspath(resolve_db_path(None))
    chatlog_db = os.path.abspath(config.db_path)

    result = {
        "unified": chatlog_db == main_db,
        "db_paths": {
            "main": main_db,
            "chatlog": chatlog_db,
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


def _get_file_size_mb(path: str) -> float:
    try:
        if os.path.exists(path):
            return os.path.getsize(path) / (1024 * 1024)
    except Exception:
        pass
    return 0.0


def _get_wal_size_mb(path: str) -> float:
    return _get_file_size_mb(path + "-wal")


def _get_chroma_queue_count(main_db: str) -> int:
    if os.path.exists(main_db):
        try:
            conn = sqlite3.connect(main_db, timeout=2)
            try:
                row = conn.execute("SELECT COUNT(*) FROM chroma_sync_queue").fetchone()
                return row[0] if row else 0
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        except Exception:
            pass
    return 0


def _get_last_turns(main_db: str) -> list[dict[str, str]]:
    turns = []
    if os.path.exists(main_db):
        try:
            conn = sqlite3.connect(main_db, timeout=2)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT created_at, content FROM memory_items "
                    "WHERE type='chat_log' AND is_deleted=0 "
                    "ORDER BY created_at DESC LIMIT 5"
                ).fetchall()
                for r in rows:
                    content = r["content"] or ""
                    snippet = ""
                    try:
                        # Try parsing as JSON to extract a clean summary
                        turn_data = json.loads(content)
                        if isinstance(turn_data, dict):
                            user_req = turn_data.get("request", "") or turn_data.get("user", "")
                            agent_res = turn_data.get("response", "") or turn_data.get("agent", "")
                            if user_req:
                                snippet = f"user: {user_req}"
                            elif agent_res:
                                snippet = f"agent: {agent_res}"
                    except Exception:
                        pass
                    if not snippet:
                        snippet = content.replace("\n", " ")
                    
                    # Clean and truncate
                    snippet = snippet.strip()
                    if len(snippet) > 62:
                        snippet = snippet[:59] + "..."
                    
                    ts = r["created_at"]
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        ts_str = dt.strftime("%H:%M:%S")
                    except Exception:
                        ts_str = ts[:10]
                        
                    turns.append({"time": ts_str, "text": snippet})
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        except Exception:
            pass
    return list(reversed(turns))


def run_live_tui():
    """Runs a zero-dependency ANSI live terminal dashboard."""
    import time
    from m3_sdk import resolve_db_path

    # Hide cursor
    print("\033[?25l", end="")

    while True:
        # Move cursor to home and clear screen
        print("\033[H\033[J", end="")

        config = chatlog_config.resolve_config()
        state = _load_state_file()
        row_counts = _get_row_counts(config)
        main_db = os.path.abspath(resolve_db_path(None))
        chatlog_db = os.path.abspath(config.db_path)
        unified = chatlog_db == main_db

        main_sz = _get_file_size_mb(main_db)
        main_wal_sz = _get_wal_size_mb(main_db)
        chat_sz = _get_file_size_mb(chatlog_db)
        chat_wal_sz = _get_wal_size_mb(chatlog_db)

        chroma_q = _get_chroma_queue_count(main_db)
        last_turns = _get_last_turns(main_db)

        # Build dashboard lines
        lines = []
        lines.append("┌────────────────────────────────────────────────────────────────────────────┐")
        lines.append("│  ⚡ M3 MEMORY Diagnostics & Subsystem Status (Live Monitor)                 │")
        lines.append("├────────────────────────────────────────────────────────────────────────────┤")
        lines.append("│ DATABASE FILES & JOURNAL SIZE (WAL)                                        │")
        
        main_status = "active" if main_wal_sz > 0 else "idle"
        lines.append(f"│  Main DB:   {main_db[:60]:<60} │")
        lines.append(f"│             Size: {main_sz:6.1f} MB  |  WAL size: {main_wal_sz:5.1f} MB  |  Status: {main_status:<6} │")
        
        if not unified:
            chat_status = "active" if chat_wal_sz > 0 else "idle"
            lines.append(f"│  Chatlog:   {chatlog_db[:60]:<60} │")
            lines.append(f"│             Size: {chat_sz:6.1f} MB  |  WAL size: {chat_wal_sz:5.1f} MB  |  Status: {chat_status:<6} │")
        else:
            lines.append("│  Chatlog:   (unified with main database file)                              │")
            
        lines.append("├────────────────────────────────────────────────────────────────────────────┤")
        lines.append("│ QUEUE DEPTHS & SPILL MONITOR                                               │")
        
        depth = state.get("queue", {}).get("depth", 0)
        max_depth = state.get("queue", {}).get("max", config.queue_max_depth)
        spill_files = len([f for f in os.listdir(chatlog_config.SPILL_DIR) if f.endswith(".jsonl")]) if os.path.exists(chatlog_config.SPILL_DIR) else 0
        spill_bytes = state.get("spill", {}).get("bytes", 0)
        
        lines.append(f"│  Chatlog Queue Depth: {depth:<4} / {max_depth:<4}                                           │")
        lines.append(f"│  Compaction Spill:    {spill_files:<4} files ({spill_bytes / 1024:.1f} KB)                             │")
        lines.append(f"│  Chroma Sync Queue:   {chroma_q:<4} pending upserts                                    │")
        lines.append("├────────────────────────────────────────────────────────────────────────────┤")
        lines.append("│ SYSTEM INTEGRATION HOOKS                                                   │")
        
        for name, spec in sorted(config.host_agents.items()):
            status_str = "ENABLED " if spec.enabled else "DISABLED"
            last_ms = state.get("hooks", {}).get(name, {}).get("last_write_ms_ago")
            if last_ms is None:
                activity_str = "never active"
            elif last_ms < 60000:
                activity_str = "active just now"
            elif last_ms < 3600000:
                activity_str = f"active {int(last_ms / 60000)} mins ago"
            else:
                activity_str = f"active {int(last_ms / 3600000)} hours ago"
            lines.append(f"│  {name:<12} : [{status_str}]  {activity_str:<45} │")
            
        lines.append("├────────────────────────────────────────────────────────────────────────────┤")
        lines.append("│ LAST 5 CHATLOG CAPTURE EVENTS                                              │")
        
        if last_turns:
            for turn in last_turns:
                lines.append(f"│  [{turn['time']}] {turn['text']:<63} │")
        else:
            lines.append("│  (No chatlog capture turns found yet)                                      │")
            
        lines.append("└────────────────────────────────────────────────────────────────────────────┘")
        lines.append("  [Press Ctrl+C to exit]")

        print("\n".join(lines))
        time.sleep(1.0)


def _format_table(data: dict[str, Any]) -> str:
    lines = []
    lines.append("=== Chat Log Status ===")
    unified = data.get("unified", False)
    lines.append(f"Main DB:    {data['db_paths']['main']}")
    lines.append(f"Chatlog DB: {data['db_paths']['chatlog']}" + (" (unified with main)" if unified else ""))
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
    if "--live" in sys.argv:
        try:
            run_live_tui()
        except KeyboardInterrupt:
            # Restore cursor and exit
            print("\033[?25h\nLive status monitor closed.")
            sys.exit(0)
    elif "--json" in sys.argv:
        print(chatlog_status_impl())
    else:
        status_json = chatlog_status_impl()
        data = json.loads(status_json)
        print(_format_table(data))


if __name__ == "__main__":
    main()
