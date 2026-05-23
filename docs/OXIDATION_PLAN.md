# Project "Oxidation": M3-Core Rust Transition Plan

> **Strategic Objective:** Transition the performance-critical "Hot Loops" of m3-memory into a Rust-backed core to eliminate Python-bound bottlenecks, enable true concurrency, and achieve sub-100ms retrieval latency at LME-M scale.

## 1. Architectural Vision
Transform m3-memory into a **Tiered Hybrid System** leveraging **PyO3** for zero-cost abstraction between research flexibility and production performance.

*   **Layer 1: Research Surface (Python):** LLM prompts, CLI, Benchmark harnesses, and high-level heuristics.
*   **Layer 2: Bridge (PyO3):** Hardened FFI layer with type-safe error propagation.
*   **Layer 3: Compute Core (Rust):** Multi-threaded engine for SQLite (WAL-optimized), SIMD Vector Math, and Graph Traversal.

## 2. Phase 1: The Foundation (Build & Infrastructure)
**Goal:** Establish the hybrid environment without disrupting the Python ecosystem.

1.  **Crate Scaffolding:** Initialize m3-core-rs using maturin. Support pip install . for seamless integration.
2.  **Hardened Error Propagation:** Implement M3Error in Rust with direct mapping to Python exceptions (e.g., VectorDimMismatch, DatabaseLocked).
3.  **FIPS Shadowing:** Re-implement SHA-256 content hashing using the ing library to ensure 100% parity with M3's FIPS mandates.
4.  **Logging Integration:** Bridge env_logger to Python's logging module for unified telemetry.

## 3. Phase 2: The "Vector Nut" (Numerical Core)
**Goal:** Eliminate Python's per-row data conversion overhead.

1.  **Zero-Copy Memory Mapping:** Replace struct.unpack with ytemuck for direct SQLite BLOB-to-slice casting.
2.  **SIMD Cosine Similarity:** Implement cosine similarity using 
darray and ayon.
    *   *Efficiency Gain:* Theoretical move from 5ms (Python/Numpy) to 10μs (Rust/SIMD) per 100-vector set.
3.  **MMR Reranker (Rust):** Move the Maximal Marginal Relevance loop to Rust. Process 100+ candidates in a single thread-safe operation.
4.  **Verification:** Add proptest suites to ensure Rust output matches Python baseline to 7 decimal places.

## 4. Phase 3: High-Throughput Ingest (A2 Ingest Killer)
**Goal:** Saturate the RTX 5080 and NVMe bandwidth.

1.  **Tokio Async Runtime:** Manage simultaneous embedding requests via the Phase 3b `EmbedDispatcher` (see §4a) rather than raw HTTP fan-out. Stream count is configurable, default = llama.cpp `-np`.
2.  **Pipelined Ingest Pipeline:**
    *   **Stage A:** Producer thread reads source data and computes FIPS hashes.
    *   **Stage B:** `EmbedDispatcher::embed_stream` consumes the producer channel and emits embedded results downstream — continuous batching + length bucketing handled internally.
    *   **Stage C:** Consumer thread batches writes to SQLite using optimized WAL pragmas.
3.  **Target Throughput:** 2,000+ turns/sec (eliminating the ~190 turns/sec Python bottleneck). The throughput win comes from continuous batching of llama.cpp's existing kernels, not from replacing them.

## 4a. Phase 3b: Embed Dispatcher (Rust-in-front-of-llama.cpp)
**Goal:** Saturate llama.cpp's parallel slots without replacing its kernels. The model math stays in llama.cpp (CUDA / ROCm / Metal / Vulkan / SYCL / CPU-NEON / CPU-AVX); Rust owns scheduling, batching, and backpressure.

**Why not a from-scratch Rust embedder:** llama.cpp's kernels are years ahead of `candle`/`burn`. Replacing them costs years and regresses hardware support (loses ROCm/Vulkan/SYCL). The realistic win is **better utilization** of the kernels we already have — continuous batching, length-bucketing, and dropping HTTP/JSON serialization per call.

### 4a.1 llama.cpp Server Config
Run `llama-server` (or embed via `llama-cpp-rs` for zero IPC) with:

```
llama-server \
  --model models/bge-m3-q8_0.gguf \
  --embeddings \
  --pooling mean \
  -np 8                  # parallel slots — tune per GPU VRAM
  -cb                    # continuous batching
  --batch-size 2048      # logical batch (tokens across slots)
  --ubatch-size 512      # physical batch (per forward pass)
  -c 8192                # context per slot
  --threads-http 4
  --host 127.0.0.1 --port 8081
```

Per-target slot-count starting points (tune empirically):
*   RTX 5080 (16GB), bge-m3 Q8_0: `-np 8`, `--ubatch 512`
*   M-series (32GB unified): `-np 4`, `--ubatch 256` (Metal prefers smaller ubatches)
*   CPU-only (16-core x86): `-np 2`, `--ubatch 128`

### 4a.2 Dispatcher API (Rust → Python via PyO3)

```rust
// m3-core-rs/src/embed/dispatcher.rs
pub struct EmbedDispatcher {
    streams: usize,           // configurable, default = llama.cpp -np
    coalesce_window: Duration, // 2–5ms typical
    max_batch_tokens: usize,  // matches --batch-size
    queues: Vec<LengthBucketQueue>, // bucketed by token count
}

impl EmbedDispatcher {
    pub fn new(cfg: DispatcherConfig) -> Result<Self, M3Error>;

    // Single-shot — joins the next coalescing window.
    pub async fn embed(&self, text: &str) -> Result<Vec<f32>, M3Error>;

    // Bulk — caller already has a batch; bypasses coalescing.
    pub async fn embed_batch(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>, M3Error>;

    // Streaming — for ingest pipelines; backpressure via bounded channel.
    pub fn embed_stream(
        &self,
        rx: mpsc::Receiver<EmbedJob>,
        tx: mpsc::Sender<EmbedResult>,
    );

    pub fn stats(&self) -> DispatcherStats; // in-flight, queue depth, p50/p99
}

pub struct DispatcherConfig {
    pub streams: usize,                // = llama.cpp -np
    pub coalesce_window_ms: u64,       // default 3
    pub max_batch_tokens: usize,       // default 2048
    pub length_buckets: Vec<usize>,    // e.g. [64, 256, 1024, 4096]
    pub backend: Backend,              // Http { url } | Embedded { model_path }
    pub circuit_breaker: BreakerCfg,
}
```

Python surface (PyO3):
```python
from m3_core_rs import EmbedDispatcher

disp = EmbedDispatcher(streams=8, coalesce_ms=3, backend_url="http://127.0.0.1:8081")
vec = await disp.embed("hello")            # joins next batch window
vecs = await disp.embed_batch(texts)        # caller-supplied batch
disp.stats()                                # { in_flight, p50_ms, p99_ms, ... }
```

### 4a.3 Scheduler Behavior
1.  **Length bucketing:** Each incoming request is tokenized (or token-length estimated) and dropped into the nearest bucket. Padding waste drops from ~40% to <5%.
2.  **Coalescing window:** Each bucket has a 2–5ms timer. On expiry OR when `max_batch_tokens` is reached, the bucket flushes one batch to llama.cpp.
3.  **Slot affinity:** Dispatcher maintains `streams` in-flight requests max. New batches queue until a slot frees.
4.  **Backpressure:** Bounded channel (default cap = 4 × streams). Ingest pipelines see `EmbedJob` rejected rather than unbounded memory growth.
5.  **Circuit breaker:** N consecutive failures → fast-fail for T seconds, then half-open probe. Protects against llama.cpp crashes or OOM.

### 4a.4 Two Backends, Same API
*   **`Backend::Http`** — POST to `llama-server`'s `/embedding`. Simpler ops; lets users swap llama.cpp for any OpenAI-compatible embed server. ~0.5–1ms overhead per batch.
*   **`Backend::Embedded`** — link `llama-cpp-rs` and call `llama_decode` directly. Zero IPC, zero JSON. ~50–200μs overhead per batch. Default for production; HTTP backend stays for dev/debug.

### 4a.5 Target Improvements (vs. current Python/HTTP client)
*   **Throughput:** 3–8× on the same hardware + same model, driven by continuous batching + length bucketing.
*   **p99 latency:** Down ~30%, mostly from killing per-call HTTP/JSON.
*   **Hardware breadth:** Unchanged from llama.cpp itself (CUDA, ROCm, Metal, Vulkan, SYCL, CPU). **No regression** vs. a from-scratch Rust embedder.

### 4a.6 What This Phase Is *Not*
*   Not a new embedder. The model math stays in llama.cpp.
*   Not a GPU-portability project. Portability is inherited from llama.cpp.
*   Not a quantization project. GGUF stays the format.

## 4b. Phase 3c: GLiNER NER Acceleration (ONNX Runtime + Dispatcher Reuse)
**Goal:** Cut GLiNER per-call latency and bump enrichment throughput by 5–15× without rewriting the model. Reuses the Phase 3b dispatcher abstraction — only the backend differs.

**Why not pure-Rust GLiNER:** `candle` could host it but loses DirectML / ROCm / OpenVINO / CoreML coverage. ONNX Runtime (`ort` crate) is the encoder-model equivalent of llama.cpp's role for decoders: mature, multi-backend, already tuned. Wrap, don't rewrite.

### 4b.1 ONNX Export
*   One-time export of the active GLiNER checkpoint to ONNX (opset 17+). Custom span-scoring head may need op patching — budget 1–2 days the first time.
*   Pin the export script to the model version; re-export gated on model upgrades.
*   Store exported model under `models/gliner/<version>.onnx` with a checksum manifest.

### 4b.2 `ort` Backend
Implement `OrtNer` against the shared `ModelBackend` trait introduced in Phase 3b:

```rust
trait ModelBackend {
    async fn run(&self, batch: Batch) -> Result<BatchOutput, M3Error>;
}

struct OrtNer {
    session: ort::Session,
    tokenizer: tokenizers::Tokenizer, // HF Rust tokenizer, direct
    labels: Vec<String>,
    threshold: f32,
}

impl ModelBackend for OrtNer { /* ... */ }
```

Execution providers selected at init time, in priority order per platform:

| Platform | EP priority |
|---|---|
| Linux + NVIDIA | CUDA → TensorRT → CPU |
| Linux + AMD | ROCm → CPU |
| Linux + Intel GPU | OpenVINO → CPU |
| Windows (any GPU) | DirectML → CPU |
| macOS Apple Silicon | CoreML → CPU |
| CPU-only (any OS) | CPU (AVX-512/VNNI on x86, NEON on ARM) |

### 4b.3 Dispatcher Reuse
Same `EmbedDispatcher` machinery from §4a, parameterized by backend:
*   Length bucketing on token count (GLiNER's padding waste is worse than the embedder's — bigger win here).
*   Coalescing window 2–5ms.
*   Per-turn calls from `extract_pending` / `enrich_pending` join the next batch; ingest paths use `embed_stream`-equivalent `ner_stream`.

### 4b.4 INT8 Quantization (Gated)
*   Quantize with ORT's dynamic quantization for CPU targets; static (calibration-based) for GPU.
*   **Accuracy gate:** existing enrichment regression suite must pass within ±0.5% F1 per entity type before quantized model promotes. GLiNER's threshold-sensitive span scoring means small drops can break specific extractors invisibly.
*   Ship FP32 and INT8 variants; user/config selects. Default INT8 on CPU, FP16 on GPU.

### 4b.5 Span Decode in Rust
Move the span-score → entity-list loop out of Python:
*   `ndarray` + `rayon` over the `[batch, max_spans, num_labels]` tensor.
*   Threshold filter, overlap resolution, and dedup all in Rust.
*   Expected: span decode drops from ~1–3ms/turn to <100μs/turn.

### 4b.6 Target Improvements (vs. current PyTorch + Python pipeline)
*   **Ingest throughput (enrichment path):** 5–15× depending on hardware (most of the win is batch coalescing; ORT + INT8 stack on top).
*   **Single-call p50:** 2–4× lower.
*   **Hardware breadth:** Strictly broader than today (gains DirectML, OpenVINO, CoreML; keeps CUDA, ROCm).

### 4b.7 What This Phase Is *Not*
*   Not a new NER model. The model stays GLiNER.
*   Not a label-schema change. Existing entity types and thresholds preserved.
*   Not a quantization-only project. Quantization is one stacked optimization; coalescing + Rust decode are independent wins.

## 4c. Phase 3d: Ingest Hot-Path Hardening (Redaction + Hashing + Decay + Rank-Fusion + Auto-Route)
**Goal:** Move the per-turn ingest hot path and the per-query critical path (route decision + rank-fusion merge) into Rust. These are the highest-value Tier-1 targets outside the embed/NER work and share the same caller surface (chatlog ingest + memory_search), the same parity-test discipline (byte-exact or behavior-identical vs. Python baseline), and the same risk profile (small, well-scoped modules).

### 4c.1 Redaction (port of `bin/chatlog_redaction.py`)
*   Multi-pattern secret scrubbing runs on every captured turn; Python regex dominates the per-turn cost.
*   **Rust implementation:** `regex` crate for general patterns, `aho-corasick` for the fixed-pattern set (API keys, known token prefixes). Expected 10–50× vs Python.
*   **Parity gate:** byte-exact output match on a redaction corpus (existing chatlog regression set). New patterns added in Python first, then ported once parity holds.
*   **Surface:** one PyO3 function `redact(text: &str, profile: &RedactionProfile) -> RedactionResult` with the same return shape Python expects.

### 4c.2 Content Hashing — FIPS-Preserving
**Non-negotiable constraint:** the FIPS-ready path must remain intact. Phase 1.3 already commits to FIPS parity for SHA-256; this section widens the scope to *callers* without weakening the crypto path.

*   **Crypto provider abstraction:** keep a single `M3Hasher` trait in Rust. Two implementations:
    *   `RingHasher` (default) — `ring` crate, FIPS-validatable when built against a FIPS module. Used wherever the existing Python `hashlib` path runs.
    *   `RustCryptoHasher` (optional, dev-only) — `sha2` crate with hardware SHA-NI. **Not on the FIPS path.** Available only behind a feature flag and explicitly disabled in production builds.
*   **FIPS guardrails:**
    *   The default build links only `ring`. `sha2` is gated behind a `--features non-fips-perf` Cargo flag that production wheels never set.
    *   CI gate: `cargo tree --no-default-features` must show zero non-FIPS crypto crates in the default feature set.
    *   Runtime assertion at module load: `M3Hasher::active_provider()` is logged and surfaced in `m3:health`. Any drift from `ring` in a production build fails the health check.
    *   Existing `test_fips_integrity.py` regression suite extended to cover the Rust path; the test must pass against the default build.
*   **Callers consolidated onto the Rust hasher:** live ingest (chatlog_core, memory_core write path), `backfill_content_hash.py`, `embed_backfill.py`. All routed through the same `M3Hasher::sha256()` entry point.
*   **Performance expectation:** `ring`'s SHA-256 uses SHA-NI / ARMv8 crypto extensions when available — 3–6× faster than Python `hashlib` on x86 and M-series **at large input sizes (≥64 KB)**, with **no FIPS compromise**. The `sha2` path (5–10×) is opt-in only for non-FIPS development scenarios.

    **Measured baseline (2026-05-17, x86 RTX 5080 host, single-call hot loop):** at 1 KB inputs — the realistic median for memory bodies and chatlog turns — `m3_core_rs.sha256_hex_bytes` via PyO3 is ~2× **slower** than Python `hashlib.sha256().hexdigest()` (163 ms vs 77 ms for 100 k hashes). PyO3 FFI overhead per call exceeds SHA-256 compute time at that size; the crossover where ring wins is around several-KB inputs. Implication for Phase 3d wiring: route ingest and `backfill_content_hash.py` through Rust as planned (large-batch flows pay the FFI cost once via a batched-hash entry point or amortise it via long blobs), but do **not** swap `bin/memory/embed.py:_content_hash` to the per-call Rust path — current Python wins at typical memory-item sizes. Audit citation: commit `c469288` skipped that swap; full reasoning in m3-memory `bin/memory/embed.py` commit message.

### 4c.3 Deterministic Ephemeral-Content Decay (port of `bin/chatlog_decay.py`)
*   Periodic sweep across chatlog rows applying decay scoring; tight numeric loop, well-defined inputs.
*   **Rust implementation:** stream rows via a single SQL cursor, score in Rust (`rayon` for parallel scoring across row chunks), batch UPDATEs back through a prepared statement. Pairs with the Phase 2 `bytemuck` BLOB work for any embedding-aware decay signal.
*   **Determinism gate:** Rust output must match Python output row-for-row on a frozen test DB. Determinism is a stronger guarantee than parity-to-7-decimals; chatlog decay drives retention and cannot drift.

### 4c.4 Hybrid Rank-Fusion Merge (extends Phase 2 MMR work)
*   `memory_core.py` currently merges FTS5 (BM25) results with vector results in Python: score normalization → weighted blend → dedup → MMR feed prep. Per-query loop over result rows.
*   **Rust implementation:** consume the two result sets as `Vec<RankRow>`, perform score normalization and fusion in a single pass, hand the deduped candidate set directly to the Phase 2 Rust MMR reranker. Eliminates one Python ↔ Rust ↔ Python round-trip per query.
*   FTS5 itself stays in SQLite (already C — no win to be had there).
*   **Parity gate:** identical top-K ordering on a frozen query set; ranking is user-visible and cannot drift silently.

### 4c.5 Multi-Signal Route Decider (port of `bin/auto_route.py`)
*   Sits in front of every `memory_search` call; feature extraction + multi-signal scoring per query. Latency adds directly to p50/p99.
*   **Rust implementation:** `decide_route(query: &str, signals: &RouteSignals) -> RouteDecision` returning `(branch, confidence, signal_breakdown)`. Feature extraction (token counts, entity hints, recency cues, intent markers) and the scoring function move together — splitting them would re-introduce the Python ↔ Rust round-trip.
*   **Behavior-parity gate (stronger than byte-exact):**
    *   **Branch identity:** On a frozen 10k-query regression corpus (built from chatlog history + LME-S queries), the Rust path must select the *identical* branch for ≥99.9% of queries. Disagreements logged with full signal breakdown.
    *   **Confidence numerical parity:** Confidence scores match Python to 7 decimal places (same gate as Phase 2.4).
    *   **Tie-break determinism:** Where multiple branches score equally, the tie-break rule (currently lexicographic on branch name) is preserved exactly. Tie-break drift is silent and user-visible — explicit test required.
    *   **Shadow mode before cutover:** Rust runs alongside Python for one release; disagreements counted in telemetry. Promotion to authoritative gated on disagreement rate <0.1%.
*   **Why this is the riskiest §4c port:** Unlike redaction/hashing/decay/rank-fusion (which are byte-exact or row-for-row deterministic), auto_route is *behavior-changing* — a wrong branch sends the query down a different retrieval pipeline entirely. The shadow-mode gate is non-negotiable for this one.

### 4c.6 Target Improvements
*   **Per-turn ingest latency:** 3–8× lower (redaction + hashing dominate today's cost).
*   **Decay sweep wall time:** 5–10× lower on chatlog DBs >100k rows.
*   **memory_search p50:** ~15–25% lower from removing the Python merge stage; **additional ~5–10%** from auto_route in Rust (estimate; confirmed only after shadow-mode telemetry).
*   **FIPS posture:** Unchanged. Same `ring`-based provider, same validation surface, additional runtime assertions making drift detectable.

### 4c.7 What This Phase Is *Not*
*   Not a redaction-policy change. Pattern set and profiles preserved.
*   Not a FIPS exception. The non-FIPS `sha2` path is dev-only, feature-gated, and CI-blocked from production builds.
*   Not a ranking-algorithm change. Score normalization and fusion weights stay identical; only the implementation language changes.
*   Not a routing-policy change. Branch definitions, signal weights, and thresholds preserved; only the implementation language changes. Any policy tuning happens in Python first, then ports once shadow-mode parity holds.

## 5. Phase 4: The Graph Engine (Multi-Hop Reasoning)
**Goal:** Enable instant recursive context assembly.

1.  **Lightweight In-Memory Index:** Rust maintains a compressed petgraph of memory_relationships.
2.  **Recursive Graph Traversal:** Move entity_graph BFS/DFS logic to Rust. Explore thousands of neighbors in microseconds.
3.  **Smart Neighbor Expansion:** Batch-fetch turns surrounding hits in a single SQL operation, reducing SQLite round-trips by 90%.

## 6. Phase 5: Hardening & Resilience
**Goal:** Ensure the system survives sustained stress and high-concurrency workloads.

1.  **Thread-Safe Connection Pooling:** Implement 2d2 or sqlx to handle high-concurrency retrieval without database is locked errors.
2.  **Circuit Breakers:** Add native retries and timeouts for the embedding and LLM servers.
3.  **Binary Portability:** Ensure the core can be compiled as a static binary for MCP distribution.

## 7. Benchmarking the "New Engine"
The transition is verified using the LME-S reproducible stack (v8 substrate):

*   **Baseline:** Execute ench/run_smart_bench.py (Python).
*   **Oxidation:** Swap import memory_core for import m3_core_rs as memory_core.
*   **Target Metrics:**
    *   **Retrieval Latency:** < 50ms per question (LME-M scale).
    *   **Ingest Time:** < 2 minutes for 246k LME-S turns.
    *   **Memory Overhead:** < 200MB steady-state.

## 8. Rollout Strategy: "The Surgical Swap"
1.  **Micro-Optimization:** Implement _pack and _unpack in Rust; call from Python memory_core.py.
2.  **Middle-Tier Migration:** Move the MMR and Cosine loops to Rust.
3.  **Full Engine Takeover:** Implement the main memory_search loop in Rust, keeping only the high-level routing logic in Python.

---
## 9. Development Workflow

This section documents *how* the oxidation work gets built, not *what* gets built. Constraints: solo developer, target outcome is a publicly reusable Rust crate set plus PyO3 bindings consumed by m3-memory.

### 9.1 Repository & Crate Layout

Single workspace, multiple focused crates. Each crate is independently useful; the PyO3 binding crate is the only one Python sees.

```
m3-core-rs/                          # workspace root (initially inside m3-memory/, extracts later — see §9.4)
├── Cargo.toml                       # workspace manifest, lists members
├── README.md
├── crates/
│   ├── m3-hash/                     # Phase 1.3 + 3d §4c.2 (FIPS-preserving)
│   ├── m3-vector/                   # Phase 2 (SIMD cosine, MMR)
│   ├── m3-dispatcher/               # Phase 3b/3c (generic ModelBackend coalescer)
│   ├── m3-embed-llamacpp/           # Phase 3b backend impl
│   ├── m3-ner-ort/                  # Phase 3c backend impl
│   ├── m3-redact/                   # Phase 3d §4c.1
│   ├── m3-rank/                     # Phase 3d §4c.4
│   ├── m3-route/                    # Phase 3d §4c.5
│   ├── m3-graph/                    # Phase 4
│   ├── m3-error/                    # shared M3Error type (Phase 1.2)
│   └── m3-core-py/                  # PyO3 bindings — depends on all above
└── tests/                           # workspace-level integration tests
```

**Reusability discipline:** generic crates use generic names in their public API (`Dispatcher`, `Hasher`, `RankRow` — not `M3Dispatcher`, `M3Hasher`). The `M3` prefix lives in `m3-core-py` and in env-var names only. A pure-Rust user could `cargo add m3-dispatcher` and use it without ever knowing m3-memory exists.

### 9.2 Branch Strategy (Solo)

*   **Single long-lived feature branch:** `feature/oxidation` off `main`.
*   **No per-phase sub-branches.** Solo doesn't need them — commit per phase boundary, tag at phase completion (`oxidation-p2-complete`, etc.).
*   **Rebase onto `main` weekly** to keep drift manageable. Python-side changes on `main` rarely touch the Rust paths once Phase 1–2 land, but the chatlog/memory_core surface will see ongoing churn.
*   **Use `git worktree` opportunistically**, not as default. The one case where it's genuinely useful: Phase 7 swap-tests, where you want Python baseline and Rust build running side-by-side for benchmarking. `git worktree add ../m3-py-baseline main` gives you that without checkout churn.
*   **Remote mapping:** `feature/oxidation*` is feature work, lands on `origin` (public). Bench artifacts from Phase 7 follow the existing rule and go to the `private` remote. See CLAUDE.md.

### 9.3 Solo PR Discipline

Even solo, treat phase completions as PRs to yourself:
*   Open a PR `feature/oxidation` → `main` at each phase boundary for review (you-as-reviewer-in-a-week catches things you-as-author missed).
*   Keep the PR open across the phase if helpful — squash-merge on completion.
*   Don't merge to `main` until the phase is gated behind a feature flag and the parity tests pass.

### 9.4 The Phase-2 → Phase-3 Extraction Trigger

`m3-core-rs/` starts as a subdirectory of `m3-memory` for Phases 1 and 2. **Extract to its own public repo (`github.com/skynetcmd/m3-core-rs`) at the Phase-2 → Phase-3 boundary.**

Rationale:
*   By end of Phase 2, the PyO3 surface (`M3Error`, `Hasher`, vector ops) is stable. The FFI contract has been exercised.
*   Phases 3b/3c/3d add significantly more crate surface; coordinating that across two repos is friction during development. Doing the extraction *before* that growth means you only move ~3 crates, not ~10.
*   m3-memory's `pyproject.toml` switches from a local path dependency to a versioned PyPI/git dependency. From then on, m3-core-rs has its own release cadence.

Mechanics of the extraction (one-time):
1.  `git subtree split --prefix=m3-core-rs -b oxidation-crate-extract`
2.  Push that branch to the new repo as its `main`.
3.  Update m3-memory's `pyproject.toml` to depend on the extracted crate.
4.  Delete `m3-memory/m3-core-rs/` and add a note in CLAUDE.md pointing to the new repo.

### 9.5 Publishing to crates.io

*   **No rush.** A crate is published only after it has been stable for one full release cycle.
*   Likely order: `m3-error` → `m3-hash` → `m3-vector` → `m3-dispatcher` → backends → `m3-core-py`.
*   `m3-core-py` may never be published to crates.io (PyO3 binding crates often aren't — they ship as wheels via PyPI instead). The other crates are the reusable surface.

### 9.6 The `M3_*` Env-Var Convention

**Principle:** namespace at the *binding* layer, not the *library* layer.

*   Generic crates (`m3-hash`, `m3-dispatcher`, `m3-vector`, …) accept **typed configuration structs**. They never call `std::env::var`.
*   `m3-core-py` is the only place that reads `M3_*` env vars. It translates them into the typed configs the generic crates consume.
*   A pure-Rust user of any generic crate builds the same struct from their own config source (TOML, CLI, kwargs) — they don't inherit the `M3_*` convention.

This preserves reusability *and* keeps the existing m3-memory env-var surface working unchanged for end users.

#### Planned `M3_*` env vars (new, introduced by oxidation)

| Env var | Phase | Type | Default | Controls |
|---|---|---|---|---|
| `M3_EMBED_STREAMS` | 3b | int | 8 | Dispatcher slot count (matches llama.cpp `-np`) |
| `M3_EMBED_COALESCE_MS` | 3b | int | 3 | Coalescing window (ms) |
| `M3_EMBED_MAX_BATCH_TOKENS` | 3b | int | 2048 | Matches llama.cpp `--batch-size` |
| `M3_EMBED_BACKEND` | 3b | enum | `embedded` | `http` / `embedded` (llama-cpp-rs) |
| `M3_EMBED_URL` | 3b | url | `http://127.0.0.1:8081` | Used when backend=http |
| `M3_EMBED_MODEL` | 3b | path | (none) | GGUF model file; used when backend=embedded |
| `M3_NER_MODEL_PATH` | 3c | path | `models/gliner/<version>.onnx` | ONNX file |
| `M3_NER_EP_PRIORITY` | 3c | csv | (per-platform table §4b.2) | Override execution-provider order |
| `M3_NER_QUANT` | 3c | enum | `int8` on CPU, `fp16` on GPU | `fp32` / `fp16` / `int8` |
| `M3_HASH_PROVIDER` | 1.3 / 3d | enum | `ring` | `ring` (FIPS) / `sha2` (requires `non-fips-perf` feature) |
| `M3_REDACT_PROFILE` | 3d | string | (inherits Python default) | Redaction profile name |
| `M3_DECAY_DRY_RUN` | 3d | bool | `false` | Preview only, no writes |
| `M3_ROUTE_SHADOW_MODE` | 3d §4c.5 | enum | `log` | `off` / `log` / `enforce` (shadow→cutover gate) |
| `M3_CORE_RS_DISABLE` | 8 | bool | `false` | Emergency kill-switch: import Python `memory_core` instead of `m3_core_rs` |

`M3_CORE_RS_DISABLE` is the load-bearing one. It lets a misbehaving Rust core be disabled with `export M3_CORE_RS_DISABLE=1` and a process restart — no redeploy, no code change.

#### Existing `M3_*` env vars (audit complete, 2026-05-14)

Audited against the live tree and cross-checked against `docs/tools/INDEX.md` (107 tools). **73 `M3_*`-prefixed vars** in active use, plus **32 non-prefixed vars** that belong to m3-memory's surface and should be namespaced under `M3_*` during the oxidation work. Auth/credential env vars (3) are listed separately as they touch the FIPS path.

The Rust binding crate (`m3-core-py`) MUST surface all 73 existing `M3_*` vars unchanged for backward compatibility, and SHOULD accept both the legacy and the `M3_`-prefixed alias forms during a deprecation window for the 32 non-prefixed vars.

##### Reconciliation summary

| Group | Count | Disposition |
|---|---|---|
| `M3_*` vars (existing) | 73 | Preserved verbatim in `m3-core-py`. Typed configs derived from them. |
| Non-prefixed vars belonging to m3-memory | 32 | Add `M3_*` aliases (e.g. `M3_CHATLOG_DB_PATH` for `CHATLOG_DB_PATH`); both forms accepted for one release cycle; legacy form emits a deprecation log line. |
| Auth/credential vars | 3 | `AGENT_OS_MASTER_KEY`, `LM_STUDIO_API_KEY`, `LM_API_TOKEN`. NOT prefixed (they're conventional secrets-manager names). Routed through `M3Hasher`'s sibling auth surface; FIPS path must verify them via `ring`. |
| Audit gap (reader files cited only as users, not introducers) | 16 | `test_*.py`, `migrate_*.py`, `augment_memory.py`, `weekly_auditor.py`, `setup_secret.py`, `cli_knowledge.py`, `re_embed_all.py`, `mission_control.py`, `examples/mac-agent/router/router.py`, etc. — all set or read existing vars; no new vars introduced. |

##### Reader → tool inventory cross-check

Cross-referenced against `docs/tools/INDEX.md`. **All env-var readers are listed tools.** No drift between the inventory and the env-var surface. Readers split:

*   **Hot-path readers** (must route through `m3-core-py` post-oxidation): `bin/memory_core.py`, `bin/chatlog_config.py`, `bin/chatlog_ingest.py`, `bin/chatlog_embed_sweeper.py`, `bin/m3_entities.py`, `bin/backfill_content_hash.py`, `bin/embed_backfill.py`, `bin/m3_enrich.py`, `bin/m3_enrich_batch.py`, `bin/slm_intent.py`, `bin/auto_route.py` (when promoted §4c.5), `bin/sqlite_pragmas.py`.
*   **Bootstrap/config readers** (stay in Python, read env vars before any Rust call): `m3_memory/cli.py`, `m3_memory/installer.py`, `bin/m3_sdk.py`, `bin/crypto_provider.py`, `bin/memory_bridge.py`.
*   **Out-of-scope readers** (not on the oxidation path): `bin/discord_bot.py`, `bin/mission_control.py`, `bin/test_*.py`, `bin/setup_*.py`, `examples/mac-agent/*`.

##### Full `M3_*` inventory (73 vars)

| Env var | Default | Type | Controls | Primary reader |
|---|---|---|---|---|
| M3_AUTO_ENRICH | `0` | bool | Auto-enrich on ingest gate | bin/chatlog_ingest.py |
| M3_AUTO_ENRICH_MIN_TURNS | `10` | int | Min turns before enrichment | bin/chatlog_ingest.py |
| M3_AUTO_INSTALL | (unset) | bool | Skip auto-install on import | m3_memory/cli.py |
| M3_AUTO_RELATED_LINK | `1` | bool | Auto-link related memories on write | bin/memory_core.py |
| M3_AUTO_RELATED_LINK_SCOPE_BY_VARIANT | `1` | bool | Restrict related-link to same variant | bin/memory_core.py |
| M3_BRIDGE_PATH | (unset) | path | MCP bridge executable path | m3_memory/installer.py |
| M3_CHROMA_SYNC_QUEUE_MAX | `500000` | int | Max queue depth before warning | bin/chroma_health.py, bin/memory_sync.py |
| M3_CHROMA_SYNC_QUEUE_SKIP_AT | `0` | int | Skip sync above threshold | bin/memory_sync.py |
| M3_CHROMA_SYNC_QUEUE_WARN | `100000` | int | Warn above threshold | bin/chroma_health.py, bin/memory_sync.py |
| M3_CONTEXT_CACHE_SIZE | `16` | int | LLM context cache size (min 2) | bin/m3_sdk.py |
| M3_CRYPTO_BACKEND | `DEFAULT` | enum | Encryption backend (DEFAULT/WOLFSSL) | bin/crypto_provider.py |
| M3_DATABASE | `memory/agent_memory.db` | path | Main memory DB path | bin/memory_core.py (+ many) |
| M3_DEBUG | (unset) | bool | Enable debug output | bin/memory_core.py |
| M3_DISABLE_AUTO_ACTIVATION | (unset) | bool | Prevent auto-activation of memory search | bin/memory_core.py |
| M3_DOCS_DIR | `/opt/m3-memory` | path | Location of docs files | bin/discord_bot.py |
| M3_ELBOW_ABS_THRESHOLD | `0.05` | float | Min cosine drop for elbow | bin/memory_core.py |
| M3_ELBOW_MIN_INPUT | `20` | int | Min samples for elbow heuristic | bin/memory_core.py |
| M3_ELBOW_MIN_RETURN | `8` | int | Min results to preserve after elbow | bin/memory_core.py |
| M3_EMBED_MODEL | (unset) | string | Embedding model override | bin/m3_enrich.py (+ others) |
| M3_EMBED_URL | (unset) | url | Embedding server URL override | bin/m3_enrich.py (+ others) |
| M3_ENABLE_ENTITY_GRAPH | `false` | bool | Enable entity-graph pipeline | bin/m3_entities.py, bin/memory_core.py |
| M3_ENABLE_FACT_ENRICHED | `false` | bool | Enable fact-enriched retrieval | bin/memory_core.py |
| M3_ENRICH_BUDGET_USD | (unset) | float | Max USD spend cap | bin/m3_enrich.py |
| M3_ENRICH_CONV_LIST | (unset) | csv | Conversation IDs to enrich | bin/m3_enrich.py |
| M3_ENRICH_INPUT_MAX_K | (unset) | int | Max input size (K rows) | bin/m3_enrich.py |
| M3_ENRICH_MAX_ATTEMPTS | `5` | int | Max retries per turn | bin/m3_enrich.py |
| M3_ENRICH_MAX_SIZE_K | (unset) | int | Max memory size (K) | bin/m3_enrich.py |
| M3_ENRICH_MIN_SIZE_K | (unset) | int | Min memory size (K) | bin/m3_enrich.py |
| M3_ENRICH_PROFILE | `enrich_local_qwen` | string | LLM profile for enrichment | bin/m3_enrich.py |
| M3_ENRICH_SEND_TO | (unset) | email | Destination email for results | bin/m3_enrich.py |
| M3_ENRICH_TRACK_STATE | `0` | bool | Track enrichment state | bin/m3_enrich.py |
| M3_ENTITIES_CONV_LIST | (unset) | csv | Conversation IDs for entity extraction | bin/m3_entities.py |
| M3_ENTITY_EXTRACT_CONCURRENCY | `2` | int | Parallel entity extraction workers | bin/memory_core.py, bin/m3_entities.py |
| M3_ENTITY_EXTRACT_MAX_ATTEMPTS | `3` | int | Max retries for entity extraction | bin/memory_core.py |
| M3_ENTITY_EXTRACTOR_MAX_ATTEMPTS | `3` | int | Alias for above (legacy) | bin/memory_core.py |
| M3_ENTITY_RESOLVE_COSINE_MIN | `0.85` | float | Min cosine for entity resolution | bin/memory_core.py |
| M3_ENTITY_RESOLVE_FUZZY_MIN | `0.8` | float | Min fuzzy score for entity resolution | bin/memory_core.py |
| M3_ENTITY_SEED_STOPLIST | `User,user,assistant` | csv | Entities excluded from BFS expansion | bin/memory_core.py |
| M3_ENTITY_VOCAB_YAML | (unset) | path | Entity type/predicate vocab YAML | bin/memory_core.py, bin/m3_entities.py |
| M3_EXPANSION_DISPLACEMENT_MARGIN | `1.75` | float | Margin for expansion-vs-primary guard | bin/memory_core.py |
| M3_EXPANSION_PROTECTED_RANKS | `3` | int | Ranks protected from displacement | bin/memory_core.py |
| M3_FACT_ENRICH_CONCURRENCY | `2` | int | Parallel fact enrichment workers | bin/memory_core.py |
| M3_FACT_ENRICH_MAX_ATTEMPTS | `5` | int | Max retries for fact enrichment | bin/memory_core.py |
| M3_FEDERATION_LOW_SCORE_THRESHOLD | `0.65` | float | Min score for federation retrieval | bin/memory_core.py |
| M3_HTTP_HOST | `127.0.0.1` | ip | MCP HTTP bind address | m3_memory/cli.py |
| M3_HTTP_PATH | `/mcp` | string | MCP HTTP path prefix | m3_memory/cli.py |
| M3_HTTP_PORT | `8080` | int | MCP HTTP port | m3_memory/cli.py |
| M3_IMPORTANCE_WEIGHT | `0.05` | float | Importance field weight in scoring | bin/memory_core.py |
| M3_INGEST_EVENT_ROWS | `0` | bool | Emit event-type rows during ingest | bin/memory_core.py |
| M3_INGEST_GIST_MIN_TURNS | `8` | int | Min turns to create gist row | bin/memory_core.py |
| M3_INGEST_GIST_ROWS | `0` | bool | Emit gist-type rows during ingest | bin/memory_core.py |
| M3_INGEST_GIST_STRIDE | `8` | int | Stride for gist row generation | bin/memory_core.py |
| M3_INGEST_WINDOW_CHUNKS | `0` | bool | Emit window-chunk rows during ingest | bin/memory_core.py |
| M3_INGEST_WINDOW_SIZE | `3` | int | Sliding window size for chunks | bin/memory_core.py |
| M3_INTENT_ROUTING | `0` | bool | Route queries by intent hint | bin/memory_core.py |
| M3_INTENT_USER_FACT_BOOST | `0.1` | float | Score boost for user-fact intent | bin/memory_core.py |
| M3_MEMORY_ROOT | (inferred from `__file__`) | path | Root dir of m3-memory installation | bin/m3_sdk.py, m3_memory/installer.py |
| M3_OBSERVATION_BUDGET_TOKENS | `4000` | int | Token budget for observation retrieval | bin/memory_core.py |
| M3_QUERY_TYPE_ROUTING | `0` | bool | Route queries by type hint | bin/memory_core.py |
| M3_RERANK_MODEL | `cross-encoder/ms-marco-MiniLM-L-6-v2` | string | Cross-encoder for reranking | bin/memory_core.py |
| M3_ROUTER_TEMPORAL_K_BUMP | (varies) | int | Boost K for temporal queries | bin/memory_core.py |
| M3_SHORT_TURN_THRESHOLD | `20` | int | Char threshold for "short" turn | bin/memory_core.py |
| M3_SLM_CLASSIFIER | (unset) | bool | Enable SLM intent classification | bin/slm_intent.py |
| M3_SLM_PROFILE | `default` | string | SLM profile for intent classification | bin/slm_intent.py |
| M3_SLM_PROFILES_DIR | (inferred from M3_MEMORY_ROOT) | path | SLM intent profiles directory | bin/slm_intent.py |
| M3_SPEAKER_IN_TITLE | `1` | bool | Include speaker role in titles | bin/memory_core.py |
| M3_SQLITE_MMAP_SIZE | (unset) | int | SQLite mmap size (bytes) | bin/sqlite_pragmas.py |
| M3_SYNC_DBS | `` | csv | DBs to sync | bin/sync_all.py |
| M3_TITLE_MATCH_BOOST | `0.05` | float | Boost when title matches query | bin/memory_core.py |
| M3_TRANSPORT | `stdio` | enum | MCP transport (stdio/http) | m3_memory/cli.py, bin/memory_bridge.py |
| M3_TWO_STAGE_MAX_TURNS_PER_OBS | `3` | int | Max turns per observation (two-stage) | bin/memory_core.py |
| M3_TWO_STAGE_TURN_PENALTY | `0.7` | float | Turn age penalty (two-stage) | bin/memory_core.py |

##### Non-prefixed vars to namespace under `M3_*` (32)

Both forms accepted in `m3-core-py` for one release cycle; legacy form emits a `DeprecationWarning` via Python logging.

| Legacy var | New alias | Default | Type |
|---|---|---|---|
| CHATLOG_DB_PATH | M3_CHATLOG_DB_PATH | `memory/agent_chatlog.db` | path |
| CHATLOG_DB_POOL_SIZE | M3_CHATLOG_DB_POOL_SIZE | `4` | int |
| CHATLOG_DB_POOL_TIMEOUT | M3_CHATLOG_DB_POOL_TIMEOUT | `10` | int |
| CHATLOG_EMBED_MAX_PER_RUN | M3_CHATLOG_EMBED_MAX_PER_RUN | `10000` | int |
| CHATLOG_STATUSLINE | M3_CHATLOG_STATUSLINE | (unset) | bool |
| CHATLOG_STATUSLINE_ASCII | M3_CHATLOG_STATUSLINE_ASCII | (unset) | bool |
| CHROMA_BASE_URL | M3_CHROMA_BASE_URL | (unset) | url |
| CONTRADICTION_THRESHOLD | M3_CONTRADICTION_THRESHOLD | `0.92` | float |
| CONTRADICTION_TITLE_GATE | M3_CONTRADICTION_TITLE_GATE | `loose` | enum |
| CONTRADICTION_TYPE_EXCLUSIONS | M3_CONTRADICTION_TYPE_EXCLUSIONS | `conversation` | csv |
| DB_POOL_SIZE | M3_DB_POOL_SIZE | `5` | int |
| DB_POOL_TIMEOUT | M3_DB_POOL_TIMEOUT | `30` | int |
| DEDUP_LIMIT | M3_DEDUP_LIMIT | `1000` | int |
| DEDUP_THRESHOLD | M3_DEDUP_THRESHOLD | `0.92` | float |
| EMBED_BULK_CHUNK | M3_EMBED_BULK_CHUNK | `1024` | int |
| EMBED_BULK_CONCURRENCY | M3_EMBED_BULK_CONCURRENCY | `4` | int |
| EMBED_DIM | M3_EMBED_DIM | `1024` | int |
| EMBED_MODEL (in `memory_core.py`) | merged with M3_EMBED_MODEL above | `qwen3-embedding` | string |
| EMBED_PRIMARY | M3_EMBED_PRIMARY | `http://localhost:1234` | url |
| EMBED_SECONDARY | M3_EMBED_SECONDARY | `http://10.0.0.2:1234` | url |
| EMBED_SERVER_GPU_HOST | M3_EMBED_SERVER_GPU_HOST | `127.0.0.1` | ip |
| EMBED_SERVER_HOST | M3_EMBED_SERVER_HOST | `127.0.0.1` | ip |
| EMBED_TERTIARY | M3_EMBED_TERTIARY | `http://10.0.0.226:1234` | url |
| ENTITY_NAME_EMBED_CACHE_MAX | M3_ENTITY_NAME_EMBED_CACHE_MAX | `50000` | int |
| LLAMA_PORT | M3_LLAMA_PORT | `9904` | int |
| LLM_READ_TIMEOUT | M3_LLM_READ_TIMEOUT | `4800.0` | float |
| LLM_TIMEOUT | M3_LLM_TIMEOUT | `120.0` | float |
| LM_STUDIO_BASE | M3_LM_STUDIO_BASE | `http://localhost:1234/v1` | url |
| ORIGIN_DEVICE | M3_ORIGIN_DEVICE | `platform.node()` | string |
| PG_URL | M3_PG_URL | (unset) | url |
| SEARCH_ROW_CAP | M3_SEARCH_ROW_CAP | `5000` | int |
| SUPERSEDES_PENALTY | M3_SUPERSEDES_PENALTY | `0.5` | float |

##### Auth/credential vars (NOT namespaced — touch FIPS path)

These three follow secrets-manager naming convention and stay unprefixed. They route through the auth surface adjacent to `M3Hasher`; FIPS validation is mandatory.

| Var | Reader | Notes |
|---|---|---|
| AGENT_OS_MASTER_KEY | bin/auth_utils.py | Master encryption key. Production: must come from OS keychain, not env. FIPS path verifies via `ring`. |
| LM_STUDIO_API_KEY | bin/auth_utils.py | LM Studio API key. Optional fallback. |
| LM_API_TOKEN | bin/m3_cognitive_loop.py | Generic LM API token. |

##### Conflicts & gotchas surfaced by reconciliation

1.  **`M3_EMBED_MODEL` is read in two places with different defaults.** `bin/m3_enrich.py` reads it as override (no default); `bin/memory_core.py` reads `EMBED_MODEL` (no `M3_` prefix) with default `qwen3-embedding`. Post-oxidation: consolidate on `M3_EMBED_MODEL` with default `qwen3-embedding`; `EMBED_MODEL` accepted as legacy alias.
2.  **`M3_ENTITY_EXTRACTOR_MAX_ATTEMPTS` is a typo-alias of `M3_ENTITY_EXTRACT_MAX_ATTEMPTS`.** Both supported in legacy Python; the Rust binding should accept both and log a deprecation for the typo form.
3.  **`M3_MEMORY_ROOT` and `M3_SLM_PROFILES_DIR` are inferred when unset.** The Rust binding must preserve the inference logic (walk up from `__file__`); cannot just fall back to a hardcoded path.
4.  **`M3_ROUTER_TEMPORAL_K_BUMP` has caller-dependent defaults.** Different call sites in `memory_core.py` supply different defaults. The Rust port must preserve per-call-site defaults rather than hoisting to a single global default.
5.  **The new `M3_HASH_PROVIDER` env var (introduced in this plan §9.6) does not conflict** with any existing var. The existing `M3_CRYPTO_BACKEND` (DEFAULT/WOLFSSL) controls encryption backend, not hashing — orthogonal.

### 9.7 Config Source Precedence

When multiple sources can set the same knob, the order from highest to lowest precedence is:

1.  Python kwarg / explicit constructor argument
2.  `M3_*` env var
3.  Config file (existing m3-memory config, where present)
4.  Built-in default (hardcoded in `m3-core-py`)

This matches existing m3-memory behavior; the Rust layer is not allowed to invert this precedence.

### 9.8 Testing Discipline

*   Each crate owns its parity tests in `crates/<crate>/tests/`.
*   Workspace-level integration tests in `m3-core-rs/tests/` exercise the full pipeline (ingest → embed → rank → return).
*   Python regression suites (`test_fips_integrity.py`, the chatlog redaction corpus, the enrichment F1 suite, the 10k-query route corpus) all run against the Rust path via `m3-core-py` and must pass before any phase merges.

---
## Appendix A: Supplementary Rust Targets (Backlog, Not Committed Scope)

Surveyed against the live tool inventory (`docs/tools/INDEX.md`, 107 tools as of 2026-05-09). Listed for visibility and future sequencing; **not** committed to any phase. Each entry includes the file, why it might be worth porting, and the dominant risk.

### Tier 1 — High value, well-scoped (candidates for promotion into a future phase)

| Target | File | Why | Risk |
|---|---|---|---|
| Multi-signal route decider | `bin/auto_route.py` | *Promoted into Phase 3d (§4c.5), with shadow-mode behavior-parity gate.* | — |
| Hybrid rank-fusion merge | inside `bin/memory_core.py` | *Promoted into Phase 3d (§4c.4).* | — |
| Redaction | `bin/chatlog_redaction.py` | *Promoted into Phase 3d (§4c.1).* | — |
| Content hashing | `bin/backfill_content_hash.py` + callers | *Promoted into Phase 3d (§4c.2), FIPS-preserving.* | — |
| Decay sweep | `bin/chatlog_decay.py` | *Promoted into Phase 3d (§4c.3).* | — |

### Tier 2 — Meaningful gains, larger surface

| Target | File(s) | Why | Risk |
|---|---|---|---|
| Entity-graph row builder | `bin/m3_entities.py` | Active development (recent `entity_seeds_dropped`, `mention_offset` work); per-mention loops over tokens. Pairs with Phase 3c GLiNER output → entity-row pipeline. | Schema-coupled — entity-link write path tightly bound to migrations. |
| KG variant builder | `bin/build_kg_variant.py` + migrations 033/034 | Batch graph construction over the full corpus; `petgraph` + `rayon` can cut multi-hour runs to minutes. | Determinism gate needed — KG outputs feed retrieval. |
| Temporal resolution | `bin/temporal_utils.py` | Relative-date parsing + chrono arithmetic; called per extracted event. Rust `chrono` is faster and better-typed. | Edge cases in natural-language date parsing — parity test must cover the full corpus of historical phrasings. |
| SLM intent classifier | `bin/slm_intent.py` | Same shape as GLiNER; route through the Phase 3c `ort` backend with a different ONNX file. Near-zero additional dispatcher work. | Requires clean ONNX export of the SLM. |

### Tier 3 — Defensible but smaller gains (measure first)

| Target | File(s) | Why | Risk |
|---|---|---|---|
| MCP tool dispatch (hot tools only) | `bin/mcp_proxy.py` + `bin/mcp_tool_catalog.py` | Per-call overhead on high-frequency m3 tools (`memory_search`, `chatlog_write`, heartbeats) is mostly I/O + JSON, but a Rust core for the hot tools via PyO3 could shave ms. | Mostly I/O-bound — measure overhead before committing. |
| Migration runner | `bin/migrate_memory.py` | Rust + prepared statements + batched txns cuts large-DB migration time 5–10×. | Rarely run; payoff only if migrations are visibly painful. |
| Chroma / Postgres sync | `bin/chroma_sync_cli.py`, `bin/pg_sync.py` | Large-batch diff + upsert; `tokio-postgres` / `sqlx` are mature. | Wire protocol coupling to ChromaDB version. |
| Agent heartbeat / notify | `bin/agent_protocol.py` + MCP `agent_*` / `notify` tools | High call frequency in multi-agent setups; mostly DB writes + small JSON. | Low absolute cost today; defer until heartbeat scales become a problem. |

### Explicitly NOT recommended for Rust

| Target | Why keep in Python |
|---|---|
| `bin/llm_failover.py`, `bin/batch_runner.py`, `bin/grok_bridge.py`, `bin/web_research_bridge.py` | Bottleneck is the remote LLM / web service. Zero gain from Rust. |
| `bin/mission_control.py`, `bin/chatlog_status*.py`, `bin/m3_enrich_report.py` | Dashboarding / reporting. Not hot. Python ergonomics win. |
| `bin/test_*.py` | Pytest ergonomics > speed. |
| `bin/generate_configs.py`, `bin/install_schedules.py`, `bin/setup_*.py`, `bin/fetch_sovereign_assets.py` | One-shot bootstrap. Python is the right tool. |
| `bin/m3_cognitive_loop.py` | Orchestration logic — must stay readable and changeable. |
| `bin/embed_server.py`, `bin/embed_server_gpu.py` | Already a thin wrapper around llama.cpp; Phase 3b replaces the dispatcher layer, not the server. |
| `bin/m3_sdk.py`, `bin/memory_bridge.py`, `bin/custom_tool_bridge.py` | Integration / SDK surfaces. Stability and ergonomics > perf. |

### Post-Phase-0–6 audit findings (2026-05-17)

After the `bin/memory_core.py` modularization closed (Phases 0–6, commit range `9344407..3c6ef5f` on m3-memory main; –52 % LOC, structure recorded in `docs/MEMORY_CORE_MODULARIZATION.md`), a Tier-A "drop-in Rust adoption" pass was run against the new modular code. Three candidates were evaluated; one shipped, two were skipped on principled grounds. Recorded here so future passes don't re-litigate.

| Candidate | Outcome | Reason | Commit |
|---|---|---|---|
| `mmr_rerank_scored` → `mmr_rerank_scored_packed` in `bin/memory/search.py` | **ADOPTED** | Skips per-row unpack of packed embedding blobs; Rust slices the contiguous bytes buffer via PyO3 zero-copy borrow and releases the GIL while rayon SIMD runs MMR. Parity-verified on synthetic 50×1024 inputs (identical selection); retrieval baseline byte-identical across 60 queries × 3 variants. Unpacked path preserved as a fallback for the partially-migrated-blob case and the `explain=True` per-candidate annotation case. | `c469288` |
| `m3_core_rs.CircuitBreaker` per-backend in `bin/memory/embed.py` cascade | **ADOPTED** | Three breakers (`_EMBEDDED_BREAKER`, `_CPU_FALLBACK_BREAKER`, `_PRIMARY_BREAKER`) gate each cascade tier — `allow_request()` before attempt, `record_success/failure` on outcome. Storm-tested: 5 sequential calls against a dead fallback URL dropped from 2 s each (raw connect-timeout) to ~50 ms after the breaker tripped at strike 3 — ~97 % per-call wall-time reduction during a backend outage. Three-state model (closed / open / half_open) with single half-open probe on `reset_after_secs` elapse. Tunable via `M3_EMBED_BREAKER_*_THRESHOLD` / `_RESET_SECS` env vars; threshold=0 or `M3_CORE_RS_DISABLE=1` falls back to pre-breaker "try every call" behaviour. Closes plan-doc item L. | pending commit (m3-memory main, 2026-05-17) |
| `_content_hash` → `m3_core_rs.sha256_hex_bytes` in `bin/memory/embed.py` | SKIPPED | Two reasons: (a) FIPS rule in §4c.2 — `ring` is the only production crypto provider, `sha2` is CI-blocked — so no fast-but-non-FIPS Rust path is available here; (b) measured 2× **regression** vs Python `hashlib` at typical memory-body sizes (≤ few KB) because PyO3 FFI overhead exceeds SHA-256 compute time at that scale. See updated §4c.2 measured-baseline note. | n/a |
| `len(text.split())*2` → `m3_core_rs.estimate_tokens` for `_COST_COUNTERS["embed_tokens_est"]` | SKIPPED | Both are heuristics; switching changes the reported token count by ~50 % (Rust uses 4-chars/token, Python uses word-count×2). Telemetry-stability concern: any downstream cost dashboard / billing report would see the count drop. Not worth breaking observability for a heuristic refinement that the actual embedder never sees. | n/a |

**Rules surfaced by this audit (worth applying to future tier-promotion decisions):**

1.  **An "available" Rust function is not an "adoption candidate."** The `dir(m3_core_rs)` inventory is a starting point. Each must be screened against: (a) is there an existing FIPS/policy constraint? (b) is the Python equivalent already a C-extension that wins on this input size? (c) does the switch change observable behavior (telemetry counters, error messages, env-config semantics) in a way downstream consumers would notice? If any of those is "yes" without a written waiver, defer.
2.  **PyO3 FFI overhead is real at small inputs.** For primitives where the per-call work is ~µs (single SHA-256 of a ≤ 1 KB string, a single `cosine` over a 1024-dim vector), the FFI hop dominates and Python C-extensions often win. Rust wins where batching keeps the FFI cost amortised: `cosine_batch_packed_flat` (one call, N×N pairwise scores), `mmr_rerank_scored_packed` (one call, K-of-N selection). **Rule: prefer Rust functions whose call shape is "many items per FFI hop."**
3.  **Memory-search before benchmark.** The SHA-256 audit re-discovered the FIPS rule and the small-input perf wall via benchmark when both were already documented (memory `8cb292d1`, §4c.2 above). Search memory and the plan text first; benchmark to confirm, not to discover. Recorded as preference `8cae97da` in the m3-memory store.

**What's still live as a Tier-2 candidate after this audit:**

*   `m3_core_rs.GraphIndex` for `_graph_neighbor_ids` (currently SQL with `WHERE from_id IN (...) OR to_id IN (...)` which defeats per-column indexes). Needs measurement before promotion; potentially 5–20× on multi-hop graph traversal.
*   `m3_core_rs.decide_route` / `extract_signals` for the AUTO routing layer in `bin/memory/search.py`. Semantics must match `bin/auto_route.py` first — a shadow-mode parity gate before swap.

*(Removed from this list 2026-05-17: `m3_core_rs.CircuitBreaker` — adopted in the audit table above and promoted out of Tier-2.)*

### FIPS guardrail (applies to all future ports)

No Tier-1/2/3 promotion may introduce a non-FIPS crypto crate into the default feature set. The `ring`-only default established in Phase 3d (§4c.2) is permanent. Any future port that computes hashes, signs, or verifies must route through `M3Hasher` (or its sibling traits for HMAC / signing when added). CI gate: `cargo tree --no-default-features | grep -E '^(sha2|md-5|sha1|blake3)'` must return empty.

---
**Plan Version:** 1.8 (Tier-A audit row added: `m3_core_rs.CircuitBreaker` adopted per-backend in `bin/memory/embed.py` cascade — closes plan-doc item L; storm-tested ~97 % per-call wall-time reduction during a dead-backend window. v1.7 retained items: §4c.2 measured-baseline SHA note; `mmr_rerank_scored_packed` adoption.)
**Target Hardware:** RTX 5080 + NVMe Storage (primary); validation also on M-series macOS and Windows-on-ARM
**Status:** Ready for Scaffolding.
