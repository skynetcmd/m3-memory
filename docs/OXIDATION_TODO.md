# Project Oxidation — Follow-up TODO

Engineering backlog for the Rust core ([`m3-core-rs`](https://github.com/skynetcmd/m3-core-rs))
integration. This is *not* the community roadmap (see `ROADMAP.md`) — it tracks
known-incomplete work and deferred decisions from the oxidation effort.

Status as of 2026-05-14. The Rust core is wired into m3-memory's hot path for
**five operations** — cosine, batch-cosine, MMR, the expansion-displacement
guard, and chat-log redaction — all behind the `M3_CORE_RS_DISABLE` kill-switch,
all parity-verified. In-process llama.cpp embeddings are also wired (opt-in via
`M3_EMBED_GGUF`). SHA-256 was wired and then **reverted** to pure-Python
`hashlib` (2026-05-14) — the micro-benchmark showed the Rust path is slower at
every realistic input size; see the benchmark item below. What follows is what
is *not* done.

---

## Blocked on external artifacts

- [ ] **GLiNER ONNX model export.** `m3-ner-ort` compiles (the `ort` `load-dynamic`
  linkage works, `onnxruntime.dll` loads), but it cannot run NER — there is no
  exported GLiNER `.onnx` model + `tokenizer.json` in the repo. Plan §4b.1 estimates
  this as a 1–2 day export task. Until it exists, the NER backend is compile-verified
  only. Owner decision: export the active GLiNER checkpoint, store under
  `models/gliner/<version>.onnx` with a checksum manifest.

- [ ] **Route shadow-mode corpus.** `auto_route.py` runs `M3_ROUTE_SHADOW_MODE=log`,
  comparing the Rust `decide_route` against Python `decide_branch`. But there is no
  10k-query corpus to *evaluate* the disagreement rate against (plan §4c.5 requires
  one before any cutover). Sample queries disagree 4/4 — expected, since the Rust
  decider sees only the query (no `candidates` signals) and uses a different branch
  vocabulary. Next: collect shadow-mode logs over real traffic, build the corpus,
  measure the rate. No cutover until then.

---

## Decisions deferred to a human

- [ ] **Embedded-embedder default switch.** The in-process llama.cpp path
  (`M3_EMBED_GGUF`) is verified at cosine ≈ 0.996 against the **550**
  `bge-m3-GGUF-Q4_K_M.gguf`-tagged rows. The **~14k** LM Studio
  `text-embedding-bge-m3` rows are a *separate, unverified* embedding namespace.
  Making the embedded path the *default* embedder would strand the bulk of the
  existing index. It is intentionally opt-in. Decision needed: leave opt-in, or
  re-embed the corpus under one tag, or verify embedded-vs-LM-Studio parity first.

- [ ] **Route cutover.** Gated on the shadow corpus above *and* a branch-name
  mapping (Python `temporal/multi_session/sharp/entity_anchored/default` vs Rust
  `entity/lexical/semantic/temporal`). `M3_ROUTE_SHADOW_MODE=enforce` is reserved
  but deliberately unimplemented.

- [ ] **`m3-rank` disposition.** The crate's `fuse` function is two-list rank-fusion;
  m3-memory's actual FTS5+vector merge is a per-row scoring loop with no two separate
  lists to fuse. `m3-rank` is **not applicable** to m3-memory as currently designed.
  Decide: leave it as an unused generic primitive, repurpose it, or drop it from the
  workspace.

---

## Verification debt

- [x] **Per-operation micro-benchmark.** _Done 2026-05-14_ — `tests/bench_oxidation.py`
  times each swap FFI-inclusive against its Python baseline on realistic inputs.
  Results: MMR 55–85× faster, cosine ~3×, cosine_batch 2.5–3×, redaction 8.5–10×
  faster. It earned its keep by catching two problems:
  - `m3_core_rs.scrub` was ~13× *slower* (recompiled regexes per call) — fixed by
    caching the compiled `Redactor` in the binding, re-benchmarked at 8.5–10× faster.
  - `sha256` was slower at every realistic input size (~0.4–0.9×; FFI overhead vs
    `hashlib`, which is already OpenSSL C with SHA-NI). A crossover sweep confirmed
    `ring` and `hashlib` only *tie* above ~64KB — Rust never wins on turn-sized
    content. **Reverted to pure-Python `hashlib`** (`memory_core.py::_sha256_hex`).
    FIPS is unaffected — a FIPS-validated OpenSSL makes `hashlib.sha256` the
    validated path; the `ring`-based `m3-hash` crate stays FIPS-gated in the
    workspace for any Rust-side hashing, just unwired from this hot path.
- [x] **End-to-end retrieval benchmark — local-scale proxy.** _Done 2026-05-14_ —
  `tests/bench_e2e_retrieval.py` drives the real `memory_search_scored_impl`
  against the local `agent_memory.db` (~20k items), Rust core on vs off,
  subprocess-per-arm. Amdahl-shaped result: **1.37×** at k=8 (scoring is a small
  slice of a small-k query), **4.56×** at k=20, **36.8×** at k=50 — Python's MMR
  is O(k·n) and degrades to ~2.3 s at k=50 while the Rust path stays flat ~62 ms.
  Sanity: 20/20 queries parity-clean at the set level; one order-only divergence
  (ranks 2/3 swapped) traced to the float32 round-off boundary between Rust and
  numpy cosine on near-tied scores — deterministic per-arm, benign.
- [ ] **End-to-end retrieval benchmark — LME-S scale.** The local proxy above is
  ~20k rows; it does NOT speak to the plan's headline target (<50 ms retrieval
  p50 at LME-M scale, ingest throughput). That still needs the LME-S reproducible
  stack run with and without the Rust core — not available in this repo, requires
  the private bench stack.

- [ ] **Embedded-embedder bulk-namespace parity.** See the default-switch item —
  before any corpus-wide adoption, verify the embedded backend matches the LM Studio
  `text-embedding-bge-m3` vectors, not just the llama.cpp-tagged ones.

- [ ] **Redaction corpus parity.** `m3-redact` is a byte-exact port of
  `chatlog_redaction.py` verified against hand-built harness inputs
  (`tests/test_redaction_parity.py`). There is no real captured-turn redaction
  corpus in the repo to test against. If one exists elsewhere, run it.

---

## Future Performance Optimization Targets (Rust)

- [ ] **Candidate-Assembly Loop Oxidation.** `memory_search_scored_impl` is heavily optimized at the math level (MMR, cosine) but still suffers from Python orchestration overhead (looping over up to 5,000 DB rows, building dictionaries). The entire hybrid scoring and filtering loop should be moved to Rust. A Rust function should take the query embedding and a raw buffer of results, performing the hybrid FTS5+vector logic natively to support 5M+ row haystacks at <50ms.
- [ ] **Entity Resolution Speedup.** The 3-tier entity resolution (Exact → Token-Jaccard → Embedding) in `bin/memory/entity.py` is currently in Python. Fuzzy-matching against thousands of entities would benefit significantly from a Rust implementation.

---

## Hygiene / smaller items

- [ ] **macOS Wheel Automation.** Create a GitHub Actions workflow in `m3-core-rs` to build and publish pre-compiled macOS wheels (Apple Silicon + Intel) with `embedded-metal` support. This removes the Xcode/Rust toolchain requirement for macOS users. See `docs/OXIDATION_FIXES.md` for current status.

- [ ] **Full env-var reconcile re-sweep cadence.** `docs/tools/ENV_VAR_RECONCILE_REPORT.md`
  was fully re-swept 2026-05-14 (all three groups). Re-run when new `M3_*` vars are
  added or `INDEX.md` is regenerated — the report's own "Re-running the audit" section
  has the trigger conditions.

- [ ] **`bin/_task_runtime.py` not in the tool inventory.** `gen_tool_inventory.py`
  skips leading-underscore modules, so `_task_runtime.py` (reader of
  `M3_TASK_LOG_FILE`) has no `docs/tools/` entry. Either rename it, or teach the
  generator to include underscore-prefixed modules.

- [ ] **`m3-core-rs` extraction vs. plan §9.4.** The plan assumed the Rust workspace
  would be extracted to a *public* repo at the Phase 2→3 boundary. It was — but the
  earlier private `m3-memory-rs` repo (a full m3-memory copy + the workspace) still
  exists and is now redundant. Decide whether to delete/archive it.

- [ ] **`m3-core-py` async `Dispatcher` binding.** The PyO3 surface exposes
  `estimate_tokens` + `DispatcherConfig` but not the async `Dispatcher` itself
  (pyo3 + async + generics was deferred as a rabbit hole). Only needed if Python
  should *drive* the dispatcher rather than just configure it.

- [ ] **crates.io publishing.** Per plan §9.5, no rush — a crate publishes only
  after one stable release cycle. Likely order: `m3-error` → `m3-hash` →
  `m3-vector` → `m3-dispatcher` → backends. `m3-core-py` may never publish to
  crates.io (ships as a PyPI wheel instead).
