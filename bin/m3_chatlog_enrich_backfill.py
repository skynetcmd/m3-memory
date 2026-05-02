#!/usr/bin/env python3
"""Backfill `observation_queue` from existing chatlog rows.

Scans `memory_items WHERE type='chat_log'` in the chatlog DB, groups by
conversation_id, and INSERTs one row per conversation into `observation_queue`
of the main DB. Order is reverse-chronological (newest conversations first),
so when the periodic drain runs it processes the most recent material first.

Idempotent: uses `INSERT OR IGNORE`, so re-running won't duplicate enqueued
work for conversations still in the queue. Conversations already drained out
of the queue WILL be re-enqueued — use `--skip-already-enriched` to also
exclude conversations that already produced observations under the configured
target variant.

Cross-platform: only depends on Python stdlib + sqlite3.
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAIN_DB = REPO_ROOT / "memory" / "agent_memory.db"
DEFAULT_CHATLOG_DB = REPO_ROOT / "memory" / "agent_chatlog.db"


def _ensure_observation_queue(con: sqlite3.Connection) -> None:
    """Verify the queue table exists; we don't create it (migrations own that).
    """
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='observation_queue'"
    ).fetchone()
    if not row:
        raise SystemExit(
            "main DB has no observation_queue table. Run migrations first."
        )


def _conversations_reverse_chrono(
    chatlog_db: Path,
    since: str | None,
) -> list[tuple[str, str, str]]:
    """Return (conversation_id, user_id, last_seen) tuples, newest first.

    Reads `memory_items` rows where `type='chat_log'`, grouped by
    conversation_id, ordered by most-recent turn timestamp descending.
    """
    con = sqlite3.connect(f"file:{chatlog_db}?mode=ro", uri=True, timeout=30)
    try:
        params: list[object] = []
        where = (
            "WHERE type='chat_log' "
            "AND conversation_id IS NOT NULL AND conversation_id != ''"
        )
        if since:
            where += " AND created_at >= ?"
            params.append(since)
        cur = con.execute(
            f"""
            SELECT conversation_id,
                   COALESCE(MAX(user_id), '') AS user_id,
                   MAX(created_at) AS last_seen
            FROM memory_items
            {where}
            GROUP BY conversation_id
            ORDER BY last_seen DESC
            """,
            params,
        )
        return [(r[0], r[1] or "", r[2]) for r in cur.fetchall()]
    finally:
        con.close()


def _already_enriched(
    main_db: Path,
    target_variant: str,
) -> set[str]:
    """Return conversation_ids that already have observations under variant."""
    con = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True, timeout=30)
    try:
        cur = con.execute(
            "SELECT DISTINCT conversation_id FROM memory_items "
            "WHERE variant=? AND conversation_id IS NOT NULL",
            (target_variant,),
        )
        return {r[0] for r in cur.fetchall() if r[0]}
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enqueue chatlog conversations into observation_queue, "
                    "newest first.",
    )
    parser.add_argument("--main-db", default=str(DEFAULT_MAIN_DB),
                        help=f"Main DB path (default: {DEFAULT_MAIN_DB})")
    parser.add_argument("--chatlog-db", default=str(DEFAULT_CHATLOG_DB),
                        help=f"Chatlog DB path (default: {DEFAULT_CHATLOG_DB})")
    parser.add_argument("--since", default=None,
                        help="Only enqueue conversations with rows since this "
                             "ISO timestamp (e.g. 2026-04-01).")
    parser.add_argument("--skip-already-enriched", default=None,
                        metavar="VARIANT",
                        help="Skip conversation_ids already present under this "
                             "variant in memory_items.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap how many conversations to enqueue.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count what would be enqueued, write nothing.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt.")
    args = parser.parse_args()

    main_db = Path(args.main_db)
    chatlog_db = Path(args.chatlog_db)
    if not main_db.exists():
        print(f"error: main DB not found: {main_db}", file=sys.stderr)
        return 1
    if not chatlog_db.exists():
        print(f"error: chatlog DB not found: {chatlog_db}", file=sys.stderr)
        return 1

    convos = _conversations_reverse_chrono(chatlog_db, args.since)
    print(f"found {len(convos)} conversations in chatlog "
          f"(since={args.since or 'beginning'})")

    skip = set()
    if args.skip_already_enriched:
        skip = _already_enriched(main_db, args.skip_already_enriched)
        print(f"  excluding {len(skip)} already enriched under "
              f"variant={args.skip_already_enriched!r}")

    candidates = [(cid, uid, ts) for cid, uid, ts in convos if cid not in skip]
    if args.limit:
        candidates = candidates[: args.limit]

    print(f"  -> {len(candidates)} candidates to enqueue (reverse chronological)")
    if not candidates:
        return 0
    print(f"     newest: {candidates[0][2]}  ({candidates[0][0][:12]}...)")
    print(f"     oldest: {candidates[-1][2]}  ({candidates[-1][0][:12]}...)")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    if not args.yes:
        try:
            resp = input(f"\nINSERT OR IGNORE {len(candidates)} rows into "
                         f"{main_db}::observation_queue ? [y/N]: ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            print("aborted.")
            return 0

    con = sqlite3.connect(str(main_db), timeout=60)
    try:
        _ensure_observation_queue(con)
        cur = con.cursor()
        cur.execute("BEGIN IMMEDIATE")
        rowcount = 0
        for cid, uid, _ts in candidates:
            cur.execute(
                "INSERT OR IGNORE INTO observation_queue (conversation_id, user_id) "
                "VALUES (?, ?)",
                (cid, uid),
            )
            rowcount += cur.rowcount
        cur.execute("COMMIT")
        print(f"\nenqueued {rowcount} new rows "
              f"({len(candidates) - rowcount} were already in the queue).")
    finally:
        con.close()

    print("\nnext step: the AgentOS_ObservationDrain scheduled task will "
          "process them on its next fire (every 15 min).")
    print("or run manually:")
    print("  python bin/m3_enrich.py --drain-queue --drain-batch 200 "
          "--profile enrich_local_qwen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
