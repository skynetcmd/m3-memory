"""Benchmark gate for entity_count first-class queries.

New MCP tools should hit P50 < 5 ms, P95 < 20 ms, P99 < 50 ms on a
representative corpus.

This script seeds a synthetic corpus that mirrors LongMemEval-S
proportions (~500 conversations × ~500 turns × ~2.5 entities per turn =
~625k mentions, ~250k entities). Runs each impl 200 times across random
conversation_ids and reports the latency percentiles.

Usage:
    python tests/bench_entity_count.py [--n-conv 500] [--turns-per-conv 500] [--repeats 200]

Exits non-zero (1) if any percentile fails the gate, suitable for CI.

The seed is deterministic (SEED=2027) so runs are repeatable.
"""
from __future__ import annotations

import argparse
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


# Latency budgets (ms) for the bench gate.
GATES = {
    "p50": 5.0,
    "p95": 20.0,
    "p99": 50.0,
}

SEED = 2027

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memory_items (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'note',
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    conversation_id TEXT,
    valid_from TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    scope TEXT NOT NULL DEFAULT 'agent'
);
CREATE INDEX IF NOT EXISTS ix_memory_items_conv ON memory_items(conversation_id);

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    attributes_json TEXT DEFAULT '{}',
    valid_from TEXT,
    valid_to TEXT,
    content_hash TEXT,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_entities_canonical_type ON entities(canonical_name, entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);

CREATE TABLE IF NOT EXISTS memory_item_entities (
    memory_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    mention_text TEXT,
    mention_offset INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0.85,
    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (memory_id, entity_id, mention_offset)
);
CREATE INDEX IF NOT EXISTS idx_mie_entity ON memory_item_entities(entity_id);
"""

ENTITY_TYPES = ["product", "organization", "place", "person", "event",
                 "date", "quantity", "topic"]


def seed_corpus(db_path: Path, n_conv: int, turns_per_conv: int,
                 ents_per_turn: float) -> dict:
    """Build a synthetic corpus. Returns metadata about seeded data."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA_SQL)

    # PRAGMAs for bulk insert speed.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    rng = random.Random(SEED)

    # Build a shared entity vocabulary — entities are reused across conversations
    # for realism (entity rows aren't per-conv, only mentions are scoped).
    n_unique_entities = max(int(n_conv * turns_per_conv * ents_per_turn / 5), 100)
    print(f"[seed] {n_unique_entities} unique entities, "
          f"{n_conv} conversations, {turns_per_conv} turns/conv")

    ent_ids = [f"ent_{i:07d}" for i in range(n_unique_entities)]
    ent_rows = [
        (eid, f"Name{i:07d}", rng.choice(ENTITY_TYPES))
        for i, eid in enumerate(ent_ids)
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO entities (id, canonical_name, entity_type) "
        "VALUES (?, ?, ?)",
        ent_rows,
    )
    conn.commit()
    print("[seed] entities inserted")

    conv_ids = [f"conv_{i:05d}" for i in range(n_conv)]
    total_mentions = 0

    for conv_idx, conv_id in enumerate(conv_ids):
        mi_rows = []
        mie_rows = []
        for t in range(turns_per_conv):
            mid = f"{conv_id}_t{t:04d}"
            mi_rows.append((mid, conv_id, f"content {conv_id} turn {t}"))
            # Sample ~ents_per_turn entities for this turn.
            k = max(1, int(rng.gauss(ents_per_turn, 1.0)))
            for j, eid in enumerate(rng.sample(ent_ids, min(k, len(ent_ids)))):
                mie_rows.append((mid, eid, "mention", j))
                total_mentions += 1
        conn.executemany(
            "INSERT OR IGNORE INTO memory_items (id, conversation_id, content) "
            "VALUES (?, ?, ?)",
            mi_rows,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO memory_item_entities "
            "(memory_id, entity_id, mention_text, mention_offset) "
            "VALUES (?, ?, ?, ?)",
            mie_rows,
        )
        if (conv_idx + 1) % 50 == 0:
            conn.commit()
            print(f"[seed] {conv_idx + 1}/{n_conv} conversations "
                  f"({total_mentions:,} mentions so far)")

    conn.commit()
    # Analyze for query planner.
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()

    return {
        "n_conv": n_conv,
        "turns_per_conv": turns_per_conv,
        "n_entities": n_unique_entities,
        "total_mentions": total_mentions,
    }


def time_call(fn, *args, **kwargs) -> float:
    """Time a single call. Returns elapsed ms."""
    t0 = time.perf_counter()
    fn(*args, **kwargs)
    return (time.perf_counter() - t0) * 1000.0


def summary(label: str, samples: list[float]) -> dict:
    """Compute and print P50/P95/P99/max for samples."""
    samples_sorted = sorted(samples)
    n = len(samples_sorted)
    p50 = samples_sorted[n // 2]
    p95 = samples_sorted[int(n * 0.95)] if n > 1 else samples_sorted[0]
    p99 = samples_sorted[int(n * 0.99)] if n > 1 else samples_sorted[0]
    mx = samples_sorted[-1]
    mn = samples_sorted[0]
    avg = statistics.mean(samples_sorted)
    print(f"\n[bench] {label} ({n} samples)")
    print(f"  min={mn:6.2f}  p50={p50:6.2f}  p95={p95:6.2f}  "
          f"p99={p99:6.2f}  max={mx:6.2f}  avg={avg:6.2f}  (ms)")
    return {"p50": p50, "p95": p95, "p99": p99, "max": mx,
            "min": mn, "avg": avg, "n": n}


def main() -> int:
    parser = argparse.ArgumentParser(description="entity_count bench gate")
    parser.add_argument("--n-conv", type=int, default=500,
                         help="Number of conversations to seed")
    parser.add_argument("--turns-per-conv", type=int, default=500,
                         help="Turns per conversation")
    parser.add_argument("--ents-per-turn", type=float, default=2.5,
                         help="Avg entities per turn")
    parser.add_argument("--repeats", type=int, default=200,
                         help="Sample size for each percentile")
    parser.add_argument("--keep-db", action="store_true",
                         help="Don't delete the seeded DB on exit")
    args = parser.parse_args()

    tmpdir = Path(tempfile.mkdtemp(prefix="bench_entity_count_"))
    db_path = tmpdir / "bench.db"
    print(f"[seed] DB: {db_path}")

    t_seed = time.perf_counter()
    meta = seed_corpus(db_path, args.n_conv, args.turns_per_conv,
                        args.ents_per_turn)
    print(f"[seed] done in {time.perf_counter() - t_seed:.1f}s — "
          f"{meta['total_mentions']:,} mentions, "
          f"{meta['n_entities']:,} entities")

    os.environ["M3_DATABASE"] = str(db_path)

    # Import AFTER setting M3_DATABASE so the active context resolves correctly.
    from memory.entity_count import (
        count_entities_impl,
        count_mentions_impl,
        list_mentions_impl,
    )

    rng = random.Random(SEED + 1)
    conv_ids = [f"conv_{i:05d}" for i in range(args.n_conv)]

    # Warmup — eat the cold-start: _lazy_init, statement preparation, page
    # cache. Without this, the first sample of every impl is 100-300x slower
    # than steady-state and pollutes the P99. The perf budgets target
    # steady-state under load, not cold-call latency.
    print("[warmup] priming connection pool and page cache...")
    for _ in range(5):
        count_entities_impl(rng.choice(conv_ids))
        count_mentions_impl(rng.choice(conv_ids), limit=10)
    print("[warmup] done")

    # Pick a stable entity id per conversation for list_mentions.
    # We sample one mention per conv to learn an existing entity id.
    sample_conn = sqlite3.connect(str(db_path))
    sample_ents_by_conv = {}
    for cid in rng.sample(conv_ids, min(args.repeats, len(conv_ids))):
        row = sample_conn.execute(
            "SELECT mie.entity_id FROM memory_item_entities mie "
            "JOIN memory_items mi ON mie.memory_id = mi.id "
            "WHERE mi.conversation_id = ? LIMIT 1",
            (cid,),
        ).fetchone()
        if row:
            sample_ents_by_conv[cid] = row[0]
    sample_conn.close()

    # ── 1. count_entities ─────────────────────────────────────────────────
    samples = []
    for _ in range(args.repeats):
        cid = rng.choice(conv_ids)
        samples.append(time_call(count_entities_impl, cid))
    r_count = summary("count_entities_impl (all types)", samples)

    samples = []
    for _ in range(args.repeats):
        cid = rng.choice(conv_ids)
        samples.append(time_call(count_entities_impl, cid,
                                   entity_type="product"))
    r_count_filt = summary("count_entities_impl (type=product)", samples)

    # ── 2. count_mentions ─────────────────────────────────────────────────
    samples = []
    for _ in range(args.repeats):
        cid = rng.choice(conv_ids)
        samples.append(time_call(count_mentions_impl, cid, limit=100))
    r_mentions = summary("count_mentions_impl (limit=100)", samples)

    # ── 3. list_mentions ──────────────────────────────────────────────────
    samples = []
    for cid, eid in list(sample_ents_by_conv.items())[: args.repeats]:
        samples.append(time_call(list_mentions_impl, cid, entity_id=eid))
    r_list = summary("list_mentions_impl (by entity_id)", samples)

    # ── Gate ──────────────────────────────────────────────────────────────
    print()
    print(f"[gate] Budgets: P50<{GATES['p50']}ms  P95<{GATES['p95']}ms  "
          f"P99<{GATES['p99']}ms")
    print()
    failures = []
    for label, r in [
        ("count_entities (all)",       r_count),
        ("count_entities (type=prod)", r_count_filt),
        ("count_mentions",             r_mentions),
        ("list_mentions",              r_list),
    ]:
        verdict = "PASS"
        for pct in ("p50", "p95", "p99"):
            if r[pct] > GATES[pct]:
                verdict = "FAIL"
                failures.append(f"{label}.{pct}={r[pct]:.2f}>{GATES[pct]}")
        print(f"  {label:30s} P50={r['p50']:6.2f}  P95={r['p95']:6.2f}  "
              f"P99={r['p99']:6.2f}  [{verdict}]")

    if not args.keep_db:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"\n[cleanup] removed {tmpdir}")
    else:
        print(f"\n[cleanup] DB retained: {db_path}")

    if failures:
        print(f"\n[gate] FAILED ({len(failures)} percentile(s) over budget):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\n[gate] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
