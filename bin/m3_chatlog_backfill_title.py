#!/usr/bin/env python3
"""m3_chatlog_backfill_title — Backfill missing/useless titles from content.

Free-win FTS5 lift from the 2026-04-26 chatlog analysis (memory id
37633aff). Title is part of the FTS index, so rows with title='user' or
title=NULL are effectively unsearchable by keyword. This tool replaces
useless titles with the first 100 chars of content.

Idempotent: rows that already have meaningful titles are left alone. The
"useless" set is configurable via --useless-titles.

Quick start:
    python bin/m3_chatlog_backfill_title.py --dry-run
    python bin/m3_chatlog_backfill_title.py
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = Path.home() / ".m3-memory" / "backups"

# Default set of titles we consider "useless" for FTS purposes.
# These are role labels or generic placeholders — they tell you nothing
# about what the row contains.
DEFAULT_USELESS_TITLES = (
    "", "user", "assistant", "system", "message", "chat_log",
    "None", "none", "[AUTO] Generated", "untitled",
)


def _resolve_db(arg_path: Optional[str], env_var: str, default_name: str) -> Optional[Path]:
    if arg_path:
        p = Path(arg_path).expanduser().resolve()
        return p if p.exists() else None
    env_val = os.environ.get(env_var)
    if env_val:
        p = Path(env_val).expanduser().resolve()
        return p if p.exists() else None
    p = REPO_ROOT / "memory" / default_name
    return p if p.exists() else None


def _backup_db(db_path: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M")
    dst = BACKUP_DIR / f"{db_path.stem}.pre-title-backfill.{stamp}.db"
    shutil.copy2(db_path, dst)
    return dst


def _derive_title(content: str, max_chars: int = 100) -> str:
    """Pull a useful title from the content's first line.

    Strategy:
      1. Strip leading whitespace + leading XML-ish tags ("<task-notification>")
         that pad the front of agent-captured turns.
      2. Take the first non-empty line.
      3. Truncate to max_chars; if mid-word, trim back to last space + "…".
      4. Collapse runs of whitespace to a single space.
    """
    if not content:
        return ""
    txt = content.strip()
    # Strip leading XML-ish wrapper tags so "<task-notification>...<summary>X</summary>..."
    # surfaces the inner X rather than the tag name.
    txt = re.sub(r"^<[^>]+>\s*", "", txt)
    # First non-empty line
    for line in txt.split("\n"):
        line = line.strip()
        if line:
            txt = line
            break
    txt = re.sub(r"\s+", " ", txt)
    if len(txt) <= max_chars:
        return txt
    truncated = txt[:max_chars]
    # If we cut mid-word, back up to the last space.
    last_space = truncated.rfind(" ")
    if last_space > max_chars * 0.5:
        truncated = truncated[:last_space]
    return truncated.rstrip() + "…"


def _audit(
    db_path: Path,
    useless_titles: tuple[str, ...],
    min_chars: int,
) -> tuple[int, dict]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" * len(useless_titles))
        sql = f"""
            SELECT type, COUNT(*) AS n
            FROM memory_items
            WHERE COALESCE(is_deleted,0)=0
              AND (title IS NULL OR title IN ({placeholders}))
              AND length(COALESCE(content,'')) >= ?
            GROUP BY type
            ORDER BY n DESC
        """
        rows = conn.execute(sql, list(useless_titles) + [min_chars]).fetchall()
        return sum(r[1] for r in rows), {r[0]: r[1] for r in rows}
    finally:
        conn.close()


def _backfill(
    db_path: Path,
    useless_titles: tuple[str, ...],
    min_chars: int,
    max_title_chars: int,
    limit: Optional[int],
) -> dict:
    counters = {"updated": 0, "skipped_empty_derived": 0, "wall_s": 0.0}
    started = time.monotonic()

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        placeholders = ",".join("?" * len(useless_titles))
        sql = f"""
            SELECT id, content
            FROM memory_items
            WHERE COALESCE(is_deleted,0)=0
              AND (title IS NULL OR title IN ({placeholders}))
              AND length(COALESCE(content,'')) >= ?
            ORDER BY created_at ASC
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, list(useless_titles) + [min_chars]).fetchall()

        cur = conn.cursor()
        # Detect whether the FTS table is a contentless index (newer m3) so
        # we know whether to issue manual UPDATE on it. Standard FTS5
        # external-content tables get refreshed via triggers automatically.
        for mid, content in rows:
            new_title = _derive_title(content or "", max_title_chars)
            if not new_title:
                counters["skipped_empty_derived"] += 1
                continue
            cur.execute(
                "UPDATE memory_items SET title=? WHERE id=?",
                (new_title, mid),
            )
            counters["updated"] += cur.rowcount
        conn.commit()

        # Refresh FTS — try the standard rebuild command. If the table
        # doesn't exist or doesn't support it, ignore the error (some
        # chatlog DBs use a contentless variant that auto-syncs).
        try:
            conn.execute("INSERT INTO memory_items_fts(memory_items_fts) VALUES('rebuild')")
            conn.commit()
            print(f"[title-backfill] {db_path.name}: FTS rebuilt", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[title-backfill] {db_path.name}: FTS rebuild skipped ({type(e).__name__})", flush=True)
    finally:
        conn.close()

    counters["wall_s"] = time.monotonic() - started
    return counters


def _print_dry_run(plan: dict) -> None:
    print()
    print("══════════════════════════════════════════════════════════════")
    print("  m3-chatlog-backfill-title DRY RUN — no writes will happen")
    print("══════════════════════════════════════════════════════════════")
    print()
    print(f"  Useless titles considered: {plan['useless']}")
    print(f"  Min content chars:         {plan['min_chars']}")
    print(f"  Max derived title chars:   {plan['max_title_chars']}")
    print()
    for label, db_info in plan["dbs"].items():
        print(f"  ── {label} ─────────────")
        print(f"     path:     {db_info['path']}")
        print(f"     to update: {db_info['n_total']}")
        for t, n in db_info["by_type"].items():
            print(f"        {t:<22} {n}")
        # Sample 3 derived titles for review
        if db_info.get("samples"):
            print(f"     samples:")
            for old, new in db_info["samples"][:3]:
                print(f"        {old!r:<30} → {new!r}")
        print()
    print("To run for real, drop --dry-run.")
    print("══════════════════════════════════════════════════════════════")


def _sample_derivations(
    db_path: Path,
    useless_titles: tuple[str, ...],
    min_chars: int,
    max_title_chars: int,
    n: int = 3,
) -> list[tuple[str, str]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" * len(useless_titles))
        sql = f"""
            SELECT title, content
            FROM memory_items
            WHERE COALESCE(is_deleted,0)=0
              AND (title IS NULL OR title IN ({placeholders}))
              AND length(COALESCE(content,'')) >= ?
            ORDER BY length(content) DESC LIMIT ?
        """
        rows = conn.execute(sql, list(useless_titles) + [min_chars, n]).fetchall()
        return [(r[0] or "<NULL>", _derive_title(r[1] or "", max_title_chars)) for r in rows]
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill memory_items.title from content where title is missing or generic.",
    )
    ap.add_argument("--core", action="store_true", dest="core_only")
    ap.add_argument("--chatlog", action="store_true", dest="chatlog_only")
    ap.add_argument("--core-db", default=None)
    ap.add_argument("--chatlog-db", default=None)
    ap.add_argument("--useless-titles", default=None,
                    help="Comma-separated list of titles to treat as useless. "
                         "Default: user,assistant,system,message,chat_log,None,'',etc.")
    ap.add_argument("--min-chars", type=int, default=10,
                    help="Skip rows whose content is shorter than this. Default 10.")
    ap.add_argument("--max-title-chars", type=int, default=100,
                    help="Cap derived titles at this many chars. Default 100.")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-backup", action="store_true")
    ap.add_argument("--yes", "-y", action="store_true")
    args = ap.parse_args()
    if args.core_only and args.chatlog_only:
        sys.exit("ERROR: --core and --chatlog are mutually exclusive.")

    if args.useless_titles:
        useless = tuple(t.strip() for t in args.useless_titles.split(",") if t.strip() != "<NONE>")
        # Allow user to keep the empty-string match by writing "<EMPTY>" or "" in the list
        if "" not in useless:
            useless = useless + ("",)
    else:
        useless = DEFAULT_USELESS_TITLES

    db_targets: list[tuple[str, Path]] = []
    if not args.chatlog_only:
        core_db = _resolve_db(args.core_db, "M3_DATABASE", "agent_memory.db")
        if core_db:
            db_targets.append(("core", core_db))
    if not args.core_only:
        chatlog_db = _resolve_db(args.chatlog_db, "M3_CHATLOG_DATABASE", "agent_chatlog.db")
        if chatlog_db:
            db_targets.append(("chatlog", chatlog_db))
    if not db_targets:
        sys.exit("ERROR: no DBs found.")

    plan = {"useless": list(useless), "min_chars": args.min_chars,
            "max_title_chars": args.max_title_chars, "dbs": {}}
    for label, db_path in db_targets:
        n_total, by_type = _audit(db_path, useless, args.min_chars)
        samples = _sample_derivations(db_path, useless, args.min_chars, args.max_title_chars)
        plan["dbs"][label] = {
            "path": str(db_path), "n_total": n_total,
            "by_type": by_type, "samples": samples,
        }

    if args.dry_run:
        _print_dry_run(plan)
        return 0

    _print_dry_run(plan)
    print()
    if not args.yes:
        try:
            ans = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            print("aborted (no changes made)")
            return 0

    if not args.skip_backup:
        for label, db_path in db_targets:
            backup = _backup_db(db_path)
            print(f"[title-backfill] backup: {db_path.name} → {backup}", flush=True)

    grand = {"updated": 0, "skipped_empty_derived": 0}
    for label, db_path in db_targets:
        n = plan["dbs"][label]["n_total"]
        if n == 0:
            print(f"[title-backfill] {db_path.name}: no rows need backfill — skipping", flush=True)
            continue
        print(f"[title-backfill] {db_path.name}: backfilling {n} rows...", flush=True)
        counters = _backfill(db_path, useless, args.min_chars, args.max_title_chars, args.limit)
        for k in grand:
            grand[k] += counters[k]
        print(f"[title-backfill] {db_path.name} done: "
              f"{counters['updated']} updated, "
              f"{counters['skipped_empty_derived']} skipped (empty derived), "
              f"{counters['wall_s']:.1f}s wall", flush=True)

    print()
    print("══════════════════════════════════════════════════════════════")
    print("  m3-chatlog-backfill-title COMPLETE")
    print("══════════════════════════════════════════════════════════════")
    print(f"  total updated: {grand['updated']}")
    print(f"  skipped (empty derived): {grand['skipped_empty_derived']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
