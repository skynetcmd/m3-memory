#!/usr/bin/env python3
"""Per-operation micro-benchmark: FTS + jaccard + packed-vector Rust paths.

Companion to ``tests/bench_oxidation.py`` covering the hot-path functions that
benchmark did NOT exercise: ``sanitize_fts``, ``compile_fts_query``,
``token_jaccard``, ``token_jaccard_batch``, ``cosine_batch_packed``, and
``mmr_rerank_scored_packed``.

Same conventions as bench_oxidation.py:
* FFI-inclusive (every Rust call crosses the PyO3 boundary as production does).
* Output-verified: Rust vs Python output agreement is asserted BEFORE timing.
* median / p95 over N iters auto-sized to ~>=1s of timed work.

NOT covered: ``rank_hybrid_packed``. Its Python "baseline" is not an isolated
function — it is the legacy ranking path in memory/search.py (sort + MMR +
temporal/recency boosts intertwined with row-dict assembly). A faithful
like-for-like micro-benchmark would require reconstructing that whole path, so
it is intentionally omitted rather than benched against a non-equivalent stub.

Run: ``python tests/bench_oxidation_fts_packed.py``
"""

import os
import re
import statistics
import struct
import sys
import time

try:
    import m3_core_rs
except ImportError:
    print("SKIP: m3_core_rs not installed (pip install m3-memory[oxidation]).")
    sys.exit(0)

try:
    import numpy as np
except ImportError:
    print("SKIP: numpy not installed.")
    sys.exit(0)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(os.path.dirname(_HERE), "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

DIM = 1024
FLOAT_TOL = 1e-5
MMR_LAMBDA = 0.7


# --------------------------------------------------------------------------
# Pure-Python baselines — faithful copies of the fallback bodies in
# bin/memory/fts.py and bin/memory/entity.py (the code that runs when the
# Rust core is absent). Copied (not imported+monkeypatched) so the timing is
# unambiguously the Python path with no m3_core_rs dispatch in the way.
# --------------------------------------------------------------------------
_FTS_OPERATORS = re.compile(r"\b(OR|AND|NOT|NEAR)\b")
_FTS_NON_TERM = re.compile(r"[^\w\s]", re.UNICODE)
_SEARCHABLE_PUNCT = str.maketrans({c: " " for c in "?!:.,;/\"'"})
_TOKEN_PUNCT_RE = re.compile(r"[^\w\s]")


def py_sanitize_fts(query, max_len=500):
    if len(query) > max_len:
        query = query[:max_len]
    query = _FTS_OPERATORS.sub(" ", query)
    query = _FTS_NON_TERM.sub(" ", query)
    return query.strip()


def _py_sanitize_for_searchable(text):
    if not text:
        return ""
    return text.lower().translate(_SEARCHABLE_PUNCT)


def py_compile_fts_query(query, mode):
    is_exact_query = (query.startswith('"') and query.endswith('"')) or (
        query.startswith("'") and query.endswith("'")
    )
    if is_exact_query:
        inner = query[1:-1].replace('"', '""')
        return f'"{inner}"', True
    clean = py_sanitize_fts(query)
    clean = _py_sanitize_for_searchable(clean)
    if not clean.strip():
        return "", False
    clean = clean.strip()
    if mode == "fts5":
        toks = [t for t in clean.split() if t]
        if len(toks) > 1:
            return " OR ".join(toks), True
        return (f"{clean}*" if clean.isalnum() else clean), True
    if " " not in clean and clean.isalnum():
        return f"{clean}*", True
    return clean, True


def py_token_jaccard(a, b):
    ta = {t for t in _TOKEN_PUNCT_RE.sub(" ", a.lower()).split() if t}
    tb = {t for t in _TOKEN_PUNCT_RE.sub(" ", b.lower()).split() if t}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def py_token_jaccard_batch(query, candidates):
    return [py_token_jaccard(query, c) for c in candidates]


def py_cosine_packed(query, blob, dim):
    """Pure-Python unpack + cosine for one row, mirroring the numpy fallback."""
    v = struct.unpack(f"{dim}f", blob)
    dot = sum(q * x for q, x in zip(query, v))
    nq = sum(q * q for q in query) ** 0.5
    nv = sum(x * x for x in v) ** 0.5
    return dot / (nq * nv) if nq and nv else 0.0


def py_cosine_batch_packed(query, blobs, dim):
    return [py_cosine_packed(query, b, dim) for b in blobs]


def py_mmr_packed(relevance, blobs, dim, lambda_, k, force_seed_first):
    vecs = [list(struct.unpack(f"{dim}f", b)) for b in blobs]

    def cos(i, j):
        a, b = vecs[i], vecs[j]
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0

    n = len(vecs)
    k = min(k, n)
    if k <= 0:
        return []
    selected, remaining = [], list(range(n))
    if force_seed_first:
        selected.append(remaining.pop(0))
    while remaining and len(selected) < k:
        best_idx, best_mmr = 0, -float("inf")
        for ci, cand in enumerate(remaining):
            max_sim = max((cos(cand, s) for s in selected), default=0.0)
            mmr = lambda_ * relevance[cand] - (1 - lambda_) * max_sim
            if mmr > best_mmr:
                best_mmr, best_idx = mmr, ci
        selected.append(remaining.pop(best_idx))
    return selected


# --------------------------------------------------------------------------
# timing harness (same as bench_oxidation.py)
# --------------------------------------------------------------------------
def bench(fn, target_seconds=1.0, warmup=5, max_iters=2_000_000):
    for _ in range(warmup):
        fn()
    t0 = time.perf_counter()
    fn()
    one = max(time.perf_counter() - t0, 1e-7)
    n = max(1, min(max_iters, int(target_seconds / one)))
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1e6)
    samples.sort()
    median = statistics.median(samples)
    p95 = samples[min(len(samples) - 1, int(round(0.95 * (len(samples) - 1))))]
    return median, p95, n


RESULTS = []


def record(op, size, py_med, py_p95, rs_med, rs_p95, unit="us"):
    speedup = (py_med / rs_med) if rs_med > 0 else float("inf")
    RESULTS.append((op, size, py_med, py_p95, rs_med, rs_p95, speedup, unit))
    div = 1000.0 if unit == "ms" else 1.0
    verdict = "rust wins" if speedup >= 1.05 else ("PY wins" if speedup <= 0.95 else "break-even")
    print(f"  {op:<24} {size:<14} "
          f"py {py_med/div:9.3f}/{py_p95/div:9.3f}  "
          f"rs {rs_med/div:9.3f}/{rs_p95/div:9.3f} {unit}  "
          f"x{speedup:7.2f}  [{verdict}]")


# --------------------------------------------------------------------------
# vectors: real from DB, else synthetic (same source logic as bench_oxidation)
# --------------------------------------------------------------------------
def load_real_blobs(n_wanted):
    import sqlite3
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
        blobs = [b for (b,) in rows if b and len(b) == DIM * 4]
        return blobs if len(blobs) >= 2 else None
    except Exception as exc:  # noqa: BLE001
        print(f"  (DB read failed: {exc} -- synthetic)")
        return None


_real = load_real_blobs(5016)
if _real is not None:
    BLOB_POOL = _real
    VEC_SOURCE = f"REAL ({len(_real)} blobs from memory/agent_memory.db, dim={DIM})"
else:
    rng = np.random.default_rng(20260622)
    BLOB_POOL = [rng.standard_normal(DIM).astype(np.float32).tobytes() for _ in range(5016)]
    VEC_SOURCE = f"SYNTHETIC ({len(BLOB_POOL)} random float32 blobs, dim={DIM})"


def take_blobs(n):
    out = []
    while len(out) < n:
        out.extend(BLOB_POOL)
    return out[:n]


# realistic text corpus for FTS / jaccard
_WORDS = ("retrieval embedding cosine memory bitemporal sqlite vector index "
          "redaction oxidation jaccard hybrid ranking session temporal").split()


def make_query(seed, n):
    return " ".join(_WORDS[(seed + i) % len(_WORDS)] for i in range(n))


# ==========================================================================
# SANITY
# ==========================================================================
print("=" * 100)
print("SANITY CHECKS (Rust vs Python output agreement -- must pass before timing)")
print("=" * 100)
_sane = True

# Capability map: an installed wheel may be STALE and miss newer functions
# (the oxidation_probe doctor check reports this). Bench only what's present;
# print a SKIP line for the rest instead of crashing on AttributeError.
HAVE = {fn: hasattr(m3_core_rs, fn) for fn in (
    "sanitize_fts", "compile_fts_query", "token_jaccard",
    "token_jaccard_batch", "cosine_batch_packed", "mmr_rerank_scored_packed",
)}
_missing = [fn for fn, ok in HAVE.items() if not ok]
if _missing:
    print(f"  NOTE: installed wheel is missing {len(_missing)} function(s) "
          f"(STALE wheel) — skipping: {', '.join(_missing)}")
    print("        rebuild from m3-core-rs source to bench these.\n")

_q = "alpha OR beta AND (gamma) NEAR delta?!"
if HAVE["sanitize_fts"]:
    if m3_core_rs.sanitize_fts(_q, 500) == py_sanitize_fts(_q, 500):
        print(f"  sanitize_fts        OK  ('{m3_core_rs.sanitize_fts(_q, 500)}')")
    else:
        _sane = False
        print(f"  sanitize_fts        !!! MISMATCH rust='{m3_core_rs.sanitize_fts(_q,500)}' py='{py_sanitize_fts(_q,500)}'")

if HAVE["compile_fts_query"]:
    for _mode in ("fts5", "hybrid"):
        _rs = tuple(m3_core_rs.compile_fts_query(_q, _mode))
        _py = py_compile_fts_query(_q, _mode)
        if _rs == _py:
            print(f"  compile_fts_query   OK  mode={_mode}  {_rs}")
        else:
            _sane = False
            print(f"  compile_fts_query   !!! MISMATCH mode={_mode} rust={_rs} py={_py}")

_a, _b = make_query(1, 8), make_query(3, 8)
if HAVE["token_jaccard"]:
    if abs(m3_core_rs.token_jaccard(_a, _b) - py_token_jaccard(_a, _b)) < FLOAT_TOL:
        print(f"  token_jaccard       OK  (rust={m3_core_rs.token_jaccard(_a, _b):.4f})")
    else:
        _sane = False
        print(f"  token_jaccard       !!! MISMATCH rust={m3_core_rs.token_jaccard(_a,_b)} py={py_token_jaccard(_a,_b)}")

if HAVE["token_jaccard_batch"]:
    _cands = [make_query(i, 8) for i in range(32)]
    _rb = m3_core_rs.token_jaccard_batch(_a, _cands)
    _pb = py_token_jaccard_batch(_a, _cands)
    if len(_rb) == len(_pb) and all(abs(r - p) < FLOAT_TOL for r, p in zip(_rb, _pb)):
        print(f"  token_jaccard_batch OK  all {len(_rb)} agree")
    else:
        _sane = False
        print("  token_jaccard_batch !!! MISMATCH")

_query_vec = list(struct.unpack(f"{DIM}f", BLOB_POOL[0]))
if HAVE["cosine_batch_packed"]:
    _test_blobs = take_blobs(64)
    _rs_cbp = m3_core_rs.cosine_batch_packed(_query_vec, _test_blobs, DIM)
    _py_cbp = py_cosine_batch_packed(_query_vec, _test_blobs, DIM)
    if len(_rs_cbp) == len(_py_cbp) and all(abs(r - p) < 1e-3 for r, p in zip(_rs_cbp, _py_cbp)):
        print(f"  cosine_batch_packed OK  all {len(_rs_cbp)} agree within 1e-3")
    else:
        _sane = False
        print("  cosine_batch_packed !!! MISMATCH")

if HAVE["mmr_rerank_scored_packed"]:
    _mflat = b"".join(take_blobs(24))
    _mrel = [1.0 - i / 24 for i in range(24)]
    _rs_m = list(m3_core_rs.mmr_rerank_scored_packed(_mrel, _mflat, DIM, MMR_LAMBDA, 8, True))
    _py_m = py_mmr_packed(_mrel, take_blobs(24), DIM, MMR_LAMBDA, 8, True)
    if _rs_m == _py_m:
        print(f"  mmr_rerank_packed   OK  selection matches ({_rs_m})")
    else:
        _sane = False
        print(f"  mmr_rerank_packed   !!! MISMATCH rust={_rs_m} py={_py_m}")

if not _sane:
    print("\n*** WARNING: sanity FAILED -- timing numbers below are meaningless.\n")
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

# [1] sanitize_fts
if HAVE["sanitize_fts"]:
    print("[1] sanitize_fts  (Rust: sanitize_fts  |  Python: fts._sanitize_fts fallback body)")
    for label, qlen in [("short 6tok", 6), ("long 60tok", 60)]:
        q = make_query(7, qlen) + " OR foo? AND bar! (baz)"
        py_med, py_p95, _ = bench(lambda qq=q: py_sanitize_fts(qq, 500))
        rs_med, rs_p95, _ = bench(lambda qq=q: m3_core_rs.sanitize_fts(qq, 500))
        record("sanitize_fts", label, py_med, py_p95, rs_med, rs_p95)
    print()

# [2] compile_fts_query (uncached body — production wraps it in lru_cache)
if HAVE["compile_fts_query"]:
    print("[2] compile_fts_query  (Rust: compile_fts_query  |  Python: fts._compile_fts_query body, UNCACHED)")
    for mode in ("fts5", "hybrid"):
        q = make_query(2, 8) + " OR foo?"
        py_med, py_p95, _ = bench(lambda qq=q, m=mode: py_compile_fts_query(qq, m))
        rs_med, rs_p95, _ = bench(lambda qq=q, m=mode: m3_core_rs.compile_fts_query(qq, m))
        record("compile_fts_query", f"mode={mode}", py_med, py_p95, rs_med, rs_p95)
    print()

# [3] token_jaccard single
a, b = make_query(1, 12), make_query(4, 12)
if HAVE["token_jaccard"]:
    print("[3] token_jaccard  (Rust: token_jaccard  |  Python: entity._token_jaccard fallback body)")
    py_med, py_p95, _ = bench(lambda: py_token_jaccard(a, b))
    rs_med, rs_p95, _ = bench(lambda: m3_core_rs.token_jaccard(a, b))
    record("token_jaccard", "12tok", py_med, py_p95, rs_med, rs_p95)
    print()

# [4] token_jaccard_batch
if HAVE["token_jaccard_batch"]:
    print("[4] token_jaccard_batch  (Rust: token_jaccard_batch  |  Python: per-candidate loop)")
    for n in (50, 500):
        cands = [make_query(i, 12) for i in range(n)]
        py_med, py_p95, _ = bench(lambda c=cands: py_token_jaccard_batch(a, c))
        rs_med, rs_p95, _ = bench(lambda c=cands: m3_core_rs.token_jaccard_batch(a, c))
        unit = "ms" if py_med > 1000 or rs_med > 1000 else "us"
        record("token_jaccard_batch", f"cands={n}", py_med, py_p95, rs_med, rs_p95, unit)
    print()

# [5] cosine_batch_packed
qv = list(struct.unpack(f"{DIM}f", BLOB_POOL[0]))
if HAVE["cosine_batch_packed"]:
    print("[5] cosine_batch_packed  (Rust: cosine_batch_packed  |  Python: struct.unpack + cosine loop)")
    for n in (100, 1000, 5000):
        blobs = take_blobs(n)
        py_med, py_p95, _ = bench(lambda bl=blobs: py_cosine_batch_packed(qv, bl, DIM), target_seconds=0.5,
                                  max_iters=(200 if n >= 5000 else 2_000_000))
        rs_med, rs_p95, _ = bench(lambda bl=blobs: m3_core_rs.cosine_batch_packed(qv, bl, DIM))
        unit = "ms" if py_med > 1000 or rs_med > 1000 else "us"
        record("cosine_batch_packed", f"corpus={n}", py_med, py_p95, rs_med, rs_p95, unit)
    print()

# [6] mmr_rerank_scored_packed
if HAVE["mmr_rerank_scored_packed"]:
    print("[6] mmr_rerank_scored_packed  (Rust: mmr_rerank_scored_packed  |  Python: unpack + MMR loop)")
    for pool in (24, 150):
        k = pool // 3
        blobs = take_blobs(pool)
        flat = b"".join(blobs)
        relevance = [1.0 - i / pool for i in range(pool)]
        py_max = 50 if pool >= 150 else 500
        py_med, py_p95, _ = bench(
            lambda bl=blobs, r=relevance, kk=k: py_mmr_packed(r, bl, DIM, MMR_LAMBDA, kk, True),
            target_seconds=0.5, max_iters=py_max)
        rs_med, rs_p95, _ = bench(
            lambda f=flat, r=relevance, kk=k: m3_core_rs.mmr_rerank_scored_packed(r, f, DIM, MMR_LAMBDA, kk, True),
            target_seconds=0.5)
        unit = "ms" if py_med > 1000 or rs_med > 1000 else "us"
        record("mmr_rerank_scored_packed", f"pool={pool},k={k}", py_med, py_p95, rs_med, rs_p95, unit)
    print()

# ==========================================================================
# SUMMARY
# ==========================================================================
print("=" * 100)
print("SUMMARY  --  FTS + jaccard + packed-vector micro-benchmark, FFI-inclusive, NOT end-to-end")
print("=" * 100)
print(f"{'operation':<26}{'input size':<18}{'py median':>13}{'rs median':>13}{'speedup':>10}  verdict")
print("-" * 100)
for op, size, py_med, py_p95, rs_med, rs_p95, speedup, unit in RESULTS:
    div = 1000.0 if unit == "ms" else 1.0
    verdict = "rust faster" if speedup >= 1.05 else ("PYTHON faster" if speedup <= 0.95 else "break-even")
    print(f"{op:<26}{size:<18}{py_med/div:>10.3f}{unit:<3}{rs_med/div:>10.3f}{unit:<3}{speedup:>9.2f}x  {verdict}")
print("-" * 100)
print(f"vectors: {VEC_SOURCE}")
print(f"sanity checks: {'ALL PASSED' if _sane else 'FAILED -- numbers unreliable'}")
print()
print("NOTE: compile_fts_query is benched UNCACHED; production wraps it in @lru_cache(2048),")
print("so the per-unique-query FFI cost is paid once. rank_hybrid_packed is intentionally not")
print("benched (its Python baseline is the whole legacy ranking path, not an isolated function).")
