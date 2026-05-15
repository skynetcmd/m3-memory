#!/usr/bin/env python3
"""Per-operation micro-benchmark: Rust ``m3_core_rs`` vs Python baselines.

WHAT THIS IS
------------
This is a PER-OPERATION micro-benchmark of the m3-memory oxidation swaps. For
each operation that was moved from Python to the Rust ``m3_core_rs`` extension,
it measures the Rust path and the original Python baseline in isolation, on
realistic input sizes, and reports the speedup ratio.

WHAT THIS IS NOT
----------------
This is NOT an end-to-end retrieval benchmark. The oxidation plan's headline
target (<50ms retrieval p50) is an end-to-end metric that requires the LME-S
benchmark stack, which is NOT available in this repo. Nothing here should be
read as an end-to-end speedup claim. This answers only: "for this one
operation, at this input size, is the Rust path faster than Python, and by
how much?"

MEASUREMENT METHOD
------------------
* FFI-INCLUSIVE: every Rust call goes through ``m3_core_rs.*`` exactly as
  production ``memory_core.py`` does. The cost of marshalling Python lists
  across the Python<->Rust boundary IS part of the measured cost. We do not
  benchmark the Rust crate in isolation.
* ``time.perf_counter``; warm-up iterations (untimed) then N timed iterations,
  N chosen so each op accumulates >= ~1s of timed work. We report MEDIAN and
  P95 (mean is skewed by GC / scheduler noise).
* Sanity assertions: for sha256 and cosine we assert Rust and Python outputs
  agree (exact / within float tolerance) BEFORE timing. A benchmark of a
  wrong implementation is worthless.

Run: ``python tests/bench_oxidation.py``
"""

import os
import sys
import time
import struct
import hashlib
import sqlite3
import statistics

# --------------------------------------------------------------------------
# skip-guard: hard deps
# --------------------------------------------------------------------------
try:
    import m3_core_rs
except ImportError:
    print("SKIP: m3_core_rs not installed (pip install m3-memory[oxidation]). Nothing to benchmark.")
    sys.exit(0)

try:
    import numpy as np
except ImportError:
    print("SKIP: numpy not installed. Cannot run Python cosine baselines.")
    sys.exit(0)

# bin/ on path for the Python baselines
_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from embedding_utils import cosine as py_cosine
from embedding_utils import batch_cosine as py_batch_cosine
from embedding_utils import unpack as eu_unpack

# crypto_provider.get_sha256 is the production Python baseline for sha256;
# with M3_CRYPTO_BACKEND=DEFAULT it is hashlib.sha256(...).hexdigest().
from crypto_provider import get_sha256 as py_get_sha256

# Force the pure-Python redaction path so we can call _scrub_python directly
# regardless of whether the Rust core is loaded by the module.
os.environ["M3_CORE_RS_DISABLE"] = "1"
import chatlog_redaction
py_scrub = chatlog_redaction._scrub_python

DIM = 1024
SEARCH_ROW_CAP = 5000
MMR_LAMBDA = 0.7
FLOAT_TOL = 1e-5


# --------------------------------------------------------------------------
# timing harness
# --------------------------------------------------------------------------
def bench(fn, target_seconds=1.0, warmup=5, max_iters=2_000_000):
    """Warm up, then time fn() repeatedly until >= target_seconds elapsed.

    Returns (median_us, p95_us, n_iters).
    """
    for _ in range(warmup):
        fn()
    # calibrate: how many iters to roughly hit target
    t0 = time.perf_counter()
    fn()
    one = time.perf_counter() - t0
    if one <= 0:
        one = 1e-7
    n = max(1, min(max_iters, int(target_seconds / one)))
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1e6)  # us
    samples.sort()
    median = statistics.median(samples)
    p95 = samples[min(len(samples) - 1, int(round(0.95 * (len(samples) - 1))))]
    return median, p95, n


# --------------------------------------------------------------------------
# real vectors from the DB (fall back to synthetic)
# --------------------------------------------------------------------------
def load_real_vectors(n_wanted):
    db = os.path.join(os.path.dirname(_HERE), "memory", "agent_memory.db")
    if not os.path.exists(db):
        return None
    try:
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT embedding FROM memory_embeddings WHERE dim=? LIMIT ?",
            (DIM, n_wanted),
        ).fetchall()
        conn.close()
        vecs = []
        for (blob,) in rows:
            v = eu_unpack(blob)
            if len(v) == DIM:
                vecs.append(v)
        return vecs if len(vecs) >= 2 else None
    except Exception as exc:  # noqa: BLE001
        print(f"  (DB read failed: {exc} -- using synthetic vectors)")
        return None


_real = load_real_vectors(SEARCH_ROW_CAP + 16)
USING_REAL_VECTORS = _real is not None
if USING_REAL_VECTORS:
    VEC_POOL = _real
    VEC_SOURCE = f"REAL ({len(_real)} vectors from memory/agent_memory.db, dim={DIM})"
else:
    rng = np.random.default_rng(20260514)
    VEC_POOL = [
        rng.standard_normal(DIM).astype(np.float32).tolist()
        for _ in range(SEARCH_ROW_CAP + 16)
    ]
    VEC_SOURCE = f"SYNTHETIC ({len(VEC_POOL)} random float32 vectors, dim={DIM})"


def take_vecs(n):
    """Return n vectors from the pool (cycled if pool is short)."""
    if n <= len(VEC_POOL):
        return VEC_POOL[:n]
    out = []
    while len(out) < n:
        out.extend(VEC_POOL)
    return out[:n]


# --------------------------------------------------------------------------
# Python MMR replica (faithful copy from tests/test_oxidation_parity.py,
# which itself replicates memory_core.py ~lines 3489-3519).
# --------------------------------------------------------------------------
def py_mmr_scored_select(relevance, candidates, lambda_, k, force_seed_first):
    n = len(candidates)
    k = min(k, n)
    if k <= 0:
        return []
    selected = []
    remaining = list(range(n))
    if force_seed_first:
        selected.append(remaining.pop(0))
    while remaining and len(selected) < k:
        best_idx, best_mmr = 0, -float("inf")
        for ci, cand_i in enumerate(remaining):
            c_score = relevance[cand_i]
            max_sim = max(
                (py_cosine(candidates[cand_i], candidates[s]) for s in selected),
                default=0.0,
            )
            mmr_score = lambda_ * c_score - (1 - lambda_) * max_sim
            if mmr_score > best_mmr:
                best_mmr = mmr_score
                best_idx = ci
        selected.append(remaining.pop(best_idx))
    return selected


# Python displacement-guard replica (faithful copy from
# tests/test_oxidation_parity.py, replicating memory_core.py ~515-573).
def py_enforce_displacement_guard(items, protected_ranks, margin):
    if not items or protected_ranks <= 0 or margin <= 1.0:
        return list(items)
    work = list(items)
    n = len(work)
    limit = min(protected_ranks, n)
    for rank in range(limit):
        score, is_exp = work[rank]
        if not is_exp:
            continue
        next_primary_idx = None
        for j in range(rank + 1, n):
            if not work[j][1]:
                next_primary_idx = j
                break
        if next_primary_idx is None:
            continue
        primary_score, _ = work[next_primary_idx]
        if score > 0 and primary_score > 0 and score >= margin * primary_score:
            continue
        work[rank], work[next_primary_idx] = work[next_primary_idx], work[rank]
    return work


# --------------------------------------------------------------------------
# results table
# --------------------------------------------------------------------------
RESULTS = []  # (op, size, py_med, py_p95, rs_med, rs_p95, speedup, unit)


def record(op, size, py_med, py_p95, rs_med, rs_p95, unit="us"):
    speedup = (py_med / rs_med) if rs_med > 0 else float("inf")
    RESULTS.append((op, size, py_med, py_p95, rs_med, rs_p95, speedup, unit))
    div = 1000.0 if unit == "ms" else 1.0
    verdict = "rust wins" if speedup >= 1.05 else (
        "PY wins" if speedup <= 0.95 else "break-even")
    print(
        f"  {op:<22} {size:<14} "
        f"py {py_med/div:9.3f}/{py_p95/div:9.3f}  "
        f"rs {rs_med/div:9.3f}/{rs_p95/div:9.3f} {unit}  "
        f"x{speedup:6.2f}  [{verdict}]"
    )


# ==========================================================================
# SANITY ASSERTIONS (before any timing)
# ==========================================================================
print("=" * 100)
print("SANITY CHECKS (Rust vs Python output agreement -- must pass before timing)")
print("=" * 100)

_sane = True

# sha256
_s = b"the quick brown fox jumps over the lazy dog" * 7
_rs_h = m3_core_rs.sha256_hex_bytes(_s)
_py_h = py_get_sha256(_s)
_hl_h = hashlib.sha256(_s).hexdigest()
if _rs_h == _py_h == _hl_h:
    print(f"  sha256       OK  rust == crypto_provider == hashlib  ({_rs_h[:16]}...)")
else:
    _sane = False
    print(f"  sha256       !!! MISMATCH  rust={_rs_h}  py={_py_h}  hashlib={_hl_h}")

# cosine
_a, _b = take_vecs(2)
_rs_c = m3_core_rs.cosine(_a, _b)
_py_c = py_cosine(_a, _b)
if abs(_rs_c - _py_c) < FLOAT_TOL:
    print(f"  cosine       OK  |rust - py| = {abs(_rs_c - _py_c):.2e} < {FLOAT_TOL}  "
          f"(rust={_rs_c:.6f})")
else:
    _sane = False
    print(f"  cosine       !!! MISMATCH  rust={_rs_c}  py={_py_c}  "
          f"diff={abs(_rs_c - _py_c):.2e}")

# batch cosine
_q = take_vecs(1)[0]
_corp = take_vecs(64)
_rs_bc = m3_core_rs.cosine_batch(_q, _corp)
_py_bc = py_batch_cosine(_q, _corp)
_bc_ok = len(_rs_bc) == len(_py_bc) and all(
    abs(r - p) < FLOAT_TOL for r, p in zip(_rs_bc, _py_bc))
if _bc_ok:
    print(f"  cosine_batch OK  all {len(_rs_bc)} scores agree within {FLOAT_TOL}")
else:
    _sane = False
    print("  cosine_batch !!! MISMATCH between rust and python batch scores")

if not _sane:
    print("\n*** WARNING: sanity assertions FAILED. Benchmark numbers below compare")
    print("*** implementations that DO NOT agree -- treat them as meaningless.\n")
else:
    print("\nAll sanity checks passed -- proceeding to timing.\n")


# ==========================================================================
# BENCHMARKS
# ==========================================================================
print("=" * 100)
print(f"VECTOR SOURCE: {VEC_SOURCE}")
print("=" * 100)
print("Columns: py median/p95 | rs median/p95 | speedup = py_median / rs_median")
print()

# --- 1. SHA-256 hex --------------------------------------------------------
print("[1] SHA-256 hex  (Rust: sha256_hex_bytes  |  Python: crypto_provider.get_sha256 -> hashlib)")
for label, nbytes in [("small 100B", 100), ("medium 2KB", 2048), ("large 32KB", 32768)]:
    data = os.urandom(nbytes)
    py_med, py_p95, _ = bench(lambda d=data: py_get_sha256(d))
    rs_med, rs_p95, _ = bench(lambda d=data: m3_core_rs.sha256_hex_bytes(d))
    record("sha256_hex", label, py_med, py_p95, rs_med, rs_p95, "us")
print()

# --- 2. cosine (single) ----------------------------------------------------
print("[2] cosine single  (Rust: cosine  |  Python: embedding_utils.cosine numpy path)")
_a, _b = take_vecs(2)
py_med, py_p95, _ = bench(lambda: py_cosine(_a, _b))
rs_med, rs_p95, _ = bench(lambda: m3_core_rs.cosine(_a, _b))
record("cosine", f"{DIM}-dim", py_med, py_p95, rs_med, rs_p95, "us")
print()

# --- 3. batch cosine -------------------------------------------------------
print("[3] batch cosine  (Rust: cosine_batch  |  Python: embedding_utils.batch_cosine numpy path)")
_q = take_vecs(1)[0]
for n in (100, 1000, 5000):
    corp = take_vecs(n)
    py_med, py_p95, _ = bench(lambda c=corp: py_batch_cosine(_q, c))
    rs_med, rs_p95, _ = bench(lambda c=corp: m3_core_rs.cosine_batch(_q, c))
    unit = "ms" if py_med > 1000 or rs_med > 1000 else "us"
    record("cosine_batch", f"corpus={n}", py_med, py_p95, rs_med, rs_p95, unit)
print()

# --- 4. MMR rerank ---------------------------------------------------------
print("[4] MMR rerank  (Rust: mmr_rerank_scored force_seed_first=True  |  Python: memory_core MMR loop replica)")
for pool in (24, 150, 500):
    k = pool // 3
    cands = take_vecs(pool)
    # descending-sorted blended relevance, as the production call site guarantees
    relevance = [1.0 - i / pool for i in range(pool)]
    # Python MMR is O(pool^2) cosine calls — cap iters to avoid multi-minute hangs
    # on large pools. 50 iters is enough for a stable median.
    py_max = 50 if pool >= 150 else 500
    py_med, py_p95, _ = bench(
        lambda c=cands, r=relevance, kk=k: py_mmr_scored_select(r, c, MMR_LAMBDA, kk, True),
        target_seconds=0.5,
        max_iters=py_max,
    )
    rs_med, rs_p95, _ = bench(
        lambda c=cands, r=relevance, kk=k: m3_core_rs.mmr_rerank_scored(r, c, MMR_LAMBDA, kk, True),
        target_seconds=0.5,
    )
    unit = "ms" if py_med > 1000 or rs_med > 1000 else "us"
    record("mmr_rerank", f"pool={pool},k={k}", py_med, py_p95, rs_med, rs_p95, unit)
print()

# --- 5. displacement guard -------------------------------------------------
print("[5] displacement guard  (Rust: enforce_displacement_guard  |  Python: _enforce_expansion_displacement_guard replica)")
for n in (10, 100):
    # alternating expansion/primary with descending scores -- exercises swaps
    items = [(float(n - i), (i % 2 == 0)) for i in range(n)]
    py_med, py_p95, _ = bench(lambda it=items: py_enforce_displacement_guard(it, 3, 2.0))
    rs_med, rs_p95, _ = bench(lambda it=items: m3_core_rs.enforce_displacement_guard(it, 3, 2.0))
    record("displacement_guard", f"rows={n}", py_med, py_p95, rs_med, rs_p95, "us")
print()

# --- 6. redaction ----------------------------------------------------------
print("[6] redaction  (Rust: m3_core_rs.scrub  |  Python: chatlog_redaction._scrub_python)")
RED_CFG = {
    "enabled": True,
    "patterns": ["api_keys", "github_tokens", "pii"],
    "custom_regex": [],
    "redact_pii": True,
}
dirty_turn = (
    "Sure, here's the deploy config. The API key is "
    "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 and the CI token is "
    "ghp_" + "a" * 36 + ". Ping me at ops@example.com or 415-555-0199 if the "
    "rollout stalls. " + "We should also review the retry budget and the "
    "circuit-breaker thresholds before the next release window. " * 8
)
clean_turn = (
    "Let's walk through the retrieval path. The query embedding gets compared "
    "against the candidate pool, then MMR reranks for diversity, then the "
    "displacement guard protects the top ranks. " * 6
)
for label, text in [("dirty ~%dch" % len(dirty_turn), dirty_turn),
                    ("clean ~%dch" % len(clean_turn), clean_turn)]:
    # warm both compile caches
    py_scrub(text, RED_CFG)
    m3_core_rs.scrub(text, RED_CFG)
    py_med, py_p95, _ = bench(lambda t=text: py_scrub(t, RED_CFG))
    rs_med, rs_p95, _ = bench(lambda t=text: m3_core_rs.scrub(t, RED_CFG))
    record("redaction", label, py_med, py_p95, rs_med, rs_p95, "us")
print()


# ==========================================================================
# SUMMARY
# ==========================================================================
print("=" * 100)
print("SUMMARY  --  per-operation micro-benchmark, FFI-inclusive, NOT end-to-end retrieval")
print("=" * 100)
print(f"{'operation':<22}{'input size':<18}{'py median':>13}{'rs median':>13}{'speedup':>10}  verdict")
print("-" * 100)
for op, size, py_med, py_p95, rs_med, rs_p95, speedup, unit in RESULTS:
    div = 1000.0 if unit == "ms" else 1.0
    verdict = "rust faster" if speedup >= 1.05 else (
        "PYTHON faster" if speedup <= 0.95 else "break-even")
    print(
        f"{op:<22}{size:<18}"
        f"{py_med/div:>10.3f}{unit:<3}"
        f"{rs_med/div:>10.3f}{unit:<3}"
        f"{speedup:>9.2f}x  {verdict}"
    )
print("-" * 100)
print(f"vectors: {VEC_SOURCE}")
print(f"sanity checks: {'ALL PASSED' if _sane else 'FAILED -- numbers unreliable'}")
print()
print("NOTE: This measures each swapped operation in isolation (FFI-inclusive).")
print("It is NOT an end-to-end retrieval benchmark. The plan's <50ms p50 retrieval")
print("target requires the LME-S benchmark stack, which is not available in this repo.")
