#!/usr/bin/env python3
"""
embed_backfill.py — fill in missing embeddings for memory_items rows.

Companion to the M3_OBSERVER_NO_EMBED=1 ingest pattern. When ingest writes
rows without embedding (decoupling write throughput from embedder
throughput), this sweeper scans the DB for rows with no entry in
memory_embeddings and embeds them in batches.

Works on any m3-memory DB — the core memory store, a bench workspace,
a future fresh-ingestion DB, anywhere. Filter by --variant / --type /
--user-id / --scope / --id-prefix / --max-age-days to narrow scope.

Resumable by construction: the WHERE NOT EXISTS query IS the resume
marker. Crash mid-run, re-launch, picks up exactly where it left off.

Cost-free at the embedder side (uses local LLM_ENDPOINTS_CSV /
:8081 / LM Studio routing — no API charges).

Usage:

    # Sweep core memory (default DB) — embeds anything missing
    python bin/embed_backfill.py

    # Bench workspace, only one variant
    python bin/embed_backfill.py \\
        --db memory/agent_test_bench.db \\
        --variant m3-observations-bench-LME-M-ingestion-20260428

    # Smoke test: 100 rows, dry-run
    python bin/embed_backfill.py --limit 100 --dry-run

    # Sharded sweepers (run multiple instances on disjoint id prefixes)
    python bin/embed_backfill.py --id-prefix 0 --lockfile /tmp/sweep0.lock &
    python bin/embed_backfill.py --id-prefix 1 --lockfile /tmp/sweep1.lock &

Hardening:
  - Per-batch timeout (--timeout-s)
  - Hard runtime cap (--max-runtime-min)
  - Auto-abort after N consecutive batch failures (--max-consecutive-fails)
  - Dim validation (--expected-dim) — won't write malformed embeddings
  - Per-row size cap (--max-row-bytes) — skips oversize content
  - Optional lockfile to prevent two sweepers racing on the same DB

This script is read-mostly + small bulk writes; safe to run alongside
an active enricher in WAL mode (SQLite handles concurrent reads fine).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
DEFAULT_DB = REPO_ROOT / "memory" / "agent_memory.db"
DEFAULT_BATCH_SIZE = 256
DEFAULT_CONCURRENCY = 4
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_RUNTIME_MIN = 60
DEFAULT_MAX_CONSEC_FAILS = 5
DEFAULT_MAX_ROW_BYTES = 32_768  # bge-m3 ctx is 8192 tokens ≈ 32KB
DEFAULT_EXPECTED_DIM = 1024     # bge-m3 / qwen3-embed default
DEFAULT_CONN_REFRESH_BATCHES = 1000


# ── Status counters used in the final report ──────────────────────────────
# Re-exported from embed_sweep_lib so callers (including tests) keep using
# `embed_backfill.Counters`. The lib owns the canonical class.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from embed_sweep_lib import Counters as _LibCounters, run_embed_loop  # noqa: E402

class Counters(_LibCounters):  # type: ignore[misc, valid-type]
    """Backwards-compat shim: subclass of lib Counters with one extra attr.

    `cache_reuses` was tracked in the original embed_backfill but is
    informational only (never read by the loop). We keep it on the
    subclass so any downstream code that touched it doesn't break.
    """

    def __init__(self) -> None:
        super().__init__()
        self.cache_reuses = 0  # rows whose content_hash already had a vector


# ── Lockfile ──────────────────────────────────────────────────────────────
@contextmanager
def _lockfile_guard(path: Path | None):
    """Refuse to start if another sweeper is running. No-op when path is None."""
    if path is None:
        yield
        return
    if path.exists():
        # Read pid + start time to give actionable error
        try:
            content = path.read_text(encoding="utf-8").strip()
        except Exception:
            content = "(unreadable)"
        raise RuntimeError(
            f"Lockfile already exists: {path}\n"
            f"Another sweeper may be running. Lockfile content: {content!r}\n"
            f"If you are sure no other sweeper is active, remove the file and retry."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()} {int(time.time())}", encoding="utf-8")
    try:
        yield
    finally:
        try:
            path.unlink()
        except Exception:
            pass


# ── Schema sanity check ───────────────────────────────────────────────────
def _verify_schema(db_path: Path) -> None:
    """Confirm the target DB has memory_items + memory_embeddings tables
    in the shape we expect. Raise with a clear actionable message if not."""
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
                    f"Run `python bin/migrate_memory.py --db {db_path} up` first."
                )
        # Probe for required columns
        mi_cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_items)")}
        for col in ("id", "content", "type", "variant", "user_id"):
            if col not in mi_cols:
                raise RuntimeError(
                    f"memory_items.{col} missing from {db_path}. "
                    f"DB schema is too old; run migrate_memory.py up."
                )
        me_cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_embeddings)")}
        for col in ("memory_id", "embedding", "content_hash"):
            if col not in me_cols:
                raise RuntimeError(
                    f"memory_embeddings.{col} missing from {db_path}. "
                    f"DB schema is too old; run migrate_memory.py up."
                )
    finally:
        conn.close()


# ── Build the candidate-row query ─────────────────────────────────────────
def _build_query(
    args: argparse.Namespace, after_id: str | None = None,
) -> tuple[str, list]:
    """Return (sql, params) for the candidate-rows SELECT.

    Selects rows that:
      - are not soft-deleted
      - have content (post-trim)
      - match optional filters (variant/type/user_id/scope/id-prefix/age)
      - have NO embedding row in memory_embeddings
      - have id > after_id when set (paginates past in-run skips)

    The after_id pagination matters: rows we skip mid-run (oversize,
    bad-dim, failed batch) still satisfy the NOT EXISTS predicate on
    the next cycle, so without forward progress on id we'd reselect
    them forever. Tracking the highest-id-seen-this-run and filtering
    `mi.id > ?` makes the sweep monotonic.

    NULL-safe on every column. Uses the existing index on
    memory_items.id (PK) and the memory_embeddings.memory_id index.
    """
    where = [
        "COALESCE(mi.is_deleted, 0) = 0",
        "LENGTH(TRIM(COALESCE(mi.content, ''))) > 0",
        "NOT EXISTS (SELECT 1 FROM memory_embeddings me WHERE me.memory_id = mi.id)",
    ]
    params: list = []

    if after_id is not None:
        where.append("mi.id > ?")
        params.append(after_id)
    if args.variant:
        placeholders = ",".join("?" * len(args.variant))
        where.append(f"mi.variant IN ({placeholders})")
        params.extend(args.variant)
    if args.type:
        placeholders = ",".join("?" * len(args.type))
        where.append(f"mi.type IN ({placeholders})")
        params.extend(args.type)
    if args.user_id:
        where.append("COALESCE(mi.user_id, '') = ?")
        params.append(args.user_id)
    if args.scope:
        where.append("COALESCE(mi.scope, '') = ?")
        params.append(args.scope)
    if args.id_prefix:
        where.append("mi.id LIKE ?")
        params.append(f"{args.id_prefix.lower()}%")
    if args.max_age_days is not None:
        # Older than N days = created_at < (now - N days)
        where.append("mi.created_at < datetime('now', ?)")
        params.append(f"-{int(args.max_age_days)} days")

    sql = f"""
        SELECT mi.id, mi.content, mi.title, mi.metadata_json
        FROM memory_items mi
        WHERE {' AND '.join(where)}
        ORDER BY mi.id
        LIMIT ?
    """
    return sql, params


# ── Pre-flight count ──────────────────────────────────────────────────────
def _count_pending(db_path: Path, args: argparse.Namespace) -> int:
    """How many rows would be in scope?  Cheap; uses same WHERE clause."""
    sql, params = _build_query(args)
    # Replace the LIMIT with a COUNT.
    count_sql = sql.replace(
        "SELECT mi.id, mi.content, mi.title, mi.metadata_json", "SELECT COUNT(*)"
    )
    # Strip ORDER BY + LIMIT (we don't pass a LIMIT param for count)
    count_sql = count_sql.split("ORDER BY")[0]
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        return conn.execute(count_sql, params).fetchone()[0]
    finally:
        conn.close()


# ── Main async loop ───────────────────────────────────────────────────────
async def _run_sweep(args: argparse.Namespace, counters: Counters) -> int:
    """Delegate to embed_sweep_lib.run_embed_loop with the embed_backfill-
    specific fetch + write callbacks.

    Late import: memory_core reads M3_DATABASE at import time, so we must
    set the env var BEFORE importing. Once imported, _db() ties to that
    path for the lifetime of the process.
    """
    os.environ["M3_DATABASE"] = str(args.db)
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))

    import memory_core as mc  # noqa: E402

    started = time.monotonic()
    deadline = started + args.max_runtime_min * 60.0

    # ── Fetch callback ────────────────────────────────────────────────
    # Pulls candidates from args.db using _build_query. The helper passes
    # us the high-water mark (after_id) and a fetch size; we just plug
    # them into the existing SQL builder.
    def _fetch(after_id, limit):
        sql, params = _build_query(args, after_id=after_id)
        params_with_limit = params + [limit]
        conn = sqlite3.connect(str(args.db), timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            return conn.execute(sql, params_with_limit).fetchall()
        finally:
            conn.close()

    # ── Write callback ────────────────────────────────────────────────
    # Persists one embedding row + the chroma_sync_queue marker. Uses
    # mc._db() so writes funnel through memory_core's connection pool
    # (keeps WAL / busy_timeout behavior consistent with the rest of
    # the codebase). Returns True iff a row was newly written.
    def _write(mid: str, vec: list[float], model_str: str, content_hash: str) -> bool:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with mc._db() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO memory_embeddings "
                "(id, memory_id, embedding, embed_model, dim, created_at, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _new_uuid(),
                    mid,
                    mc._pack(vec),
                    model_str,
                    len(vec),
                    now_iso,
                    content_hash,
                ),
            )
            if cur.rowcount > 0:
                db.execute(
                    "INSERT INTO chroma_sync_queue (memory_id, operation) "
                    "VALUES (?, ?)",
                    (mid, "upsert"),
                )
                return True
            return False

    # ── Text transform callback ───────────────────────────────────────
    # Match memory_write_impl's inline behavior: augment with anchors.
    # Skip when --no-augment-anchors set.
    if args.no_augment_anchors:
        transform = None  # use lib default (identity)
    else:
        def transform(text: str, metadata) -> str:
            return mc._augment_embed_text_with_anchors(text, metadata)

    # Drive the loop
    await run_embed_loop(
        fetch_candidates=_fetch,
        write_embedding=_write,
        counters=counters,
        embed_many=mc._embed_many,
        content_hash_fn=mc._content_hash,
        transform_text=transform if transform is not None else (lambda t, _m: t),
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        timeout_s=args.timeout_s,
        deadline_s=deadline,
        max_consecutive_fails=args.max_consecutive_fails,
        max_row_bytes=args.max_row_bytes,
        expected_dim=(args.expected_dim if args.expected_dim else None),
        limit=args.limit,
        log=_log,
    )
    return 0


# ── Helpers ───────────────────────────────────────────────────────────────
def _log(msg: str, ts: str | None = None) -> None:
    """ASCII-safe stdout. Avoids the cp1252 trap on Windows."""
    if ts is None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"[{ts}] [embed_backfill] {msg}\n"
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
    except UnicodeEncodeError:
        sys.stdout.write(line.encode("ascii", "replace").decode("ascii"))
        sys.stdout.flush()


def _new_uuid() -> str:
    import uuid
    return str(uuid.uuid4())


def _print_report(counters: Counters, started_at: float, db_path: Path) -> None:
    elapsed = time.monotonic() - started_at
    rate = counters.embedded / max(elapsed, 1e-3)
    print()
    print("=" * 64)
    print("  embed_backfill COMPLETE")
    print("=" * 64)
    print(f"  db:                {db_path}")
    print(f"  scanned:           {counters.scanned}")
    print(f"  embedded:          {counters.embedded}")
    print(f"  skipped (empty):   {counters.skipped_empty}")
    print(f"  skipped (oversize):{counters.skipped_oversize}")
    print(f"  skipped (bad dim): {counters.skipped_bad_dim}")
    print(f"  failed batches:    {counters.failed_batches}")
    print(f"  cache reuses:      (handled by _embed_many internal cache)")
    print(f"  wall time:         {elapsed:.1f}s")
    print(f"  effective rate:    {rate:.1f} embeds/s")
    if counters.errors_by_class:
        print()
        print("  Error breakdown:")
        for cls, n in sorted(counters.errors_by_class.items(),
                             key=lambda kv: kv[1], reverse=True):
            print(f"    {cls:<32} {n}")
    print()


# ── argparse + main ───────────────────────────────────────────────────────
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Selection
    sel = ap.add_argument_group("selection")
    sel.add_argument("--db", type=Path, default=Path(os.environ.get("M3_DATABASE", str(DEFAULT_DB))),
                     help=f"Target DB. Default: $M3_DATABASE or {DEFAULT_DB}")
    sel.add_argument("--variant", action="append", default=[],
                     help="Filter to one variant. Repeatable for OR.")
    sel.add_argument("--type", action="append", default=[],
                     help="Filter to one memory type. Repeatable for OR.")
    sel.add_argument("--user-id", type=str, default=None,
                     help="Filter to one user_id.")
    sel.add_argument("--scope", type=str, default=None,
                     help="Filter to one scope (user/session/agent/org).")
    sel.add_argument("--id-prefix", type=str, default=None,
                     help="Backfill only rows whose id starts with this hex prefix. "
                          "Use to shard across multiple sweeper instances.")
    sel.add_argument("--max-age-days", type=int, default=None,
                     help="Only rows older than N days. Useful when you want "
                          "to leave fresh writes alone for a window first.")
    sel.add_argument("--limit", type=int, default=None,
                     help="Stop after N successful embeds. Smoke testing.")

    # Performance
    perf = ap.add_argument_group("performance")
    perf.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                      help=f"Rows per embed call. Default: {DEFAULT_BATCH_SIZE}.")
    perf.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                      help=f"Concurrent batches in flight. Default: {DEFAULT_CONCURRENCY}. "
                           f"Cap by your llama-server's --parallel slots.")
    perf.add_argument("--connection-refresh", type=int, default=DEFAULT_CONN_REFRESH_BATCHES,
                      help=f"Batches between connection-pool recycle. "
                           f"Default: {DEFAULT_CONN_REFRESH_BATCHES}.")

    # Resilience
    res = ap.add_argument_group("resilience")
    res.add_argument("--timeout-s", type=float, default=DEFAULT_TIMEOUT_S,
                     help=f"Per-batch embed call timeout. Default: {DEFAULT_TIMEOUT_S}s.")
    res.add_argument("--max-runtime-min", type=int, default=DEFAULT_MAX_RUNTIME_MIN,
                     help=f"Hard kill at N min wall-clock. Default: {DEFAULT_MAX_RUNTIME_MIN}.")
    res.add_argument("--max-consecutive-fails", type=int, default=DEFAULT_MAX_CONSEC_FAILS,
                     help=f"Abort after N back-to-back batch fails. "
                          f"Default: {DEFAULT_MAX_CONSEC_FAILS}.")
    res.add_argument("--max-row-bytes", type=int, default=DEFAULT_MAX_ROW_BYTES,
                     help=f"Skip rows whose content > N bytes. "
                          f"Default: {DEFAULT_MAX_ROW_BYTES} (bge-m3 ctx limit).")
    res.add_argument("--expected-dim", type=int, default=DEFAULT_EXPECTED_DIM,
                     help=f"Skip embeddings whose dim != N. "
                          f"Default: {DEFAULT_EXPECTED_DIM}. Pass 0 to disable.")
    res.add_argument("--lockfile", type=Path, default=None,
                     help="Refuse to start if this file exists; create it on start, "
                          "delete on clean exit. Use for cron / scheduled sweepers.")

    # Behavior
    beh = ap.add_argument_group("behavior")
    beh.add_argument("--no-augment-anchors", action="store_true",
                     help="Skip _augment_embed_text_with_anchors before embed. "
                          "Default OFF — anchors match memory_write_impl behavior.")
    beh.add_argument("--dry-run", action="store_true",
                     help="Print plan and counts; don't embed or write.")

    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Schema sanity
    try:
        _verify_schema(args.db)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    # Plan
    pending = _count_pending(args.db, args)
    print(f"DB:       {args.db}")
    print(f"Pending:  {pending}")
    print(f"Filters:")
    if args.variant:    print(f"  variant: {args.variant}")
    if args.type:       print(f"  type: {args.type}")
    if args.user_id:    print(f"  user_id: {args.user_id}")
    if args.scope:      print(f"  scope: {args.scope}")
    if args.id_prefix:  print(f"  id_prefix: {args.id_prefix!r}")
    if args.max_age_days is not None: print(f"  max_age_days: {args.max_age_days}")
    if args.limit:      print(f"  limit: {args.limit}")
    print(f"Performance: batch_size={args.batch_size} concurrency={args.concurrency}")
    print(f"Resilience:  timeout={args.timeout_s}s max_runtime={args.max_runtime_min}min "
          f"max_consec_fails={args.max_consecutive_fails}")
    if args.dry_run:
        print()
        print("(dry-run: no embeddings written)")
        return 0

    if pending == 0:
        print()
        print("No rows pending; nothing to do.")
        return 0

    # Lockfile guard + run
    counters = Counters()
    started_at = time.monotonic()
    try:
        with _lockfile_guard(args.lockfile):
            asyncio.run(_run_sweep(args, counters))
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
        _log("INTERRUPTED: caught Ctrl-C; reporting partial run.")

    _print_report(counters, started_at, args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
