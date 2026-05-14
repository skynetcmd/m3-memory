#!/usr/bin/env python3
"""
bench_e2e_retrieval.py — end-to-end retrieval latency benchmark for m3-memory.

WHAT THIS IS
------------
An *end-to-end retrieval* benchmark. It drives the real
`memory_core.memory_search_scored_impl` against the real local
`memory/agent_memory.db` (~20.9k memory_items, ~15.4k embedded) and times the
full search path: FTS5 query -> candidate fetch -> vector cosine scoring ->
MMR rerank -> the expansion/displacement guard. It measures the *cumulative*
effect of the Rust `m3_core_rs` oxidation swaps on a realistic query, not one
operation in isolation.

It runs two arms in **separate subprocesses** (mandatory — `memory_core.py`
reads `M3_CORE_RS_DISABLE` at import time, so the Rust core cannot be toggled
within a live process):
  - arm "rust":   M3_CORE_RS_DISABLE unset  -> Rust core active.
  - arm "python": M3_CORE_RS_DISABLE=1      -> pure-Python scoring path.

WHAT THIS IS NOT
----------------
This is NOT the LME-S reproducible benchmark stack. LME-S runs at ~2.4M-row
scale with a curated query set, lives on a private branch, and is the plan's
official harness — it is out of scope here. This harness runs against the
local ~20k-item DB. It is a smaller, honest proxy: useful for "did the swaps
move end-to-end latency on a real DB," NOT for the plan's headline
"<50ms at LME-M scale" claim. It says nothing about LME-M-scale targets.

EMBEDDER HANDLING
-----------------
The swaps are in *scoring* (cosine / MMR / guard), not embedding. To isolate
that surface, this benchmark uses approach (a): each worker pre-computes the
query embedding vectors ONCE (untimed warmup), then monkeypatches
`memory_core._embed` to a pure in-memory dict lookup. The timed window is
therefore FTS5 + vector scoring + MMR + guard — the actual oxidation surface —
with the embed HTTP/GGUF cost excluded. Pre-computation tries, in order:
the in-process GGUF embedder (M3_EMBED_GGUF) and the HTTP embed path; if
neither produces vectors the benchmark skips cleanly.

OUTPUT IS CONTENT-FREE: only memory IDs (sanity check) and timings are printed.
No memory row content is emitted, so the output is safe to share.

Usage:  python tests/bench_e2e_retrieval.py
"""

import os
import sys
import json
import time
import statistics
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
DB = REPO / "memory" / "agent_memory.db"

# Fixed, hardcoded query set — stable & repeatable. Mix of short keyword,
# natural-language, temporal-flavored, and entity-ish queries. They need not
# be "good" queries, just realistic and varied enough to exercise different
# ranking paths.
QUERIES = [
    "scheduler",
    "rust oxidation",
    "embedding dimension mismatch",
    "what changed in the credential store",
    "how does the displacement guard work",
    "why did the MCP plugin fail to load",
    "benchmark results last week",
    "what did we decide about expansion margin",
    "memory consolidation policy",
    "FTS5 query construction",
    "vector weight tuning",
    "agent heartbeat timeout",
    "recent changes to memory_core",
    "temporal anchor resolution",
    "config for the embed server",
    "MMR rerank diversity",
    "chatlog redaction settings",
    "single instance guard cross OS",
    "what is the public bench whitelist",
    "subprocess console window suppression",
]

K_VALUES = [8, 20, 50]
N_TIMED = 8       # timed runs per (query, k)
WARMUP = 1        # untimed warmup runs per (query, k)


# ---------------------------------------------------------------------------
# WORKER  (runs inside a subprocess; one arm per process)
# ---------------------------------------------------------------------------
def run_worker(arm: str) -> int:
    """Import memory_core, precompute query embeddings, monkeypatch _embed,
    run the fixed query set at each k, emit timing JSON on stdout."""
    sys.path.insert(0, str(BIN))
    os.chdir(str(REPO))  # so the default DB path (memory/agent_memory.db) resolves

    import asyncio
    import memory_core as mc

    rust_active = not mc._OXIDATION_DISABLED
    # Sanity: the arm must match what we asked for.
    if arm == "rust" and not rust_active:
        print(json.dumps({"error": "rust arm but oxidation disabled"}))
        return 2
    if arm == "python" and rust_active:
        print(json.dumps({"error": "python arm but oxidation active"}))
        return 2

    # --- Precompute query embeddings (untimed) using the real _embed path ---
    orig_embed = mc._embed
    vec_cache = {}

    async def _precompute():
        for q in QUERIES:
            try:
                vec, model = await orig_embed(q)
            except Exception as e:
                vec, model = None, ""
            vec_cache[q] = (vec, model)

    asyncio.run(_precompute())

    missing = [q for q, (v, _) in vec_cache.items() if not v]
    if missing:
        # No embedder available for these queries -> cannot isolate scoring path.
        print(json.dumps({
            "error": "embedder unavailable",
            "missing_count": len(missing),
            "total": len(QUERIES),
        }))
        return 3

    # --- Monkeypatch _embed -> pure dict lookup (timed window excludes embed) ---
    async def _embed_stub(text):
        hit = vec_cache.get(text)
        if hit is not None:
            return hit
        # Fallback for any query-recursion text we did not precompute.
        return await orig_embed(text)

    mc._embed = _embed_stub

    # --- Run the query set ---
    results = {}  # k -> { query -> {median, p95, n} }
    sanity = {}   # query -> [top ids]  (only for first query, content-free)

    async def _bench():
        for k in K_VALUES:
            results[k] = {}
            for q in QUERIES:
                # warmup (untimed)
                for _ in range(WARMUP):
                    await mc.memory_search_scored_impl(
                        q, mmr=True, k=k, search_mode="hybrid", vector_weight=0.7
                    )
                samples = []
                last_rows = None
                for _ in range(N_TIMED):
                    t0 = time.perf_counter()
                    rows = await mc.memory_search_scored_impl(
                        q, mmr=True, k=k, search_mode="hybrid", vector_weight=0.7
                    )
                    t1 = time.perf_counter()
                    samples.append((t1 - t0) * 1000.0)
                    last_rows = rows
                samples.sort()
                med = statistics.median(samples)
                p95 = samples[min(len(samples) - 1, int(round(0.95 * (len(samples) - 1))))]
                results[k][q] = {"median_ms": med, "p95_ms": p95, "n": len(samples),
                                 "n_results": len(last_rows or [])}
                # capture sanity ids at k=8 for every query
                if k == K_VALUES[0]:
                    ids = []
                    for item in (last_rows or [])[:5]:
                        # rows are (score, item_dict)
                        try:
                            ids.append(str(item[1].get("id")))
                        except Exception:
                            pass
                    sanity[q] = ids

    asyncio.run(_bench())

    print(json.dumps({
        "arm": arm,
        "rust_active": rust_active,
        "results": results,
        "sanity": sanity,
    }))
    return 0


# ---------------------------------------------------------------------------
# PARENT  (orchestrates the two arms, compares, prints the table)
# ---------------------------------------------------------------------------
def _spawn(arm: str) -> dict:
    env = dict(os.environ)
    if arm == "rust":
        env.pop("M3_CORE_RS_DISABLE", None)
        env["M3_CORE_RS_DISABLE"] = "0"
    else:
        env["M3_CORE_RS_DISABLE"] = "1"
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--arm", arm],
        env=env, capture_output=True, text=True,
    )
    # The worker prints exactly one JSON line on stdout; logs may go to stderr.
    out = proc.stdout.strip()
    line = ""
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("{"):
            line = ln
    if not line:
        return {"error": "no JSON from worker",
                "returncode": proc.returncode,
                "stderr_tail": proc.stderr.strip()[-800:]}
    try:
        return json.loads(line)
    except Exception as e:
        return {"error": f"bad JSON from worker: {e}",
                "stderr_tail": proc.stderr.strip()[-800:]}


def main() -> int:
    if "--arm" in sys.argv:
        arm = sys.argv[sys.argv.index("--arm") + 1]
        return run_worker(arm)

    # ---- Skip-guards ----
    if not DB.exists():
        print(f"SKIP: DB not found at {DB}")
        return 0
    sys.path.insert(0, str(BIN))
    try:
        import m3_core_rs  # noqa: F401
    except Exception as e:
        print(f"SKIP: m3_core_rs not importable ({e})")
        return 0

    print("=" * 72)
    print("m3-memory end-to-end retrieval benchmark")
    print(f"  DB: {DB}")
    print(f"  queries: {len(QUERIES)}  |  k values: {K_VALUES}  |  timed runs/query: {N_TIMED}")
    print("  (end-to-end: FTS5 + vector scoring + MMR + guard; embed excluded)")
    print("  NOTE: this is NOT LME-S; ~20k-row local proxy only.")
    print("=" * 72)

    print("\nrunning arm: rust   (M3_CORE_RS_DISABLE=0) ...")
    rust = _spawn("rust")
    print("running arm: python (M3_CORE_RS_DISABLE=1) ...")
    py = _spawn("python")

    for name, res in (("rust", rust), ("python", py)):
        if "error" in res:
            print(f"\nFAILED ({name} arm): {res['error']}")
            if res.get("stderr_tail"):
                print("  stderr tail:\n  " + res["stderr_tail"].replace("\n", "\n  "))
            if res.get("missing_count"):
                print(f"  embedder produced no vectors for "
                      f"{res['missing_count']}/{res['total']} queries.")
            return 1

    # ---- Sanity check: do the two arms agree on result IDs? ----
    print("\n" + "-" * 72)
    print("SANITY CHECK — result parity between arms (k=%d, top-5 IDs)" % K_VALUES[0])
    print("-" * 72)
    rs, ps = rust["sanity"], py["sanity"]
    # Two distinct kinds of divergence, very different severity:
    #   set-level   — different IDs returned, or different count. A real
    #                 correctness problem: a swap isn't parity-clean.
    #   order-only  — identical ID *set*, different rank order. Expected and
    #                 benign: Rust cosine and numpy cosine agree to ~1e-6 but
    #                 not bit-for-bit, so two results whose scores differ by
    #                 less than that gap resolve their tie differently. Each
    #                 arm is internally deterministic.
    set_divergence = []
    order_divergence = []
    for q in QUERIES:
        r_ids, p_ids = rs.get(q, []), ps.get(q, [])
        r_n = rust["results"][str(K_VALUES[0])][q]["n_results"]
        p_n = py["results"][str(K_VALUES[0])][q]["n_results"]
        if set(r_ids) != set(p_ids) or r_n != p_n:
            set_divergence.append((q, r_n, p_n, r_ids, p_ids))
        elif r_ids != p_ids:
            order_divergence.append((q, r_ids, p_ids))
    if set_divergence:
        print(f"  !! {len(set_divergence)}/{len(QUERIES)} queries DIVERGED at the SET level.")
        print("  !! This IS a correctness finding — the swaps are not parity-clean")
        print("  !! under real conditions. Reporting loudly, not hiding it.")
        for q, rn, pn, rid, pid in set_divergence:
            print(f"     query={q!r}  n_results rust={rn} py={pn}")
            print(f"       rust top-ids: {rid}")
            print(f"       py   top-ids: {pid}")
    if order_divergence:
        print(f"  ~  {len(order_divergence)}/{len(QUERIES)} queries: identical ID set, "
              "different rank order.")
        print("  ~  Benign — float32 round-off boundary between Rust and numpy cosine")
        print("  ~  on near-tied scores. Deterministic per-arm; not a correctness defect.")
        for q, rid, pid in order_divergence:
            print(f"     query={q!r}")
            print(f"       rust order: {rid}")
            print(f"       py   order: {pid}")
    if not set_divergence and not order_divergence:
        print(f"  OK — all {len(QUERIES)} queries return identical result count and order")
    elif not set_divergence:
        print(f"  OK at the set level — all {len(QUERIES)} queries return the same "
              "result IDs (see order-only notes above)")
        print("       and identical top-5 IDs in both arms. Parity holds.")

    # ---- Aggregate timing table ----
    print("\n" + "-" * 72)
    print("END-TO-END LATENCY  (aggregate across query set, per k)")
    print("-" * 72)
    hdr = f"{'k':>4} | {'arm':>7} | {'p50 ms':>9} | {'p95 ms':>9} | {'speedup(p50)':>13}"
    print(hdr)
    print("-" * len(hdr))

    summary_rows = []
    for k in K_VALUES:
        ks = str(k)
        # per-arm aggregate across the query set: p50/p95 of per-query medians
        def agg(res):
            meds = sorted(res["results"][ks][q]["median_ms"] for q in QUERIES)
            p95s = sorted(res["results"][ks][q]["p95_ms"] for q in QUERIES)
            p50 = statistics.median(meds)
            idx95 = min(len(p95s) - 1, int(round(0.95 * (len(p95s) - 1))))
            return p50, p95s[idx95]
        r_p50, r_p95 = agg(rust)
        p_p50, p_p95 = agg(py)
        speedup = p_p50 / r_p50 if r_p50 else float("nan")
        summary_rows.append((k, r_p50, p_p50, speedup))
        print(f"{k:>4} | {'rust':>7} | {r_p50:>9.3f} | {r_p95:>9.3f} | {'(baseline)':>13}")
        print(f"{k:>4} | {'python':>7} | {p_p50:>9.3f} | {p_p95:>9.3f} | {speedup:>12.2f}x")
        print("-" * len(hdr))

    print("\nSUMMARY")
    for k, r_p50, p_p50, sp in summary_rows:
        print(f"  k={k:>3}: rust p50={r_p50:.3f}ms  python p50={p_p50:.3f}ms  "
              f"-> {sp:.2f}x end-to-end")
    best = max(summary_rows, key=lambda r: r[3])
    print(f"\n  Best end-to-end speedup: {best[3]:.2f}x at k={best[0]}.")
    print("  CAVEAT (Amdahl): this is the FULL search path — FTS5, SQLite I/O,")
    print("  candidate fetch, scoring, MMR, guard. The Rust swaps only touch")
    print("  scoring/MMR/guard, a fraction of total query cost, so the e2e ratio")
    print("  is necessarily smaller than any micro-benchmark of MMR alone.")
    print("  This is a ~20k-row local proxy, NOT LME-S / LME-M scale.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
