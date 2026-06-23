# Oxidation Benchmarks — Rust `m3_core_rs` vs Python

Per-operation micro-benchmark of the hot paths moved from Python to the in-process
Rust extension (`m3_core_rs`). This is the **only** Rust-vs-Python comparison in the
project with committed results; reproduce it with:

```
python tests/bench_oxidation.py
```

## What this measures (and what it does not)

- **FFI-inclusive:** every Rust call crosses the Python↔Rust boundary exactly as
  production `memory_core.py` does. The marshalling cost of Python lists into Rust
  **is** part of the measured cost — this is not the Rust crate benched in isolation.
- **Output-verified:** for sha256 and cosine the harness asserts Rust and Python
  produce identical output (exact / within 1e-5) *before* timing. All sanity checks
  passed on this run.
- **Per-operation, NOT end-to-end.** This answers "for this one operation, at this
  input size, is Rust faster than Python, and by how much?" It is **not** an
  end-to-end retrieval benchmark and must not be read as an end-to-end speedup claim.

## Run context

| | |
|---|---|
| Date | 2026-06-22 |
| Python | 3.14.3 |
| Platform | Windows 11 (10.0.26200), AMD64 (AMD Ryzen-class) |
| `m3_core_rs` embed backend | cuda (GPU-built wheel active) |
| Vectors | **REAL** — 5016 vectors (dim=1024) from `memory/agent_memory.db` |
| Timing | `time.perf_counter`; per-op median/p95 over N iters auto-sized to ≥~1s |

## Results

Speedup = Python median ÷ Rust median. >1 means Rust is faster.

| Operation | Input size | Python median | Rust median | Speedup | Verdict |
|---|---|---:|---:|---:|---|
| `mmr_rerank` | pool=150, k=50 | 3956.693 ms | 15.353 ms | **257.71×** | rust faster |
| `mmr_rerank` | pool=24, k=8 | 14.416 ms | 0.151 ms | **95.59×** | rust faster |
| `redaction` | clean ~1110 ch | 45.300 µs | 3.000 µs | **15.10×** | rust faster |
| `redaction` | dirty ~1073 ch | 45.200 µs | 4.000 µs | **11.30×** | rust faster |
| `cosine` | 1024-dim | 26.600 µs | 8.300 µs | 3.20× | rust faster |
| `cosine_batch` | corpus=1000 | 13.816 ms | 4.636 ms | 2.98× | rust faster |
| `cosine_batch` | corpus=5000 | 71.918 ms | 27.383 ms | 2.63× | rust faster |
| `cosine_batch` | corpus=100 | 1.271 ms | 0.504 ms | 2.52× | rust faster |
| `displacement_guard` | rows=10 | 0.700 µs | 0.400 µs | 1.75× | rust faster |
| `sha256_hex` | large 32 KB | 13.700 µs | 14.000 µs | 0.98× | break-even |
| `sha256_hex` | medium 2 KB | 1.800 µs | 2.100 µs | 0.86× | **Python faster** |
| `sha256_hex` | small 100 B | 1.000 µs | 1.300 µs | 0.77× | **Python faster** |
| `displacement_guard` | rows=100 | 0.800 µs | 1.300 µs | 0.62× | **Python faster** |

## Honest reading

- **The big wins are real and concentrated.** MMR rerank is the standout: at a
  realistic candidate pool (150) the Python path is O(n²) cosine calls and takes
  ~4 **seconds**; Rust does it in ~15 ms — a **257×** win on a genuine retrieval
  hot path. Redaction (11–15×) and vector cosine (2.5–3.2×) are solid, repeatable
  wins on operations that run on every write / every search.
- **Rust is not universally faster, and we do not claim it is.** For tiny,
  C-backed operations the FFI boundary costs more than it saves: `sha256_hex`
  (hashlib is already C) is break-even-to-slower, and `displacement_guard` on
  100 rows favors Python. This is exactly why `sha256` was reverted to the
  Python `hashlib` path in production (see `docs/OXIDATION_TODO.md`).
- **No single headline multiplier is honest.** A "85×" or "250×" summary
  cherry-picks MMR and hides the operations where Rust loses. The defensible
  statement is the **range with per-operation context** above: large wins on
  MMR / redaction / cosine, break-even-or-worse on trivially small C-backed ops.

These are FFI-inclusive micro-benchmarks on one machine; treat them as
order-of-magnitude guidance for *which* operations benefit from oxidation, not as
a guaranteed end-to-end speedup.

## Packed-vector variants (`cosine_batch_packed`, `mmr_rerank_scored_packed`)

The production search path passes raw embedding **bytes** straight from SQLite
into the Rust core (`page_blobs` → `rank_hybrid_packed` / `cosine_batch_packed`),
so the Python baseline for these must include the per-row `struct.unpack` that the
Rust path does zero-copy. That makes the packed wins larger than the list-based
cosine/MMR above — this is the realistic production comparison, not a
list-of-floats convenience API. Reproduce with `python tests/bench_oxidation_fts_packed.py`.

Output-verified (Rust == Python), real DB vectors (5016 × dim-1024), measured on
the freshly built CUDA wheel (8/8 native paths), 2026-06-22:

| Operation | Input size | Python median | Rust median | Speedup | Verdict |
|---|---|---:|---:|---:|---|
| `mmr_rerank_scored_packed` | pool=150, k=50 | 10446.155 ms | 12.348 ms | **846.01×** | rust faster |
| `mmr_rerank_scored_packed` | pool=24, k=8 | 38.168 ms | 0.054 ms | **701.61×** | rust faster |
| `cosine_batch_packed` | corpus=100 | 7.676 ms | 0.043 ms | **177.68×** | rust faster |
| `cosine_batch_packed` | corpus=1000 | 76.076 ms | 0.656 ms | 115.99× | rust faster |
| `cosine_batch_packed` | corpus=5000 | 391.826 ms | 4.045 ms | 96.87× | rust faster |

The packed MMR at pool=150 is the most extreme honest number in the suite: the
pure-Python path (unpack 150 blobs, then O(n²) cosine) takes **~10.4 seconds**;
Rust does it in ~12 ms — ~846×. This is the path production actually takes when
reranking a full candidate page, which is why it is wired to Rust by default.

## FTS + lexical-overlap functions

The FTS query helpers and token-Jaccard scorers. These are small string
operations, so the FFI boundary is a larger fraction of the cost and the wins are
more modest than the vector paths — but all are net-positive. `compile_fts_query`
is benched **uncached**; production wraps it in `@lru_cache(2048)`, so the FFI
crossing is paid once per unique `(query, mode)`. Output-verified, 2026-06-22:

| Operation | Input size | Python median | Rust median | Speedup | Verdict |
|---|---|---:|---:|---:|---|
| `token_jaccard_batch` | cands=500 | 1.539 ms | 0.146 ms | **10.58×** | rust faster |
| `token_jaccard_batch` | cands=50 | 153.500 µs | 20.400 µs | 7.52× | rust faster |
| `sanitize_fts` | long 60tok | 7.800 µs | 1.300 µs | 6.00× | rust faster |
| `sanitize_fts` | short 6tok | 1.900 µs | 0.600 µs | 3.17× | rust faster |
| `compile_fts_query` | mode=hybrid | 2.100 µs | 0.700 µs | 3.00× | rust faster |
| `compile_fts_query` | mode=fts5 | 2.400 µs | 0.900 µs | 2.67× | rust faster |
| `token_jaccard` | single, 12tok | 3.100 µs | 2.200 µs | 1.41× | rust faster |

## `rank_hybrid_packed` — intentionally not benched

`rank_hybrid_packed` is present in the wheel but is **not** in this suite: it has
no isolated Python baseline to compare against. Its fallback is the whole legacy
ranking path in `memory/search.py` (sort + MMR + temporal/recency boosts
intertwined with row-dict assembly), so a faithful like-for-like micro-benchmark
would mean reconstructing that path. It is omitted rather than benched against a
non-equivalent stub.

## Wheel-staleness note (resolved 2026-06-22)

When this suite was first authored, the installed wheel was **stale** — the
`oxidation_probe` doctor check reported only **3/8 native paths present**, so
`sanitize_fts`, `compile_fts_query`, `token_jaccard`, `token_jaccard_batch`, and
`rank_hybrid_packed` were silently running the Python fallback in production. The
wheel was rebuilt from current `m3-core-rs` source (Windows + CUDA, crate 3.6.6)
and reinstalled; the probe now reports **8/8 native paths present (current)** and
the FTS/jaccard numbers above are from that fresh wheel. If `oxidation_probe`
ever reports STALE again, rebuild via `crates/m3-core-py/build_wheel.py
--backend <cpu|cuda|vulkan> --os <…> --release` and re-run this benchmark.
