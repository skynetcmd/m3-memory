#!/usr/bin/env python3
"""chatlog_decay — deterministic ephemeral-content decay for chatlog turns.

Aggressively suppresses chatlog turns whose content has only short-term
value (transient IDs, status snapshots, short user commands) by lowering
their `importance` over time and setting `valid_to` past a hard cutoff.

The `m3:curate-chatlog` subagent calls this tool to do the heavy lifting
without spending tokens evaluating each row.

USAGE
=====

    # Dry run — print what would change, no writes.
    python bin/chatlog_decay.py [--db <path>] [--dry-run]

    # Apply the decay sweep.
    python bin/chatlog_decay.py [--db <path>] --apply

    # Override DB explicitly (also respects $CHATLOG_DB env var).
    python bin/chatlog_decay.py --db /path/to/agent_chatlog.db --apply

DB SELECTION
============

In priority order:
  1. --db <path> CLI argument
  2. $CHATLOG_DB env var
  3. $M3_DATABASE env var (unified mode)
  4. memory/agent_chatlog.db (default)

ALL queries scope to `type='chat_log'`, regardless of layout.

EPHEMERAL CONTENT CATEGORIES
============================

(1) GENERAL EPHEMERAL  — transient IDs, status snapshots, system noise:
    - PIDs / ports / uuids / batch_ids / temp-file paths
    - "completion: X%", "cost: $Y", "X/Y in_progress", live status numbers
    - JSON tool-result-only content like {"ok": true} or {"count": 42}

(2) SHORT-COMMAND  — short user-role turns:
    - "status", "start", "do it", "proceed", "yes", "ok", "go", "(a)"
    - any user-role content ≤4 words (per token-split heuristic)
    Halves the multiplier for stage (1).
    EXCLUSIONS: assistant-role short turns (could carry decisions),
    questions ("?" present), explicit refusals ("no", "stop", "kill").

DECAY SCHEDULE  (importance multiplier vs. age)
================================================

GENERAL EPHEMERAL:
  age <  1 day  -> 1.00x
  age <  3 days -> 0.50x
  age <  7 days -> 0.20x
  age >= 7 days -> 0.05x  AND  valid_to = now (immediate retire)

SHORT-COMMAND (additional halving):
  age <  1 day  -> 0.50x
  age <  3 days -> 0.10x
  age >= 3 days -> 0.02x  AND  valid_to = now - 1 day (immediate retire)

PROMOTION ESCAPE HATCH
======================

Rows with `type != 'chat_log'` (already promoted via `chatlog_promote`)
are excluded from this sweep. Promotion graduates a turn out of the
ephemeral regime entirely.

"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from m3_sdk import getenv_compat


# ── DB resolution ──────────────────────────────────────────────────────────
def resolve_db_path(cli_arg: str | None) -> str:
    if cli_arg:
        return os.path.abspath(cli_arg)
    # Resolution order: legacy CHATLOG_DB alias, then the canonical
    # CHATLOG_DB_PATH (used by chatlog_config / chatlog_ingest /
    # m3_cognitive_loop), then the generic M3_DATABASE. CHATLOG_DB_PATH was
    # previously skipped entirely — setting it and running decay silently
    # pointed at M3_DATABASE instead of the chatlog DB.
    env = (os.environ.get("CHATLOG_DB")
           or getenv_compat("M3_CHATLOG_DB_PATH", "CHATLOG_DB_PATH")
           or os.environ.get("M3_DATABASE"))
    if env:
        return os.path.abspath(env)
    # Default: bench worktree's standard chatlog location
    repo_root = Path(__file__).resolve().parent.parent
    default = repo_root / "memory" / "agent_chatlog.db"
    return str(default)


# ── Ephemeral classification ───────────────────────────────────────────────
_PID_OR_UUID_RE = re.compile(
    r"(?:"
    r"\bPID\s*\d+"                                      # PID 1234
    r"|\bprocess[_\s]+id[\s:=]*\d+"
    r"|\bport\s*\d{4,5}"
    r"|\bbatches/[A-Za-z0-9_-]{12,}"                    # batch ids
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"  # uuid
    r"|/tmp/[A-Za-z0-9._/-]+"                           # /tmp paths (no \b before /)
    r"|AppData[\\\\/]+Local[\\\\/]+Temp"
    r")",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(
    r"\b(?:"
    r"completion[:\s]+\d+\.?\d*\s*%"
    r"|cost[:\s]+\$?\d+\.\d+"
    r"|\d+\s*/\s*\d+\s+(?:in_?progress|done|pending|sessions)"
    r"|workers?\s+alive[:\s]+\d+"
    r"|slice\s+\d+\s+poll\s*#?\d+"
    r")",
    re.IGNORECASE,
)
_SHORT_COMMAND_WORDS = frozenset({
    "status", "start", "stop", "go", "yes", "y", "ok", "proceed", "continue",
    "do it", "kick off", "run it", "fire", "fire it",
})
_REFUSAL_WORDS = frozenset({"no", "stop", "kill", "abort", "wait", "hold"})


def _is_general_ephemeral(content: str) -> bool:
    if not content:
        return False
    snippet = content[:2000]  # cap regex work
    if _PID_OR_UUID_RE.search(snippet) or _STATUS_RE.search(snippet):
        return True
    # JSON-only one-liner like {"ok": true} or just a number
    stripped = snippet.strip()
    if len(stripped) <= 30 and (stripped.startswith("{") or stripped.lstrip("-+").isdigit()):
        return True
    return False


def _is_short_user_command(role: str, content: str) -> bool:
    """A short user command worth halving — but not a refusal or question."""
    if (role or "").lower() != "user":
        return False
    if not content:
        return False
    stripped = content.strip().lower()
    if "?" in stripped:
        return False
    if any(w in stripped.split() for w in _REFUSAL_WORDS):
        return False
    word_count = len(stripped.split())
    if word_count <= 4:
        return True
    # explicit short-command phrases up to 5 chars beyond word_count
    if stripped in _SHORT_COMMAND_WORDS:
        return True
    return False


# ── Decay schedule ─────────────────────────────────────────────────────────
def _age_days(created_at: str | None, now_ts: float) -> float:
    if not created_at:
        return 0.0
    # Handle ISO 8601 with or without trailing Z and microseconds
    s = created_at.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Fall back: SQLite "2026-05-07 10:23:45" form (no T, no tz)
        try:
            dt = datetime.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now_ts - dt.timestamp()) / 86400.0)


def _decay_decision(role: str, content: str, age_days: float, now_ts: float):
    """Return (new_importance_factor, new_valid_to_or_none, category) or (None,None,None) if no change."""
    is_general = _is_general_ephemeral(content)
    is_short_cmd = _is_short_user_command(role, content)
    if not (is_general or is_short_cmd):
        return None, None, None

    if is_short_cmd:
        # Short-command schedule (more aggressive)
        if age_days < 1.0:
            return 0.50, None, "short_cmd_fresh"
        if age_days < 3.0:
            return 0.10, None, "short_cmd_aging"
        # >= 3 days: retire
        retire_at = datetime.fromtimestamp(now_ts - 86400.0, tz=timezone.utc).isoformat()
        return 0.02, retire_at, "short_cmd_retired"

    # General-ephemeral schedule
    if age_days < 1.0:
        return 1.00, None, "ephemeral_fresh"   # no change yet
    if age_days < 3.0:
        return 0.50, None, "ephemeral_aging_1"
    if age_days < 7.0:
        return 0.20, None, "ephemeral_aging_2"
    # >= 7 days: retire
    retire_at = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()
    return 0.05, retire_at, "ephemeral_retired"


# ── Sweep ──────────────────────────────────────────────────────────────────
def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == col for r in rows)


def run_sweep(db_path: str, *, apply: bool, batch_size: int = 1000) -> dict:
    """Walk all chat_log rows, apply decay decisions, return a summary."""
    if not os.path.exists(db_path):
        return {"error": f"DB not found: {db_path}"}

    now_ts = time.time()
    summary: dict[str, Any] = {
        "db_path": db_path,
        "apply": apply,
        "scanned": 0,
        "skip_promoted": 0,
        "unflagged_role": 0,    # rows whose title didn't match `<role>@<host>:` convention
        "by_category": {},
        "applied_writes": 0,
        "errors": [],
    }

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        # Sanity: schema must have the columns we touch.
        # NOTE: chatlog has no top-level `role` column. We derive role from
        # the `title` prefix using the `<role>@<host_agent>: ...` convention.
        # See `_extract_role_from_title` in the SQL CASE expression below.
        for col in ("id", "type", "title", "content", "importance", "created_at"):
            if not _has_column(conn, "memory_items", col):
                return {"error": f"memory_items.{col} missing in {db_path}"}
        has_valid_to = _has_column(conn, "memory_items", "valid_to")

        # MANDATORY: type='chat_log' filter, regardless of layout (per m3:curate-chatlog policy).
        # Role is derived from `title` prefix using the `<role>@<host_agent>: ...`
        # convention enforced by chatlog_core's writer. Empirical match rate vs
        # metadata_json.role on 1000-row sample: 99.8% (2 mismatches).
        # Rows whose title doesn't match the convention land in the
        # "unflagged_role" bucket and are reported separately. They get the
        # general-ephemeral schedule (short-command decay does not fire on them)
        # so misclassification is in the safe direction.
        cur = conn.execute("""
            SELECT id,
                   CASE
                       WHEN title LIKE 'user@%'      THEN 'user'
                       WHEN title LIKE 'assistant@%' THEN 'assistant'
                       WHEN title LIKE 'system@%'    THEN 'system'
                       WHEN title LIKE 'tool@%'      THEN 'tool'
                       ELSE ''
                   END AS role,
                   content,
                   importance,
                   created_at
            FROM memory_items
            WHERE type='chat_log' AND is_deleted=0
        """)

        write_buffer = []
        for row in cur:
            summary["scanned"] += 1
            role = row["role"] or ""
            if role == "":
                summary["unflagged_role"] += 1
            content = row["content"] or ""
            cur_imp = float(row["importance"]) if row["importance"] is not None else 0.3
            age = _age_days(row["created_at"], now_ts)

            factor, retire_at, category = _decay_decision(role, content, age, now_ts)
            if category is None:
                continue
            summary["by_category"][category] = summary["by_category"].get(category, 0) + 1

            new_imp = round(cur_imp * factor, 4)
            if abs(new_imp - cur_imp) < 0.001 and retire_at is None:
                continue   # no-op (e.g., ephemeral_fresh with factor=1.0)

            write_buffer.append((row["id"], new_imp, retire_at))
            if len(write_buffer) >= batch_size:
                summary["applied_writes"] += _flush(conn, write_buffer, apply, has_valid_to)
                write_buffer.clear()

        if write_buffer:
            summary["applied_writes"] += _flush(conn, write_buffer, apply, has_valid_to)

        if apply:
            conn.commit()
    except Exception as exc:
        summary["errors"].append(repr(exc))
    finally:
        conn.close()
    return summary


def _flush(conn: sqlite3.Connection, buf: list[tuple], apply: bool, has_valid_to: bool) -> int:
    if not apply:
        return len(buf)
    written = 0
    for row_id, new_imp, retire_at in buf:
        if retire_at and has_valid_to:
            conn.execute(
                "UPDATE memory_items SET importance=?, valid_to=?, updated_at=? "
                "WHERE id=? AND type='chat_log'",
                (new_imp, retire_at, datetime.now(timezone.utc).isoformat(), row_id),
            )
        else:
            conn.execute(
                "UPDATE memory_items SET importance=?, updated_at=? "
                "WHERE id=? AND type='chat_log'",
                (new_imp, datetime.now(timezone.utc).isoformat(), row_id),
            )
        written += 1
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=None, help="path to chatlog DB (overrides env)")
    parser.add_argument("--apply", action="store_true", help="actually write changes (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true", help="explicit dry-run (default behavior)")
    args = parser.parse_args()

    apply = args.apply and not args.dry_run
    db = resolve_db_path(args.db)
    summary = run_sweep(db, apply=apply)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not summary.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
