#!/usr/bin/env python
"""SessionStart hook: verify m3 chatlog capture is actually LANDING writes.

Prints a plain one-line GREEN/RED status as a hook systemMessage. Deliberately
checks whether rows are being WRITTEN to the DB in the recent past, NOT whether
config.host_agents[*].enabled is set — that flag reflects only whether a per-turn
shell hook was wired into settings.json at init time, and reads `false` even when
the Stop-hook / MCP write path is capturing fine (confirmed 2026-06-13). A future
session running the CLAUDE.md mandated session-start check must trust DATA, not the
flag, or it gets a permanent false alarm (or false comfort).

Outputs hook JSON on stdout: {"systemMessage": "...", "suppressOutput": true}.
Never throws — a monitoring check must not break session start.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

WINDOW_MIN = 15


def _resolve_db() -> str:
    """Resolve the m3 agent-memory DB path portably (no hardcoded user path).

    Order: m3_sdk.resolve_db_path (canonical, honors M3_DATABASE / M3_ENGINE_ROOT
    / M3_MEMORY_ROOT exactly as the server does) -> M3_DB_PATH env (legacy) ->
    canonical engine root (<M3_ENGINE_ROOT|~/.m3/engine>/agent_memory.db) ->
    repo-root/engine/agent_memory.db (last-resort dev-clone guess).

    The canonical resolver is tried first AND its engine-root default is used
    for the fallback, so this hook checks the SAME DB the running MCP server
    writes to. The old fallback resolved the DB from M3_HOME/engine, which
    diverges from the server's M3_ENGINE_ROOT — the split-brain that produced a
    false "chatlog NOT writing" alarm against a stale pre-Homecoming copy.
    """
    # bin/hooks/chatlog/this_file.py -> repo root is parents[3]. M3_HOME (if set)
    # only helps LOCATE the bin/ dir to import m3_sdk; it does NOT decide the DB.
    repo = Path(os.environ.get("M3_HOME") or Path(__file__).resolve().parents[3])
    try:
        sys.path.insert(0, str(repo / "bin"))
        from m3_sdk import get_m3_engine_root, resolve_db_path  # type: ignore
        p = resolve_db_path(None)
        if p:
            return os.path.abspath(p)
    except Exception:  # noqa: BLE001 — fall back to path heuristics
        get_m3_engine_root = None  # type: ignore
    env = os.environ.get("M3_DB_PATH")
    if env:
        return os.path.abspath(env)
    # Prefer the canonical engine root (matches the server) over the repo-relative
    # guess, which is a frozen pre-Homecoming copy on migrated installs.
    if get_m3_engine_root is not None:  # type: ignore
        try:
            return os.path.abspath(os.path.join(get_m3_engine_root(), "agent_memory.db"))
        except Exception:  # noqa: BLE001
            pass
    engine_root = os.environ.get("M3_ENGINE_ROOT")
    if engine_root:
        return os.path.abspath(os.path.join(os.path.expanduser(engine_root), "agent_memory.db"))
    return str(repo / "engine" / "agent_memory.db")


def main() -> None:
    db = _resolve_db()
    try:
        conn = sqlite3.connect(db, timeout=5)
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM memory_items "
                "WHERE type = 'chat_log' "
                f"AND created_at > datetime('now', '-{WINDOW_MIN} minutes')"
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — never break session start
        msg = (
            f"\U0001f6a8 m3 chatlog check FAILED to query DB: {exc} "
            "— capture status UNKNOWN. Verify before trusting memory."
        )
        print(json.dumps({"systemMessage": msg}))
        return

    if count > 0:
        msg = f"✅ m3 chatlog capture: WORKING ({count} rows/{WINDOW_MIN}min)"
    else:
        msg = (
            f"\U0001f6a8 WARNING: m3 chatlog capture NOT writing "
            f"(0 rows in last {WINDOW_MIN}min). Design decisions are NOT being "
            "preserved. Restart the m3 MCP server before continuing."
        )
    print(json.dumps({"systemMessage": msg, "suppressOutput": True}))


if __name__ == "__main__":
    main()
    sys.exit(0)
