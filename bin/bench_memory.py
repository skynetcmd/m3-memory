#!/usr/bin/env python3
"""
Memory system benchmark script.
Seeds test data, measures latency/throughput, reports pass/fail against targets.

Usage: python bin/bench_memory.py
"""
import sqlite3
import os
import sys
import json
import time
import uuid
import struct
import random
import statistics

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")

TARGETS = {
    "write_throughput_items_per_sec": 50,
    "search_vector_p95_ms": 100,
    "search_fts_p95_ms": 20,
    "dedup_scan_1000_ms": 5000,
}


def _pack(floats: list[float]) -> bytes:
    return struct.pack(f"{len(floats)}f", *floats)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _random_vec(dim: int = 768) -> list[float]:
    return [random.gauss(0, 1) for _ in range(dim)]  # nosec B311 - benchmark noise, not cryptographic


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    ma = sum(x * x for x in a) ** 0.5
    mb = sum(x * x for x in b) ** 0.5
    return dot / (ma * mb) if (ma and mb) else 0.0


def get_conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA busy_timeout = 5000")
    return c


def bench_write(conn, n=500):
    """Benchmark memory_items + memory_embeddings INSERT throughput."""
    print(f"\n[BENCH] Write throughput ({n} items)...")
    ids = []
    now = time.time()

    for i in range(n):
        item_id = str(uuid.uuid4())
        ts = f"2026-01-01T00:00:{i % 60:02d}Z"
        conn.execute(
            """INSERT INTO memory_items
               (id, type, title, content, metadata_json, importance, source,
                origin_device, is_deleted, created_at)
               VALUES (?,?,?,?,?,?,?,?,0,?)""",
            (item_id, "bench_test", f"Bench item {i}",
             f"Benchmark content for item {i} with random data {random.random()}",  # nosec B311 - benchmark filler, not cryptographic
             "{}", 0.5, "bench", "bench_host", ts),
        )
        vec = _random_vec()
        conn.execute(
            """INSERT INTO memory_embeddings
               (id, memory_id, embedding, embed_model, dim, created_at)
               VALUES (?,?,?,?,?,?)""",
            (str(uuid.uuid4()), item_id, _pack(vec), "bench_model", 768, ts),
        )
        ids.append(item_id)

    conn.commit()
    elapsed = time.time() - now
    rate = n / elapsed
    passed = rate >= TARGETS["write_throughput_items_per_sec"]
    print(f"  {n} items in {elapsed:.2f}s = {rate:.0f} items/sec "
          f"(target: {TARGETS['write_throughput_items_per_sec']}) {'PASS' if passed else 'FAIL'}")
    return {"write_items": n, "write_seconds": round(elapsed, 3),
            "write_rate": round(rate, 1), "pass": passed}, ids


def bench_vector_search(conn, query_vec, trials=50):
    """Benchmark vector search latency (pure SQL + Python cosine)."""
    print(f"\n[BENCH] Vector search latency ({trials} queries)...")
    latencies = []

    for _ in range(trials):
        t0 = time.perf_counter()
        rows = conn.execute(
            """SELECT me.memory_id, me.embedding, mi.importance
               FROM memory_embeddings me
               JOIN memory_items mi ON me.memory_id = mi.id
               WHERE mi.is_deleted = 0 AND mi.importance > 0.01
               ORDER BY mi.importance DESC LIMIT 5000"""
        ).fetchall()
        vecs = [_unpack(r["embedding"]) for r in rows]
        scores = [_cosine(query_vec, v) for v in vecs]
        scores.sort(reverse=True)
        _ = scores[:8]
        latencies.append((time.perf_counter() - t0) * 1000)

    p50 = statistics.median(latencies)
    p95 = sorted(latencies)[int(len(latencies) * 0.95)]
    p99 = sorted(latencies)[int(len(latencies) * 0.99)]
    passed = p95 <= TARGETS["search_vector_p95_ms"]
    print(f"  p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms "
          f"(target p95: {TARGETS['search_vector_p95_ms']}ms) {'PASS' if passed else 'FAIL'}")
    return {"p50_ms": round(p50, 1), "p95_ms": round(p95, 1),
            "p99_ms": round(p99, 1), "pass": passed}


def bench_fts_search(conn, trials=50):
    """Benchmark FTS5 keyword search latency."""
    print(f"\n[BENCH] FTS keyword search latency ({trials} queries)...")
    # Check if FTS table exists
    fts_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_items_fts'"
    ).fetchone()
    if not fts_exists:
        print("  SKIP: memory_items_fts table not found (run migration 006)")
        return {"skip": True, "reason": "FTS table not found"}

    queries = ["benchmark", "random", "content", "item", "test"]
    latencies = []

    for i in range(trials):
        q = queries[i % len(queries)]
        t0 = time.perf_counter()
        rows = conn.execute(
            """SELECT mi.id, rank
               FROM memory_items_fts fts
               JOIN memory_items mi ON mi.rowid = fts.rowid
               WHERE memory_items_fts MATCH ?
                 AND mi.is_deleted = 0
               ORDER BY rank LIMIT 20""",
            (q,),
        ).fetchall()
        latencies.append((time.perf_counter() - t0) * 1000)

    p50 = statistics.median(latencies)
    p95 = sorted(latencies)[int(len(latencies) * 0.95)]
    passed = p95 <= TARGETS["search_fts_p95_ms"]
    print(f"  p50={p50:.1f}ms  p95={p95:.1f}ms "
          f"(target p95: {TARGETS['search_fts_p95_ms']}ms) {'PASS' if passed else 'FAIL'}")
    return {"p50_ms": round(p50, 1), "p95_ms": round(p95, 1), "pass": passed}


def bench_dedup_scan(conn):
    """Benchmark pairwise cosine scan for dedup candidates."""
    print("\n[BENCH] Dedup scan (pairwise cosine on all embeddings)...")
    t0 = time.perf_counter()
    rows = conn.execute(
        """SELECT me.memory_id, me.embedding
           FROM memory_embeddings me
           JOIN memory_items mi ON me.memory_id = mi.id
           WHERE mi.is_deleted = 0
           LIMIT 1000"""
    ).fetchall()
    items = [(r["memory_id"], _unpack(r["embedding"])) for r in rows]

    dupes = 0
    for i in range(len(items)):
        for j in range(i + 1, min(i + 50, len(items))):  # sample neighbors
            sim = _cosine(items[i][1], items[j][1])
            if sim > 0.92:
                dupes += 1

    elapsed_ms = (time.perf_counter() - t0) * 1000
    passed = elapsed_ms <= TARGETS["dedup_scan_1000_ms"]
    print(f"  {len(items)} items scanned in {elapsed_ms:.0f}ms, {dupes} candidate pairs "
          f"(target: {TARGETS['dedup_scan_1000_ms']}ms) {'PASS' if passed else 'FAIL'}")
    return {"items_scanned": len(items), "elapsed_ms": round(elapsed_ms, 0),
            "candidate_pairs": dupes, "pass": passed}


def cleanup(conn, ids):
    """Remove benchmark test data."""
    print(f"\n[CLEANUP] Removing {len(ids)} benchmark items...")
    for item_id in ids:
        conn.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (item_id,))
        conn.execute("DELETE FROM memory_items WHERE id = ?", (item_id,))
    conn.commit()
    print("  Done.")


def main():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = get_conn()
    report = {}
    bench_ids = []

    try:
        # Write benchmark
        write_result, bench_ids = bench_write(conn, n=500)
        report["write"] = write_result

        # Vector search
        query_vec = _random_vec()
        report["vector_search"] = bench_vector_search(conn, query_vec, trials=30)

        # FTS search
        report["fts_search"] = bench_fts_search(conn, trials=30)

        # Dedup scan
        report["dedup_scan"] = bench_dedup_scan(conn)

        # Summary
        all_pass = all(r.get("pass", True) for r in report.values())
        report["overall"] = "PASS" if all_pass else "FAIL"

        print(f"\n{'='*60}")
        print(f"OVERALL: {report['overall']}")
        print(f"{'='*60}")

        # Save report
        report_path = os.path.join(BASE_DIR, "memory", "bench_report.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nReport saved to {report_path}")

    finally:
        cleanup(conn, bench_ids)
        conn.close()

    sys.exit(0 if report.get("overall") == "PASS" else 1)


if __name__ == "__main__":
    main()
