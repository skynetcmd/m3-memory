# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Performance

Measured latency for the paths you actually wait on. Every number here was
produced by a harness you can run yourself, and every one is qualified by the
machine it came from.

> **Read the hardware line before the numbers.** These were measured on a fast
> desktop with a GPU-accelerated embedder. They are a *reference point*, not a
> guarantee, and not a floor — a laptop, a small VPS, or a CPU-only install will
> be slower, in some cases by a lot. Where a number is hardware-independent, it
> says so.

## The measurement host

| | |
|---|---|
| CPU | AMD Ryzen 7 9800X3D — 8C/8T, 4.7 GHz |
| GPU | NVIDIA RTX 5080 — the embedder runs the **CUDA** build |
| RAM | 31 GB (8.8 GB free at measurement time) |
| OS / Python | Windows 11 · Python 3.14.6 |
| `m3_core_rs` | 3.7.31 (native wheel present) |
| Machine state | **Stock Windows install**, no tuning — 398 processes, editor, browser and a WSL VM all running |
| m3 state | **nothing hitting m3 concurrently** — no second agent, no loop pass, no sync |

This is consumer hardware — a stock Windows machine with no performance tuning,
no tweaked power plan, running everything a working desktop runs. But it is
*fast* consumer hardware: a current-generation X3D desktop part with a pro-sumer
GPU. No other process was contending for m3's own paths, so nothing here is
distorted by concurrent m3 work.

**The single biggest variable is whether you have a GPU** — and we measured both
sides of that, below.

## Writes: the deferred path is 14.5× faster

| Path | p50 | p95 |
|---|---|---|
| write, embedding inline | 31.40 ms | 33.39 ms |
| write, embedding deferred | **2.16 ms** | 3.66 ms |

The whole gap is the embed call. A write that does not embed — validate,
contradiction-check, hash, store — is about **2 ms**.

This is what the zero-lag design buys, and it is an architectural result rather
than a hardware one: when no fast embedder is reachable, m3 persists the row
verbatim and defers the vector to the [Cognitive
Loop](ARCHITECTURE.md#-the-cognitive-loop) instead of making the caller wait.
The row is full-text searchable immediately either way, and vector search picks
it up once the `embed` pass has run.

Single-threaded, that is roughly **32 writes/second** with inline embedding and
**460/second** deferred.

## Sync: `bulk_upsert` is 24.6× faster than row-at-a-time

3,000-row upsert to a live PostgreSQL warehouse, median of 10 runs:

| | p50 | p95 |
|---|---|---|
| `bulk_upsert` (`execute_values`) | **25.0 ms** | 28.4 ms |
| `cursor.executemany` | 614.5 ms | 670.8 ms |

**This one is largely hardware-independent** — it is a round-trip count, not a
CPU speed. psycopg2's `executemany` issues the statement once per row, so 3,000
rows is 3,000 round trips; `execute_values` collapses each 1,000-row page into a
single statement. On a WAN link to a remote warehouse the gap *widens*, because
it multiplies by network latency.

It is also why `bulk_upsert` exists as a
[storage-seam primitive](ARCHITECTURE.md#-extension-seams) rather than each
backend improvising: a correct-but-row-at-a-time port would have been 25× slower
and still passed every correctness test.

## Search

| Stage | p50 | p95 |
|---|---|---|
| hybrid k=10 — FTS5+BM25, vector, fuse, MMR, ranking | 45.67 ms | 48.56 ms |
| cross-encoder rerank (opt-in), added to the above | 3.71 ms | 4.61 ms |

⚠ **Measured against a ~200-row store.** `SEARCH_ROW_CAP` defaults to **5,000**,
and cost at the cap is *not* measured here — expect it to be higher. Treat 46 ms
as "what a small store does", not as a scaling claim.

One number worth explaining: the fastest observed query was **0.80 ms**, ~50×
faster than the median. That is the FTS short-circuit — when a query has an
exact lexical match, m3 skips the vector side entirely. It is real, but it
describes a specific case, not the general one.

## Embedding, and the shared-vs-in-process question

m3's default is a **shared embed server** on `127.0.0.1:8082` — one model in RAM
for every m3 process. **In-process** embedding (llama.cpp linked into the calling
process, no IPC) is opt-in and loads its own copy per process.

Embedding a 300 KB corpus, 698 paragraphs, one at a time through each topology:

| Paragraph size | n | shared | in-process |
|---|---|---|---|
| small (<200 chars) | 284 | 30.1 ms | **16.0 ms** |
| medium (200–2k) | 395 | 32.2 ms | **19.0 ms** |
| large (>2k) | 19 | **45.3 ms** | 67.2 ms |
| whole corpus (wall clock) | 698 | 22.6 s | **19.5 s** |

**In-process is faster on real text** — about 1.9× on short chunks and 1.75× on
medium ones, which together are 679 of the 698 chunks measured. Only the largest
chunks favour the shared server.

### Without a GPU, expect ~7× slower — and the choice above stops mattering

The same corpus with the GPU taken out of play:

| | shared (GPU) | in-process (GPU) | shared (CPU) | in-process (CPU) |
|---|---|---|---|---|
| small (n=284) | 30.1 ms | **16.0 ms** | 68.1 ms | 57.6 ms |
| medium (n=395) | 32.2 ms | **19.0 ms** | 186.2 ms | 177.5 ms |
| large (n=19) | 45.3 ms | 67.2 ms | 1,464.6 ms | 1,437.6 ms |
| whole corpus | 22.6 s | **19.5 s** | 157.9 s | 152.5 s |

**The GPU is worth about 7× on this corpus**, whichever topology you choose.
That dwarfs everything else on this page, and it is the number to plan around:
if you are sizing a CPU-only box, use the right-hand columns, not the headline
ones.

**On CPU the two topologies converge** — 157.9 s vs 152.5 s, a 3% difference.
With the GPU gone, the model's forward pass dominates so completely that the
IPC hop stops being measurable. So the shared-vs-in-process choice above is a
GPU-only consideration; on CPU, pick shared and save the RAM.

Ranked by what actually moves embed latency here:

1. **GPU vs CPU** — ~7×
2. **Chunk size** — ~25× between short and long paragraphs in one configuration
3. **Shared vs in-process** — ~1.2× on GPU, ~1.03× on CPU

Two honest caveats on the tier table specifically:

- **It rests on one machine**, with a CUDA embedder. This is the most
  hardware-sensitive result on this page.
- **The 19-sample row is thin.** An earlier run of the same comparison, with only
  5 large samples, showed in-process losing 6–8× and reversed the overall
  verdict. Widening the corpus corrected it. Read that row with less confidence
  than the others.

**So why is shared still the default?** Because latency is not the trade — memory
is. In-process loads one model copy *per process*, and a typical setup runs
several (MCP server, cognitive loop, CLI). Shared keeps one model in RAM for all
of them. Choose in-process when you have RAM to spare and a short-text workload;
choose shared — the default — otherwise.

## The Rust core

Separately from the above, m3 ships a Rust compute core (`m3_core_rs`) that
accelerates MMR re-ranking, batch cosine, and FTS compilation by **90×–800×**.
It is installed by default, and the pure-Python fallback is results-equivalent —
it changes speed, never answers. See
[Oxidation Benchmarks](OXIDATION_BENCHMARKS.md).

## Reproducing this

The harness is not shipped in the public repo (it captures host-specific
timings, which is not something to publish as a product claim). The method is:

- `time.perf_counter`, untimed warm-up, then N timed iterations
- **p50 and p95, never a mean** — a mean hides the slow tail, which is the part
  that matters on a write path
- a throwaway store, so no real memory is touched
- each measurement reports its own `n`

Three traps worth knowing if you build your own, all of which produced a
confident wrong answer here first:

1. **m3's write and search entry points are coroutines.** Timing the call
   without awaiting it measures coroutine *construction*: 40 "writes" at
   p50 0.00 ms, an excellent number for code that never ran.
2. **`M3_EMBED_INPROC=1` does not by itself enable in-process embedding.** A
   present `.embed_config.json` takes precedence. A probe that ignores this
   measures the shared server twice and reports the two topologies as identical.
3. **Small buckets lie.** The 5-sample result above was reproducible and wrong.

---

*Retrieval accuracy — the metric that measures the memory layer itself — is in
[Benchmarks](../README.md#-benchmarks).*
