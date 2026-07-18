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
from pathlib import Path
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


def _get_row_counts(config: chatlog_config.ChatlogConfig) -> dict[str, Any]:
    """Count chat_log rows plus unembedded rows on the chatlog DB.

    Values are row counts (int), except ``main_chat_log_rows`` may be a display
    string ("n/a (primary store is PostgreSQL)") when the primary store isn't
    SQLite — hence ``Any`` rather than ``int`` (matching _load_state_file's dict).

    When the chatlog DB and the main DB are the same file, both counts reflect
    that single file. When they differ, the chatlog file is queried on its own
    and the main file is also polled for any chat_log rows that were promoted
    into it.
    """
    from m3_sdk import resolve_db_path

    counts: dict[str, Any] = {
        "main_chat_log_rows": 0,
        "chatlog_rows": 0,
        "chatlog_without_embed": 0,
        "files_leaves": 0,
        "files_unembedded": 0,
    }

    chatlog_db = os.path.abspath(config.db_path)
    main_db = os.path.abspath(resolve_db_path(None))
    unified = chatlog_db == main_db

    # The main-store chat_log count comes from the primary DB. On a PostgreSQL-
    # primary deployment `main_db` is a stale/absent SQLite path, so reading it
    # would report a misleading count. Skip the SQLite read on PG and mark it —
    # the chatlog DB portion below is unaffected (chatlog is its own subsystem).
    from memory.backends import resolve_backend_name
    _primary_is_sqlite = resolve_backend_name() == "sqlite"
    try:
        if not _primary_is_sqlite:
            counts["main_chat_log_rows"] = "n/a (primary store is PostgreSQL)"
        elif os.path.exists(main_db):
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

        # Query Files DB counts
        try:
            from memory.config import FILES_DB_PATH as CONFIG_FILES_DB_PATH
            files_db = os.path.abspath(CONFIG_FILES_DB_PATH)
        except Exception:
            from m3_sdk import get_m3_root
            files_db = os.path.abspath(os.environ.get("M3_FILES_DB_PATH") or os.path.join(get_m3_root(), "memory", "files_database.db"))

        if os.path.exists(files_db):
            try:
                conn = sqlite3.connect(files_db, timeout=5)
                try:
                    row = conn.execute("SELECT COUNT(*) FROM leaves").fetchone()
                    counts["files_leaves"] = row[0] if row else 0

                    row = conn.execute("SELECT COUNT(*) FROM leaves WHERE embedded = 0").fetchone()
                    counts["files_unembedded"] = row[0] if row else 0
                finally:
                    conn.close()
            except sqlite3.Error:
                pass

    except Exception as e:
        logger.warning(f"Error fetching row counts: {e}")

    return counts


def _recent_write_count(config: chatlog_config.ChatlogConfig,
                        window_min: int = 15) -> int:
    """Count chat_log rows WRITTEN in the last `window_min` minutes.

    This is the TRUE capture-health signal: it reflects whether writes are
    actually landing in the DB right now. Use it instead of
    config.host_agents[*].enabled — that flag only records whether a per-turn
    shell hook was wired into settings.json at init time, and reads False even
    when the Stop-hook / MCP write path is capturing perfectly (confirmed
    2026-06-13: status showed every hook enabled=False while 94 rows/15min were
    landing). Reporting the wiring flag as "capture status" produced a permanent
    false alarm for the CLAUDE.md session-start check. Returns -1 on query error
    (distinct from a real 0 = nothing written).

    On a NON-UNIFIED install, chat_log rows are written to the chatlog DB
    (config.db_path), NOT the main DB. Query config.db_path, which equals the
    main DB when unified — so this is correct in both topologies. Querying
    resolve_db_path(None) here was a bug: it counts the main DB, which holds
    zero chat_log rows on a split install, giving a permanent false
    capture-down alarm (confirmed 2026-07-04: 161 rows/15min in the chatlog DB
    while the main DB showed 0). If, on a split install, no rows are found in
    the chatlog DB, fall back to the main DB to cover installs that promote
    turns into main instead.

    Read-only by construction (§6): opens each DB via a ``mode=ro`` URI so the
    probe can never mutate the store and never flips journal_mode on a DB it is
    only counting (which is why apply_pragmas — a WAL-setting write pragma — is
    deliberately NOT used here). The COUNT is index-served: EXPLAIN QUERY PLAN
    confirms ``SEARCH ... USING INDEX idx_mi_created (created_at>?)`` on both the
    chatlog and main DBs, so it is a bounded index range-scan, not a table scan,
    even on a multi-hundred-MB chatlog DB (§8, verified 2026-07-04).
    """
    from m3_sdk import resolve_db_path

    chatlog_db = os.path.abspath(config.db_path)
    main_db = os.path.abspath(resolve_db_path(None))
    # Chatlog DB first (where the writer lands turns on a split install); the
    # main DB is a fallback for promote-into-main topologies. On a unified
    # install both paths are identical and the second probe is skipped.
    candidates = [chatlog_db] if chatlog_db == main_db else [chatlog_db, main_db]

    # `queried_ok` = a candidate DB that we not only opened but successfully ran
    # the COUNT against. It must NOT be set on open alone: a DB can open yet the
    # query error (e.g. `no such table: memory_items` on a partially-initialised
    # or wrong-schema file). If such an error counted as "saw a readable DB", the
    # fn would return 0 (a real "capture down" alarm) instead of -1 (unknown) —
    # reintroducing the false-signal class this whole fix removes. So the flag is
    # set only after fetchone() returns without raising.
    queried_ok = False
    for db in candidates:
        if not os.path.exists(db):
            continue
        conn = None
        try:
            # mode=ro: read-only handle — cannot create/alter the DB or its WAL.
            # Path.as_uri() builds an OS-correct file:// URI (Windows drive
            # letters and POSIX paths alike), keeping this cross-platform (§1).
            uri = f"{Path(db).as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            row = conn.execute(
                "SELECT COUNT(*) FROM memory_items WHERE type='chat_log' "
                "AND created_at > datetime('now', ?)",
                (f"-{int(window_min)} minutes",),
            ).fetchone()
            queried_ok = True  # only reached if the query itself did not raise
            n = int(row[0]) if row else 0
            if n > 0:
                return n
        except sqlite3.Error:
            continue
        finally:
            if conn is not None:
                conn.close()

    # Successfully queried a DB but found no recent rows -> a real 0 (warns).
    # Never got a clean query off any candidate (all missing / open-errored /
    # query-errored) -> -1 unknown (distinct warning, no false "down").
    return 0 if queried_ok else -1


# A 0-in-15min window is only a genuine "capture down" signal if the LAST write
# was also long ago. On a just-started session (or an ordinary between-turns
# lull), the Stop/PreCompact hook has simply not fired yet, so the 15-min count
# is legitimately 0 while the store is perfectly healthy — the last write is just
# minutes-to-hours old. Screaming "capture may be down" in that case is a false
# alarm that trips the CLAUDE.md session-start check every single fresh session
# (observed 2026-07-12: alarm fired while this very session's turns were landing).
# Grace: if the last write is within this many minutes, downgrade the scream to a
# benign note. Beyond it (or last-write unknown), the real alarm still fires.
_LULL_GRACE_MIN = 360  # 6h — comfortably longer than any between-turns gap


def _minutes_since(iso_ts: str | None) -> float | None:
    """Minutes between `iso_ts` (an ISO-8601 UTC stamp) and now, or None if the
    stamp is missing/unparseable. Tolerant of a trailing 'Z' and of fractional
    seconds, matching how last_write_at is stored."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def _compute_warnings(
    state: dict[str, Any],
    config: chatlog_config.ChatlogConfig,
    row_counts: dict[str, Any],
    recent_writes: int = -1,
    recent_window_min: int = 15,
) -> list[str]:
    warnings = []

    # PRIMARY capture-health signal: did anything actually get WRITTEN recently?
    # Driven by recent_writes (a real DB count), NOT host_agents[*].enabled — see
    # _recent_write_count for why the wiring flag is not a capture signal. Only
    # warn when total chatlog rows exist (a fresh/empty install legitimately has
    # 0 recent writes and should not scream).
    total_rows = row_counts.get("main_chat_log_rows", 0) or row_counts.get("chatlog_rows", 0)
    if recent_writes == 0 and total_rows > 0:
        # Disambiguate session-start/between-turns lull from a real outage using
        # the age of the LAST write (see _LULL_GRACE_MIN). A recent last-write
        # means the hook just hasn't fired this window yet — benign; anything
        # older (or unknown) keeps the real "capture may be down" alarm.
        mins_since_last = _minutes_since(state.get("last_write_at"))
        if mins_since_last is not None and mins_since_last <= _LULL_GRACE_MIN:
            warnings.append(
                f"no chatlog writes in last {recent_window_min}min "
                f"(last write {int(mins_since_last)}min ago — "
                "capture idle, not down)"
            )
        else:
            warnings.append(
                f"NO chatlog writes in last {recent_window_min}min "
                "(capture may be down — verify before trusting memory)"
            )
    elif recent_writes < 0:
        warnings.append("could not query recent chatlog writes (capture status unknown)")

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
    recent_window_min = 15
    recent_writes = _recent_write_count(config, recent_window_min)

    main_db = os.path.abspath(resolve_db_path(None))
    chatlog_db = os.path.abspath(config.db_path)

    # Resolve Files DB path
    try:
        from memory.config import FILES_DB_PATH as CONFIG_FILES_DB_PATH
        files_db = os.path.abspath(CONFIG_FILES_DB_PATH)
    except Exception:
        from m3_sdk import get_m3_root
        files_db = os.path.abspath(os.environ.get("M3_FILES_DB_PATH") or os.path.join(get_m3_root(), "memory", "files_database.db"))

    result = {
        "unified": chatlog_db == main_db,
        "db_paths": {
            "main": main_db,
            "chatlog": chatlog_db,
            "files": files_db,
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
        # TRUE capture-health signal — reflects actual recent writes to the DB,
        # not whether a per-turn shell hook was wired. Consumers (CLAUDE.md
        # session-start check, m3:status, m3:health) should read THIS, not
        # hooks[*].wired. recent_rows == -1 means the query failed (unknown).
        "capture": {
            "healthy": recent_writes > 0,
            "recent_rows": recent_writes,
            "window_min": recent_window_min,
        },
        # NOT a capture-health signal. Each entry's `wired`/`enabled` only
        # records whether a per-turn shell hook was configured in settings.json
        # at init time; the Stop-hook / MCP write path captures turns even when
        # every entry here is False (that is the norm on this deployment). Read
        # `capture.healthy` for whether turns are landing — NOT these flags.
        # Kept a flat {agent_name: {...}} map (no sentinel keys) so consumers can
        # iterate it as pure agent data; the caveat lives here in code, not in
        # the return shape (§3/§12a: returns stay structured for machine callers).
        "hooks": {
            name: {
                "wired": spec.enabled,
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
        "warnings": _compute_warnings(state, config, row_counts,
                                      recent_writes, recent_window_min),
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


def _get_keypress(timeout: float) -> str | None:
    """Read a keypress within timeout. Supports Windows and Unix."""
    import sys
    import time

    start_time = time.time()

    if sys.platform == "win32":
        import msvcrt
        while time.time() - start_time < timeout:
            if msvcrt.kbhit():
                try:
                    ch = msvcrt.getch()
                    if isinstance(ch, bytes):
                        val = ch.decode("utf-8", errors="ignore").lower()
                        if val:
                            return val
                    else:
                        val = str(ch).lower()
                        if val:
                            return val
                except Exception:
                    pass
            time.sleep(0.05)
        return None
    else:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            time.sleep(timeout)
            return None

        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while time.time() - start_time < timeout:
                rem = timeout - (time.time() - start_time)
                step = min(0.05, max(0.01, rem))
                rlist, _, _ = select.select([sys.stdin], [], [], step)
                if rlist:
                    ch = sys.stdin.read(1)
                    return ch.lower()
        except Exception:
            time.sleep(timeout)
            return None
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass
        return None


def _wait_for_any_key() -> None:
    """Wait for any keypress. Supports Windows and Unix."""
    import sys
    import time
    if sys.platform == "win32":
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()
        while not msvcrt.kbhit():
            time.sleep(0.05)
        msvcrt.getch()
    else:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            time.sleep(2.0)
            return
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            select.select([sys.stdin], [], [])
            sys.stdin.read(1)
        except Exception:
            time.sleep(2.0)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass


def _run_subprocess_interactive(cmd: list[str]) -> None:
    """Pause live TUI, restore cursor, execute subprocess, wait for key, and resume."""
    import os
    import subprocess
    print("\033[?25h", end="")
    print("\033[H\033[J", end="")
    print(f"\n>>> Executing command:\n    {' '.join(cmd)}")
    print("=" * 80)

    # Add bin directory to PYTHONPATH so packages like files_memory are importable when run as modules
    env = os.environ.copy()
    bin_dir = os.path.dirname(os.path.abspath(__file__))
    if "PYTHONPATH" in env:
        env["PYTHONPATH"] = bin_dir + os.pathsep + env["PYTHONPATH"]
    else:
        env["PYTHONPATH"] = bin_dir

    try:
        result = subprocess.run(cmd, env=env, shell=False)
        print("=" * 80)
        print(f"Command finished with exit code {result.returncode}.")
    except Exception as e:
        print("=" * 80)
        print(f"Error executing command: {e}")

    print("\nPress any key to return to the live monitor...")
    _wait_for_any_key()
    print("\033[?25l", end="")


def _make_line(content: str) -> str:
    """Helper to pad/trim inner line content to exactly 76 terminal columns, avoiding jagged right borders."""
    width = 76
    # Emojis like ⚡ take 2 terminal columns but 1 Python char
    if "⚡" in content:
        width -= 1
    # Trim to width if too long
    if len(content) > width:
        content = content[:width - 3] + "..."
    return f"│{content:<{width}}│"


def run_live_tui(interval: float = 5.0):
    """Runs a zero-dependency ANSI live terminal dashboard with interactive controls."""
    from m3_sdk import resolve_db_path

    # Hide cursor
    print("\033[?25l", end="")

    tick = 0
    t1_state = None
    t2_state = None
    current_interval = interval

    while True:
        # Move cursor to home and clear screen
        print("\033[H\033[J", end="")

        config = chatlog_config.resolve_config()
        state = _load_state_file()
        row_counts = _get_row_counts(config)
        main_db = os.path.abspath(resolve_db_path(None))
        chatlog_db = os.path.abspath(config.db_path)
        unified = chatlog_db == main_db

        # Resolve Files DB path
        try:
            from memory.config import FILES_DB_PATH as CONFIG_FILES_DB_PATH
            files_db = os.path.abspath(CONFIG_FILES_DB_PATH)
        except Exception:
            from m3_sdk import get_m3_root
            files_db = os.path.abspath(os.environ.get("M3_FILES_DB_PATH") or os.path.join(get_m3_root(), "memory", "files_database.db"))

        main_sz = _get_file_size_mb(main_db)
        main_wal_sz = _get_wal_size_mb(main_db)
        chat_sz = _get_file_size_mb(chatlog_db)
        chat_wal_sz = _get_wal_size_mb(chatlog_db)
        files_sz = _get_file_size_mb(files_db)
        files_wal_sz = _get_wal_size_mb(files_db)

        last_turns = _get_last_turns(main_db)

        # Refresh embedding cascade stats every tick if interval is slow, or every 10 ticks if fast
        refresh_cascade = False
        if current_interval >= 5.0:
            refresh_cascade = True
        else:
            refresh_cascade = (tick % int(5.0 / max(0.1, current_interval)) == 0) if current_interval > 0 else True

        if refresh_cascade or t1_state is None:
            try:
                from memory import doctor as doc
                t1_state = doc._probe_tier1()
                t2_state = doc._probe_tier2()
            except Exception:
                t1_state = {"status": "error"}
                t2_state = {"status": "error"}
        tick += 1

        # Truncate paths to safe display lengths to avoid clipping
        main_disp = main_db if len(main_db) <= 60 else "..." + main_db[-57:]
        chat_disp = chatlog_db if len(chatlog_db) <= 60 else "..." + chatlog_db[-57:]
        files_disp = files_db if len(files_db) <= 60 else "..." + files_db[-57:]

        # Build dashboard lines
        lines = []
        lines.append("┌────────────────────────────────────────────────────────────────────────────┐")
        lines.append(_make_line("  ⚡ M3 MEMORY Diagnostics & Subsystem Status (Live Monitor)"))
        lines.append("├────────────────────────────────────────────────────────────────────────────┤")
        lines.append(_make_line(" DATABASE FILES & JOURNAL SIZE (WAL)"))

        main_status = "active" if main_wal_sz > 0 else "idle"
        lines.append(_make_line(f"  Main DB:   {main_disp}"))
        lines.append(_make_line(f"             Size: {main_sz:6.1f} MB  |  WAL size: {main_wal_sz:5.1f} MB  |  Status: {main_status}"))

        if not unified:
            chat_status = "active" if chat_wal_sz > 0 else "idle"
            lines.append(_make_line(f"  Chatlog:   {chat_disp}"))
            lines.append(_make_line(f"             Size: {chat_sz:6.1f} MB  |  WAL size: {chat_wal_sz:5.1f} MB  |  Status: {chat_status}"))
        else:
            lines.append(_make_line("  Chatlog:   (unified with main database file)"))

        files_status = "active" if files_wal_sz > 0 else "idle"
        lines.append(_make_line(f"  Files DB:  {files_disp}"))
        lines.append(_make_line(f"             Size: {files_sz:6.1f} MB  |  WAL size: {files_wal_sz:5.1f} MB  |  Status: {files_status}"))

        lines.append("├────────────────────────────────────────────────────────────────────────────┤")
        lines.append(_make_line(" EMBEDDING CASCADE DIAGNOSTICS"))

        t1_status = t1_state.get("status", "unknown").upper() if t1_state else "UNKNOWN"
        t1_path = t1_state.get("gguf_path") or "Not set" if t1_state else "Not set"
        if len(t1_path) > 40:
            t1_path = "..." + t1_path[-37:]
        lines.append(_make_line(f"  GGUF (Tier 1):     [{t1_status:<14}] Path: {t1_path}"))

        t2_status = t2_state.get("status", "unknown").upper() if t2_state else "UNKNOWN"
        t2_url = t2_state.get("url") or "Not set" if t2_state else "Not set"
        t2_lat = t2_state.get("latency_ms") if t2_state else None
        lat_str = f"({t2_lat} ms)" if t2_lat is not None else "(Offline)"
        lines.append(_make_line(f"  Fallback (Tier 2): [{t2_status:<14}] URL: {t2_url:<27} {lat_str}"))

        lines.append("├────────────────────────────────────────────────────────────────────────────┤")
        lines.append(_make_line(" QUEUE DEPTHS & SPILL MONITOR"))

        depth = state.get("queue", {}).get("depth", 0)
        max_depth = state.get("queue", {}).get("max", config.queue_max_depth)
        spill_files = len([f for f in os.listdir(chatlog_config.SPILL_DIR) if f.endswith(".jsonl")]) if os.path.exists(chatlog_config.SPILL_DIR) else 0
        spill_bytes = state.get("spill", {}).get("bytes", 0)

        files_leaves = row_counts.get("files_leaves", 0)
        files_unembedded = row_counts.get("files_unembedded", 0)

        lines.append(_make_line(f"  Chatlog Queue Depth: {depth} / {max_depth}"))
        lines.append(_make_line(f"  Compaction Spill:    {spill_files} files ({spill_bytes / 1024:.1f} KB)"))
        lines.append(_make_line(f"  Files DB Chunks:     {files_leaves} total ({files_unembedded} pending embeddings)"))
        lines.append("├────────────────────────────────────────────────────────────────────────────┤")
        lines.append(_make_line(" SYSTEM INTEGRATION HOOKS"))

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
            lines.append(_make_line(f"  {name:<12} : [{status_str}]  {activity_str}"))

        lines.append("├────────────────────────────────────────────────────────────────────────────┤")
        lines.append(_make_line(" LAST 5 CHATLOG CAPTURE EVENTS"))

        if last_turns:
            for turn in last_turns:
                lines.append(_make_line(f"  [{turn['time']}] {turn['text']}"))
        else:
            lines.append(_make_line("  (No chatlog capture turns found yet)"))

        lines.append("└────────────────────────────────────────────────────────────────────────────┘")
        lines.append(f"  [Ctrl+C / Q] Exit  |  [+] / [-] Change Interval ({current_interval:.1f}s)")
        lines.append("  Interactive Actions:")
        lines.append("  [D] Decay Sweep (Dry-run)  |  [A] Apply Decay Sweep  |  [S] Run Embed Sweeper")
        lines.append("  [T] Backfill Titles        |  [E] Backfill Embeddings |  [F] Ingest / Sync Files")
        lines.append("  [H] Files DB Health / Rebuild")

        print("\n".join(lines))

        # Read keypress
        key = _get_keypress(current_interval)
        if key:
            if key in ("q", "\x03"):
                raise KeyboardInterrupt()
            elif key == "+":
                current_interval = min(60.0, current_interval + 1.0)
            elif key == "-":
                current_interval = max(0.5, current_interval - 1.0)
            elif key == "d":
                cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "chatlog_decay.py"), "--db", chatlog_db]
                _run_subprocess_interactive(cmd)
            elif key == "a":
                cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "chatlog_decay.py"), "--db", chatlog_db, "--apply"]
                _run_subprocess_interactive(cmd)
            elif key == "s":
                cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "chatlog_embed_sweeper.py"), "--database", main_db, "--drain-spill"]
                _run_subprocess_interactive(cmd)
            elif key == "t":
                cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "m3_chatlog_backfill_title.py")]
                _run_subprocess_interactive(cmd)
            elif key == "e":
                cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "m3_chatlog_backfill_embed.py")]
                _run_subprocess_interactive(cmd)
            elif key == "h":
                cmd = [sys.executable, "-m", "files_memory.tools", "health", "--rebuild"]
                _run_subprocess_interactive(cmd)
            elif key == "f":
                print("\033[?25h", end="") # Show cursor
                print("\033[H\033[J", end="") # Clear screen
                print("\n=== Ingest / Sync Files Database ===")
                print("This will walk a directory, chunk its documents, and embed them.")
                try:
                    path_input = input("\nEnter absolute directory path to ingest (or press Enter to cancel):\n> ").strip()
                except (KeyboardInterrupt, EOFError):
                    path_input = ""

                if path_input:
                    resolved_path = os.path.abspath(os.path.expanduser(path_input))
                    if not os.path.isdir(resolved_path):
                        print(f"\n[Error] Directory does not exist: {resolved_path}")
                        print("\nPress any key to return to the live monitor...")
                        _wait_for_any_key()
                    else:
                        print("\nSelect extraction mode:")
                        print("1. Deferred Fact Extraction (Queue mode - default)")
                        print("2. Synchronous Fact Extraction (Inline mode)")
                        print("3. No Fact Extraction (None mode)")
                        try:
                            mode_choice = input("Select option [1-3] (default: 1): ").strip()
                        except (KeyboardInterrupt, EOFError):
                            mode_choice = "1"

                        extract_mode = "queue"
                        if mode_choice == "2":
                            extract_mode = "inline"
                        elif mode_choice == "3":
                            extract_mode = "none"

                        cmd = [
                            sys.executable,
                            "-m",
                            "files_memory.tools",
                            "ingest",
                            resolved_path,
                            "--mode",
                            extract_mode
                        ]
                        _run_subprocess_interactive(cmd)
                print("\033[?25l", end="") # Hide cursor


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
    import argparse
    parser = argparse.ArgumentParser(
        description="chatlog_status.py — summary of the chat log subsystem state."
    )
    parser.add_argument("--json", action="store_true", help="Output JSON format")
    parser.add_argument("--live", action="store_true", help="Run live status monitor")
    parser.add_argument(
        "-i", "--interval",
        type=float,
        default=5.0,
        help="Refresh interval for live monitor in seconds (default: 5.0)"
    )

    args = parser.parse_args()

    if args.live:
        try:
            run_live_tui(interval=args.interval)
        except KeyboardInterrupt:
            # Restore cursor and exit
            print("\033[?25h\nLive status monitor closed.")
            sys.exit(0)
    elif args.json:
        print(chatlog_status_impl())
    else:
        status_json = chatlog_status_impl()
        data = json.loads(status_json)
        print(_format_table(data))


if __name__ == "__main__":
    main()
