"""Read-only retrieval-quality regression baseline.

Snapshots `memory_search_scored_impl` and `memory_search_routed_impl`
outputs for a deterministic sample of queries against the live
`agent_chatlog.db`. The result IDs and rank order at each k are the
load-bearing comparison; scores are rounded to 1e-6 to absorb
floating-point noise from CUDA drivers + httpx retry timing while still
catching real ranking drift.

Required BEFORE the Phase 4.B sub-6+7 extraction of the four search
impls — see `docs/MEMORY_CORE_MODULARIZATION_LESSONS.md` lesson #6.

## What it captures, exactly

For each of N_QUERIES randomly-seeded short queries pulled from real
content:

  - `_scored_impl(q, k=K)` with `vector_kind_strategy='default'`
  - `_scored_impl(q, k=K)` with `vector_kind_strategy='max'`
  - `_routed_impl(q, k=K)` (no extra knobs — defaults)

Each result is a list of (rank, memory_id, score_rounded_to_6dp).
A sha256 of the full structured payload is also stored so a single
diff line tells you "drifted" or "stable" before you read the full row
list.

## Behaviors deliberately turned off / pinned

  - `recency_bias=0`           — eliminates the dated-row interpolation
                                  bonus, which would shift scores if any
                                  row's `valid_from` field is touched.
  - `explain=False`            — keeps the result-dict shape stable.
  - `adaptive_k=False`         — disables the elbow trim so test_run = test_run
                                  on the same pool size.
  - Federation is left at module default — `CHROMA_BASE_URL` is set,
                                            so federation may fire and add
                                            rows. That's intentional —
                                            we WANT to detect if a
                                            Chroma row drops out. But:
  - **NOTE**: federation hits a remote ChromaDB. If that
              host is down at compare time, the diff will show federated
              row dropouts that are environmental, not regressions. The
              suggested workflow is to capture and compare in the same
              session (same Chroma state) — or to set `M3_DISABLE_FEDERATION`
              first if you want corpus-only comparison.

## Sample queries

Sampled from `memory_items.content` deterministically (seeded). Short
queries (30-200 chars) — enough to be semantically meaningful, short
enough to behave like real user input. The seeded sample is stable
across runs.

## Workflow

  # Capture baseline before refactor (or first run)
  M3_RETRIEVAL_REFRESH_BASELINE=1 python tests/capture_retrieval_baseline.py

  # Compare current behavior to baseline (default)
  python tests/capture_retrieval_baseline.py

The baseline lives at `.scratch/migration_baseline/retrieval_baseline.json`
and is gitignored along with the rest of `.scratch/`.

Exit code: 0 if results match baseline, 1 if any drift detected
(printed in detail).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = ROOT / "bin"
BASELINE_DIR = ROOT / ".scratch" / "migration_baseline"
BASELINE_PATH = BASELINE_DIR / "retrieval_baseline.json"
sys.path.insert(0, str(BIN_DIR))

# In-process embedder required so cache lookups + cold embeds are
# deterministic across runs (HTTP fallback would introduce network jitter).
# Set M3_EMBED_GGUF in your env to the local bge-m3 GGUF path before running.
os.environ.setdefault("GGML_CUDA_DISABLE_GRAPHS", "1")
if not os.environ.get("M3_EMBED_GGUF"):
    print("ERROR: set M3_EMBED_GGUF to the bge-m3 GGUF path before running.",
          file=sys.stderr)
    sys.exit(2)
os.environ.setdefault("M3_EMBED_STREAMS", "1")
os.environ.setdefault("M3_EMBED_CTX", "8192")
os.environ.setdefault("M3_EMBED_SEQ_MAX", "8")
os.environ.setdefault("M3_EMBED_N_BATCH", "8192")
os.environ.setdefault("M3_EMBED_N_UBATCH", "8192")

CHATLOG_DB = ROOT / "memory" / "agent_chatlog.db"

# ── Sampling knobs ────────────────────────────────────────────────────────────
# Larger N reduces false negatives (more chances to detect drift) at the cost
# of capture/compare wall-time. 60 queries × 4 search variants × ~0.3s/call
# ~= 1.5 minutes on the 5080 in-process.
SEED = 1729  # Ramanujan; stable for the life of this test
N_QUERIES = 60
K = 20  # top-k captured per query
SCORE_DECIMALS = 6  # round to this many decimals before hashing
# ──────────────────────────────────────────────────────────────────────────────


def sample_queries(n: int) -> list[str]:
    """Deterministic sample of short queries pulled from content snippets.

    Read-only with mode=ro&immutable=1 so accidental writes raise instead of
    silently mutating the test DB.
    """
    uri = f"file:{CHATLOG_DB.as_posix()}?mode=ro&immutable=1"
    c = sqlite3.connect(uri, uri=True)
    cur = c.cursor()
    # Pull a deterministic candidate set — sort by id so the same rows always
    # surface across runs. Filter to mid-length text for "natural query" shape.
    cur.execute(
        """SELECT content FROM memory_items
           WHERE is_deleted = 0 AND COALESCE(content,'') != ''
             AND LENGTH(content) BETWEEN 40 AND 200
           ORDER BY id LIMIT 2000"""
    )
    cands = [r[0] for r in cur.fetchall()]
    c.close()
    rng = random.Random(SEED)
    # Sample without replacement so we get N distinct queries.
    return rng.sample(cands, min(n, len(cands)))


def fingerprint_result(rows: list[tuple[float, dict]]) -> dict:
    """Convert a raw `_scored_impl` / `_routed_impl` result into a stable
    structured snapshot.

    Returns:
      {
        "ids":      [memory_id_rank_0, ..., memory_id_rank_k-1],
        "scores":   [round(score, 6), ...],
        "sha256":   "<hash of the JSON-serialized (ids, scores) tuple>"
      }

    Why two fields plus a hash:
      - `ids` alone catches rank-order changes. If a refactor reorders the
        top-K but keeps the same set, ids[i] changes — drift.
      - `scores` catches "same ranking, different magnitudes" — a sign that
        scoring weights drifted but the elbow/MMR happened to preserve order.
      - `sha256` is the single-line "drifted yes/no" indicator for the
        progress log.
    """
    ids = []
    scores = []
    for score, item in rows[:K]:
        mid = item.get("id") if isinstance(item, dict) else None
        ids.append(mid)
        scores.append(round(float(score), SCORE_DECIMALS))
    payload = json.dumps({"ids": ids, "scores": scores}, sort_keys=True)
    return {
        "ids": ids,
        "scores": scores,
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
    }


async def capture_one_query(mc, query: str) -> dict:
    """Run all three retrieval variants for one query. Returns a dict keyed
    by variant name with the fingerprint of each."""
    out: dict = {"query": query[:80]}
    # Variant 1: scored, vector_kind_strategy='default'
    try:
        rows = await mc.memory_search_scored_impl(
            query, k=K, mmr=True, recency_bias=0.0,
            explain=False, adaptive_k=False,
            vector_kind_strategy="default",
        )
        out["scored_default"] = fingerprint_result(rows)
    except Exception as e:
        out["scored_default"] = {"error": str(e)[:200]}
    # Variant 2: scored, vector_kind_strategy='max' (dual-embed-aware)
    try:
        rows = await mc.memory_search_scored_impl(
            query, k=K, mmr=True, recency_bias=0.0,
            explain=False, adaptive_k=False,
            vector_kind_strategy="max",
        )
        out["scored_max"] = fingerprint_result(rows)
    except Exception as e:
        out["scored_max"] = {"error": str(e)[:200]}
    # Variant 3: routed (the production path)
    try:
        rows = await mc.memory_search_routed_impl(query, k=K, mmr=True)
        out["routed"] = fingerprint_result(rows)
    except Exception as e:
        out["routed"] = {"error": str(e)[:200]}
    return out


async def capture() -> dict:
    """Run the full sweep. Returns the full baseline payload."""
    import memory_core as mc
    queries = sample_queries(N_QUERIES)
    print(f"running {len(queries)} queries × 3 variants...")
    out: list[dict] = []
    t0 = time.perf_counter()
    for i, q in enumerate(queries):
        out.append(await capture_one_query(mc, q))
        if (i + 1) % 10 == 0:
            elapsed = time.perf_counter() - t0
            print(f"  {i+1}/{len(queries)} done in {elapsed:.1f}s "
                  f"({(i+1)/elapsed:.1f} q/s)", flush=True)
    elapsed = time.perf_counter() - t0
    print(f"  done: {len(queries)} queries × 3 variants in {elapsed:.1f}s")
    return {
        "seed": SEED,
        "n_queries": len(queries),
        "k": K,
        "score_decimals": SCORE_DECIMALS,
        "elapsed_s": round(elapsed, 2),
        "results": out,
    }


def compare(current: dict, baseline: dict) -> tuple[bool, list[str]]:
    """Returns (matched, drift_lines). drift_lines is a human-readable list of
    every query × variant that diverged, with both fingerprints printed."""
    drift: list[str] = []
    if current["seed"] != baseline["seed"]:
        drift.append(
            f"  SEED MISMATCH — current={current['seed']} baseline={baseline['seed']}; "
            f"queries differ across runs, comparison is invalid"
        )
        return False, drift
    if current["n_queries"] != baseline["n_queries"]:
        drift.append(
            f"  N_QUERIES MISMATCH — current={current['n_queries']} "
            f"baseline={baseline['n_queries']}"
        )

    cur_results = current["results"]
    base_results = baseline["results"]

    for i, (c, b) in enumerate(zip(cur_results, base_results)):
        for variant in ("scored_default", "scored_max", "routed"):
            c_fp = c.get(variant, {})
            b_fp = b.get(variant, {})
            # Compare the cheap sha256 first; only if different, drill in.
            if c_fp.get("sha256") == b_fp.get("sha256"):
                continue
            # Drift — emit a detailed diff.
            drift.append(
                f"  [{i:>2}] {variant:<14} drift "
                f"q={c.get('query', '')[:60]!r}\n"
                f"      baseline ids: {b_fp.get('ids', [])[:5]}...\n"
                f"      current  ids: {c_fp.get('ids', [])[:5]}...\n"
                f"      baseline sha: {b_fp.get('sha256')}\n"
                f"      current  sha: {c_fp.get('sha256')}"
            )

    return (len(drift) == 0), drift


async def main():
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    refresh = bool(os.environ.get("M3_RETRIEVAL_REFRESH_BASELINE"))

    if refresh or not BASELINE_PATH.exists():
        if not BASELINE_PATH.exists():
            print(f"No baseline at {BASELINE_PATH} — capturing fresh.")
        else:
            print("M3_RETRIEVAL_REFRESH_BASELINE set — overwriting baseline.")
        current = await capture()
        BASELINE_PATH.write_text(json.dumps(current, indent=2, sort_keys=True))
        print(f"  wrote baseline: {BASELINE_PATH}")
        print("  re-run without M3_RETRIEVAL_REFRESH_BASELINE to compare.")
        sys.exit(0)

    print(f"Comparing against baseline: {BASELINE_PATH}")
    baseline = json.loads(BASELINE_PATH.read_text())
    print(f"  baseline: seed={baseline['seed']} n_queries={baseline['n_queries']} "
          f"k={baseline['k']}")
    current = await capture()
    matched, drift_lines = compare(current, baseline)

    if matched:
        print("\nOK — retrieval output is byte-identical to baseline.")
        sys.exit(0)
    else:
        print(f"\nDRIFT DETECTED — {len(drift_lines)} variant(s) diverged:")
        for line in drift_lines:
            print(line)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
