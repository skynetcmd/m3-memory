#!/usr/bin/env python3
"""m3_chatlog_backfill_embed — Embed unembedded rows in core memory + chatlog.

Free-win recall fix from the 2026-04-26 chatlog analysis (memory id
37633aff): in older chatlog DBs the majority of `chat_log` rows have NO
embedding, leaving them invisible to vector search. This tool finds rows
in `memory_items` that lack a corresponding `memory_embeddings` row and
batch-embeds them using the local embedding server.

Apply to:
  - agent_memory.db (core memory) — usually mostly-embedded
  - agent_chatlog.db (chatlog) — typically has the most missing embeddings

Idempotent: if every eligible row already has an embedding, exits 0
without spending compute.

Quick start (LM Studio with text-embedding-bge-m3 loaded):
    python bin/m3_chatlog_backfill_embed.py --dry-run
    python bin/m3_chatlog_backfill_embed.py

Defaults:
  - covers both DBs in one pass (--core / --chatlog narrow)
  - skips rows with content shorter than --min-chars (default 10)
  - applies type allowlist matching m3_enrich (configurable via
    --include-types)
  - chunked batches of --batch (default 32) per embed call
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sqlite3
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bin"))

import memory_core as mc  # noqa: E402

BACKUP_DIR = Path.home() / ".m3-memory" / "backups"


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
    dst = BACKUP_DIR / f"{db_path.stem}.pre-embed-backfill.{stamp}.db"
    shutil.copy2(db_path, dst)
    return dst


def _audit_unembedded(
    db_path: Path,
    type_filter: Optional[list[str]],
    min_chars: int,
) -> tuple[int, dict]:
    """Return (total_eligible_rows, by_type_breakdown)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # Use length(content) >= min_chars to skip near-empty rows.
        type_clause = ""
        params: list = [min_chars]
        if type_filter:
            placeholders = ",".join("?" * len(type_filter))
            type_clause = f" AND mi.type IN ({placeholders})"
            params.extend(type_filter)
        sql = f"""
            SELECT mi.type, COUNT(*) AS n
            FROM memory_items mi
            LEFT JOIN memory_embeddings me ON me.memory_id = mi.id
            WHERE COALESCE(mi.is_deleted,0)=0
              AND me.id IS NULL
              AND length(COALESCE(mi.content,'')) >= ?
              {type_clause}
            GROUP BY mi.type
            ORDER BY n DESC
        """
        rows = conn.execute(sql, params).fetchall()
        return sum(r[1] for r in rows), {r[0]: r[1] for r in rows}
    finally:
        conn.close()


async def _embed_one_batch(
    rows: list[tuple[str, str]],
) -> list[tuple[str, list[float], str]]:
    """Embed a batch of (memory_id, content) pairs via memory_core._embed.

    Returns list of (memory_id, embedding, model_id). Drops rows whose
    embed call returned None (logged in counters by caller).
    """
    out: list[tuple[str, list[float], str]] = []
    # _embed is async; one call per row keeps it simple. The local server
    # batches internally; spawning N concurrent tasks at the m3 level just
    # serializes anyway because of the embed semaphore (max 4 concurrent
    # per memory_core).
    sem = asyncio.Semaphore(4)
    async def one(mid: str, content: str) -> None:
        async with sem:
            try:
                vec, model = await mc._embed(content)
                if vec:
                    out.append((mid, vec, model or ""))
            except Exception:  # noqa: BLE001
                pass
    await asyncio.gather(*(one(mid, content) for mid, content in rows))
    return out


def _write_embeddings_batch(
    db_path: Path,
    embeddings: list[tuple[str, list[float], str]],
) -> int:
    """Insert embedding rows directly via sqlite. Bypasses memory_core's
    write path because we don't want to trigger contradiction-check or
    chroma-sync queueing for backfill rows."""
    if not embeddings:
        return 0
    from embedding_utils import pack as _pack
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        cur = conn.cursor()
        # Detect schema variant — newer m3_chatlog DBs may not have content_hash column
        embed_cols = [c[1] for c in cur.execute("PRAGMA table_info(memory_embeddings)").fetchall()]
        has_kind = "vector_kind" in embed_cols
        has_chash = "content_hash" in embed_cols

        cols = ["id", "memory_id", "embedding", "embed_model", "dim", "created_at"]
        if has_kind:
            cols.append("vector_kind")
        if has_chash:
            cols.append("content_hash")
        placeholders = ",".join("?" * len(cols))
        sql = f"INSERT INTO memory_embeddings ({','.join(cols)}) VALUES ({placeholders})"

        n = 0
        for mid, vec, model in embeddings:
            row = [str(uuid.uuid4()), mid, _pack(vec), model, len(vec), now]
            if has_kind:
                row.append("default")
            if has_chash:
                row.append("")  # backfilled, content unchanged from DB
            cur.execute(sql, row)
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


async def _backfill_db(
    db_path: Path,
    type_filter: Optional[list[str]],
    min_chars: int,
    batch_size: int,
    limit: Optional[int],
) -> dict:
    """Drive the backfill loop on one DB. Returns counters dict."""
    counters = {"queried": 0, "embedded": 0, "failed": 0, "wall_s": 0.0}
    started = time.monotonic()

    # Fetch eligible rows in chunks to avoid pulling 12k rows of content
    # into memory at once.
    type_clause = ""
    params: list = [min_chars]
    if type_filter:
        placeholders = ",".join("?" * len(type_filter))
        type_clause = f" AND mi.type IN ({placeholders})"
        params.extend(type_filter)

    sql = f"""
        SELECT mi.id, mi.content
        FROM memory_items mi
        LEFT JOIN memory_embeddings me ON me.memory_id = mi.id
        WHERE COALESCE(mi.is_deleted,0)=0
          AND me.id IS NULL
          AND length(COALESCE(mi.content,'')) >= ?
          {type_clause}
        ORDER BY mi.created_at ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    # Important: open a read-only handle for the SELECT, write-handle for
    # the INSERT. Avoids "database is locked" if the user has another
    # m3 process running.
    read_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cursor = read_conn.execute(sql, params)
        batch: list[tuple[str, str]] = []
        os.environ["M3_DATABASE"] = str(db_path)  # so memory_core._embed picks the right embed server / config
        while True:
            row = cursor.fetchone()
            if row is None:
                if batch:
                    counters["queried"] += len(batch)
                    embeddings = await _embed_one_batch(batch)
                    written = _write_embeddings_batch(db_path, embeddings)
                    counters["embedded"] += written
                    counters["failed"] += len(batch) - written
                break
            batch.append(row)
            if len(batch) >= batch_size:
                counters["queried"] += len(batch)
                embeddings = await _embed_one_batch(batch)
                written = _write_embeddings_batch(db_path, embeddings)
                counters["embedded"] += written
                counters["failed"] += len(batch) - written
                if counters["queried"] % (batch_size * 10) == 0:
                    elapsed = time.monotonic() - started
                    rate = counters["queried"] / max(elapsed, 1e-3)
                    print(f"[embed-backfill] {db_path.name}: {counters['queried']} queried, "
                          f"{counters['embedded']} embedded, {counters['failed']} failed, "
                          f"rate={rate:.1f}/s", flush=True)
                batch = []
    finally:
        read_conn.close()

    counters["wall_s"] = time.monotonic() - started
    return counters


def _print_dry_run(plan: dict) -> None:
    print()
    print("══════════════════════════════════════════════════════════════")
    print("  m3-chatlog-backfill-embed DRY RUN — no writes will happen")
    print("══════════════════════════════════════════════════════════════")
    print()
    print(f"  Type allowlist:  {plan['types']}")
    print(f"  Min content len: {plan['min_chars']} chars")
    print()
    for label, db_info in plan["dbs"].items():
        print(f"  ── {label} ─────────────")
        print(f"     path:        {db_info['path']}")
        print(f"     unembedded:  {db_info['n_total']}")
        for t, n in db_info["by_type"].items():
            print(f"        {t:<22} {n}")
        print()
    print("To run for real, drop --dry-run.")
    print("══════════════════════════════════════════════════════════════")


async def _main_async(args) -> int:
    type_filter = None
    if args.include_types:
        type_filter = [t.strip() for t in args.include_types.split(",") if t.strip()]
    elif args.all_types:
        type_filter = None  # no filter: cover every type
    else:
        # Sensible default: chat-shaped types + the synthesized layers.
        type_filter = ["chat_log", "message", "conversation", "summary",
                       "note", "observation", "fact_enriched", "fact"]

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
        sys.exit("ERROR: no DBs found. Set M3_DATABASE / M3_CHATLOG_DATABASE or pass --core-db/--chatlog-db.")

    plan = {"types": type_filter or "ALL types", "min_chars": args.min_chars, "dbs": {}}
    for label, db_path in db_targets:
        n_total, by_type = _audit_unembedded(db_path, type_filter, args.min_chars)
        plan["dbs"][label] = {"path": str(db_path), "n_total": n_total, "by_type": by_type}

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
            print(f"[embed-backfill] backup: {db_path.name} → {backup}", flush=True)

    grand_totals = {"queried": 0, "embedded": 0, "failed": 0}
    for label, db_path in db_targets:
        n_total = plan["dbs"][label]["n_total"]
        if n_total == 0:
            print(f"[embed-backfill] {db_path.name}: no unembedded rows — skipping", flush=True)
            continue
        print(f"[embed-backfill] {db_path.name}: {n_total} rows to embed", flush=True)
        counters = await _backfill_db(
            db_path, type_filter, args.min_chars, args.batch, args.limit,
        )
        for k in grand_totals:
            grand_totals[k] += counters[k]
        rate = counters["queried"] / max(counters["wall_s"], 1e-3)
        print(f"[embed-backfill] {db_path.name} done: "
              f"{counters['embedded']} embedded, "
              f"{counters['failed']} failed, "
              f"{counters['wall_s']/60:.1f} min, {rate:.1f} rows/s", flush=True)

    print()
    print("══════════════════════════════════════════════════════════════")
    print("  m3-chatlog-backfill-embed COMPLETE")
    print("══════════════════════════════════════════════════════════════")
    print(f"  total queried:  {grand_totals['queried']}")
    print(f"  total embedded: {grand_totals['embedded']}")
    print(f"  total failed:   {grand_totals['failed']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill embeddings for memory_items rows that lack one.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python bin/m3_chatlog_backfill_embed.py --dry-run
  python bin/m3_chatlog_backfill_embed.py
  python bin/m3_chatlog_backfill_embed.py --chatlog --batch 64
  python bin/m3_chatlog_backfill_embed.py --include-types chat_log,message
""",
    )
    ap.add_argument("--core", action="store_true", dest="core_only",
                    help="Only backfill the core memory DB (skip chatlog).")
    ap.add_argument("--chatlog", action="store_true", dest="chatlog_only",
                    help="Only backfill the chatlog DB (skip core).")
    ap.add_argument("--core-db", default=None, help="Explicit path to core memory DB.")
    ap.add_argument("--chatlog-db", default=None, help="Explicit path to chatlog DB.")
    ap.add_argument("--include-types", default=None,
                    help="Comma-separated types to backfill. Default: chat_log,message,"
                         "conversation,summary,note,observation,fact_enriched,fact.")
    ap.add_argument("--all-types", action="store_true",
                    help="Backfill every memory_items type (overrides --include-types).")
    ap.add_argument("--min-chars", type=int, default=10,
                    help="Skip rows whose content is shorter than this. Default 10.")
    ap.add_argument("--batch", type=int, default=32,
                    help="Embed-call batch size. Default 32.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap rows backfilled per DB (smoke testing).")
    ap.add_argument("--dry-run", action="store_true", help="Preview only.")
    ap.add_argument("--skip-backup", action="store_true", help="Don't create a pre-run DB backup.")
    ap.add_argument("--yes", "-y", action="store_true", help="Skip the confirm prompt.")
    args = ap.parse_args()
    if args.core_only and args.chatlog_only:
        sys.exit("ERROR: --core and --chatlog are mutually exclusive.")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
