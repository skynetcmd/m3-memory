#!/usr/bin/env python3
"""
backfill_content_hash.py — populate memory_embeddings.content_hash on legacy rows.

Why: the embed-cache lookup in memory_core._embed / _embed_many uses
`WHERE embed_model = ? AND content_hash IN (?, ...)`. Rows with NULL
content_hash are invisible to that cache — duplicate-text re-embeds
incur a fresh ~2080ms llama_encode roundtrip instead of a sub-ms hit.

Older write paths (and chatlog_embed_sweeper before commit d4b7b2c)
inserted memory_embeddings rows without populating content_hash. This
tool computes the hash from each row's source content (matching what
memory_core._content_hash would produce at write time) and UPDATEs the
embedding row in place.

Idempotent — re-running picks up only rows still NULL. Safe to run
alongside live writers (single-row UPDATEs commit per batch in WAL
mode; no schema change).

Scope decision:
  - For chat_log / message rows, the embed text is the raw content
    (chatlog sweeper uses identity transform). Hash matches.
  - For other types, memory_write_impl applies _augment_embed_text_with_anchors
    to the content + metadata before hashing. To match exactly, we'd need
    to re-augment during backfill. We skip non-chat_log/message types by
    default (--types defaults to chat_log,message) so the backfilled
    hashes match what _embed_many computes for new embeds; pass --types
    explicitly with --augment-anchors to backfill other types with the
    augmentation transform applied.

Usage:

    # Default: only chat_log + message (raw-content hashes are safe)
    python bin/backfill_content_hash.py --db memory/agent_chatlog.db

    # Smoke test 100 rows
    python bin/backfill_content_hash.py --db memory/agent_chatlog.db --limit 100

    # Dry run — show counts, write nothing
    python bin/backfill_content_hash.py --db memory/agent_chatlog.db --dry-run

    # Custom type + augmented hashing (matches inline _embed behavior for non-chatlog)
    python bin/backfill_content_hash.py --db memory/agent_memory.db \\
        --type summary --type note --augment-anchors

    # Sharded across multiple invocations
    python bin/backfill_content_hash.py --db DB --id-prefix 0
    python bin/backfill_content_hash.py --db DB --id-prefix 1
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
DEFAULT_DB = REPO_ROOT / "memory" / "agent_memory.db"
DEFAULT_TYPES = ("chat_log", "message")
DEFAULT_BATCH_SIZE = 1000


def _verify_schema(db_path: Path) -> None:
    """Confirm memory_items + memory_embeddings exist with required columns."""
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        for tbl in ("memory_items", "memory_embeddings"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (tbl,),
            ).fetchone()
            if not row:
                raise RuntimeError(
                    f"Table {tbl!r} not found in {db_path}. "
                    f"Run migrate_memory.py up first."
                )
        me_cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_embeddings)")}
        if "content_hash" not in me_cols:
            raise RuntimeError(
                f"memory_embeddings.content_hash missing from {db_path}. "
                f"Schema is too old; run migrate_memory.py up."
            )
        mi_cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_items)")}
        for col in ("id", "content", "type"):
            if col not in mi_cols:
                raise RuntimeError(
                    f"memory_items.{col} missing from {db_path}. "
                    f"Schema is too old; run migrate_memory.py up."
                )
    finally:
        conn.close()


def _build_select(args: argparse.Namespace, after_id: str | None) -> tuple[str, list]:
    """Build the candidate-rows SELECT.

    Joins memory_embeddings to memory_items so we can filter on type /
    variant / user_id while reading the source content for hashing.
    Returns rows of (embedding_id, memory_id, content, metadata_json).
    """
    where = [
        "me.content_hash IS NULL",
        "COALESCE(mi.is_deleted, 0) = 0",
        "LENGTH(TRIM(COALESCE(mi.content, ''))) > 0",
    ]
    params: list = []

    if after_id is not None:
        where.append("me.id > ?")
        params.append(after_id)
    if args.type:
        placeholders = ",".join("?" * len(args.type))
        where.append(f"mi.type IN ({placeholders})")
        params.extend(args.type)
    if args.variant:
        placeholders = ",".join("?" * len(args.variant))
        where.append(f"mi.variant IN ({placeholders})")
        params.extend(args.variant)
    if args.user_id:
        where.append("COALESCE(mi.user_id, '') = ?")
        params.append(args.user_id)
    if args.id_prefix:
        where.append("me.id LIKE ?")
        params.append(f"{args.id_prefix.lower()}%")

    sql = f"""
        SELECT me.id, me.memory_id, mi.content, mi.metadata_json
        FROM memory_embeddings me
        JOIN memory_items mi ON mi.id = me.memory_id
        WHERE {' AND '.join(where)}
        ORDER BY me.id
        LIMIT ?
    """
    return sql, params


def _count_pending(db_path: Path, args: argparse.Namespace) -> int:
    sql, params = _build_select(args, after_id=None)
    count_sql = sql.replace(
        "SELECT me.id, me.memory_id, mi.content, mi.metadata_json",
        "SELECT COUNT(*)",
    ).split("ORDER BY")[0]
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        return conn.execute(count_sql, params).fetchone()[0]
    finally:
        conn.close()


def _run_backfill(args: argparse.Namespace) -> dict:
    """Run the backfill loop. Returns counters dict."""
    # Late import: memory_core's _content_hash + _augment_embed_text_with_anchors
    # need M3_DATABASE set BEFORE import.
    os.environ["M3_DATABASE"] = str(args.db)
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    import memory_core as mc  # noqa: E402

    counters = {
        "scanned": 0,
        "updated": 0,
        "skipped_empty": 0,
        "errors": 0,
    }

    fetch_size = args.batch_size
    after_id: str | None = None
    started = time.monotonic()

    # Open with separate read/write connections via the same DB path.
    # WAL mode allows concurrent reads while we update.
    write_conn = sqlite3.connect(str(args.db), timeout=30.0)
    write_conn.execute("PRAGMA journal_mode=WAL")
    write_conn.execute("PRAGMA busy_timeout=30000")
    read_conn = sqlite3.connect(str(args.db), timeout=30.0)

    try:
        while True:
            if args.limit and counters["updated"] >= args.limit:
                break

            sql, params = _build_select(args, after_id=after_id)
            params_with_limit = params + [fetch_size]
            rows = read_conn.execute(sql, params_with_limit).fetchall()
            counters["scanned"] += len(rows)

            if not rows:
                break

            after_id = rows[-1][0]  # advance cursor

            updates: list[tuple[str, str]] = []
            for embedding_id, memory_id, content, metadata_json in rows:
                base_text = (content or "").strip()
                if not base_text:
                    counters["skipped_empty"] += 1
                    continue
                if args.augment_anchors:
                    embed_text = mc._augment_embed_text_with_anchors(
                        base_text, metadata_json
                    )
                else:
                    embed_text = base_text
                hash_value = mc._content_hash(embed_text)
                updates.append((hash_value, embedding_id))

            if not updates:
                continue

            if args.dry_run:
                counters["updated"] += len(updates)
            else:
                try:
                    write_conn.executemany(
                        "UPDATE memory_embeddings SET content_hash = ? WHERE id = ?",
                        updates,
                    )
                    write_conn.commit()
                    counters["updated"] += len(updates)
                except sqlite3.Error as e:
                    counters["errors"] += 1
                    print(f"  WRITE_FAIL: {type(e).__name__}: {e}", file=sys.stderr)
                    # Don't abort; cursor advanced, next batch tries different rows
                    continue

            elapsed = time.monotonic() - started
            rate = counters["updated"] / max(elapsed, 1e-3)
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            print(
                f"[{ts}] scanned={counters['scanned']} "
                f"updated={counters['updated']} "
                f"skipped_empty={counters['skipped_empty']} "
                f"errors={counters['errors']} "
                f"rate={rate:.0f}/s"
            )

    finally:
        read_conn.close()
        write_conn.close()

    return counters


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    sel = ap.add_argument_group("selection")
    sel.add_argument("--db", type=Path,
                     default=Path(os.environ.get("M3_DATABASE", str(DEFAULT_DB))),
                     help=f"Target DB. Default: $M3_DATABASE or {DEFAULT_DB}")
    sel.add_argument("--type", action="append", default=None,
                     help=f"Memory type to include. Repeatable. "
                          f"Defaults to {' + '.join(DEFAULT_TYPES)} (raw-content "
                          f"hashing is safe for these). For other types, pass "
                          f"--type explicitly with --augment-anchors so the "
                          f"hash matches what _embed_many would compute.")
    sel.add_argument("--variant", action="append", default=[],
                     help="Filter to memory_items.variant. Repeatable for OR.")
    sel.add_argument("--user-id", type=str, default=None,
                     help="Filter to one memory_items.user_id.")
    sel.add_argument("--id-prefix", type=str, default=None,
                     help="Backfill only embedding rows whose id starts with "
                          "this hex prefix. Use to shard across instances.")
    sel.add_argument("--limit", type=int, default=None,
                     help="Stop after AT LEAST N successful updates. The check "
                          "fires at batch boundaries; actual stop can overshoot "
                          "by up to one batch (--batch-size).")

    perf = ap.add_argument_group("performance")
    perf.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                      help=f"Rows per UPDATE batch. Default: {DEFAULT_BATCH_SIZE}.")

    beh = ap.add_argument_group("behavior")
    beh.add_argument("--augment-anchors", action="store_true",
                     help="Apply memory_core._augment_embed_text_with_anchors "
                          "to content before hashing. Required for non-chatlog "
                          "types where memory_write_impl applied this transform "
                          "at write time. Default OFF (chatlog uses raw content).")
    beh.add_argument("--dry-run", action="store_true",
                     help="Count rows that would be updated; write nothing.")

    args = ap.parse_args(argv)
    if args.type is None:
        args.type = list(DEFAULT_TYPES)
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        _verify_schema(args.db)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    pending = _count_pending(args.db, args)
    print(f"DB:        {args.db}")
    print(f"Pending:   {pending}")
    print(f"Filters:")
    print(f"  type:        {args.type}")
    if args.variant:    print(f"  variant:     {args.variant}")
    if args.user_id:    print(f"  user_id:     {args.user_id}")
    if args.id_prefix:  print(f"  id_prefix:   {args.id_prefix!r}")
    if args.limit:      print(f"  limit:       {args.limit}")
    print(f"Behavior:  augment_anchors={args.augment_anchors} batch_size={args.batch_size}")

    if args.dry_run:
        print()
        print("(dry-run: no UPDATEs written)")
        return 0

    if pending == 0:
        print()
        print("No NULL-content_hash rows match; nothing to do.")
        return 0

    counters = _run_backfill(args)
    print()
    print("=" * 64)
    print("  backfill_content_hash COMPLETE")
    print("=" * 64)
    print(f"  scanned:        {counters['scanned']}")
    print(f"  updated:        {counters['updated']}")
    print(f"  skipped_empty:  {counters['skipped_empty']}")
    print(f"  errors:         {counters['errors']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
