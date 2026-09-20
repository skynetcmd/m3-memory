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

- [x] **GLiNER ONNX model export.** Exported `urchade/gliner_small-v2.1` to
  `repo/_assets/models/gliner/v1.onnx` + `tokenizer.json` with checksums.
  (Completed 2026-05-23).

- [ ] **Route shadow-mode corpus.** `auto_route.py` runs `M3_ROUTE_SHADOW_MODE=log`,
  comparing the Rust `decide_route` against Python `decide_branch`. But there is no
  10k-query corpus to *evaluate* the disagreement rate against (plan §4c.5 requires
  one before any cutover). Sample queries disagree 4/4 — expected, since the Rust
  decider sees only the query (no `candidates` signals) and uses a different branch
  vocabulary. Next: collect shadow-mode logs over real traffic, build the corpus,
  measure the rate. No cutover until then.

  ⚠ **"No cutover until then" no longer holds — the cutover shipped anyway.**
  `enforce` is implemented and defaults ON wherever the native core is installed
  (see the Route cutover item below). So this corpus is no longer a gate ahead of
  a change; it is now *validation of a decider already serving traffic*, which
  makes it more urgent, not less. Note also that `log` mode — the thing that
  produces the corpus — is no longer the default, so real traffic is not
  currently generating comparison logs on a native install.

---

## Decisions deferred to a human

- [ ] **Embedded-embedder default switch.** The in-process llama.cpp path
  (`M3_EMBED_GGUF`) is verified at cosine ≈ 0.996 against the
  `bge-m3-GGUF-Q4_K_M.gguf`-tagged rows. The LM Studio `text-embedding-bge-m3`
  rows carry a *different tag*. Making the embedded path the *default* embedder
  would change which tag new rows get. It is intentionally opt-in. Decision
  needed: leave opt-in, or re-embed the corpus under one tag.

  ⚠ RE-MEASURE BEFORE DECIDING — the counts in the original note are stale, and
  the framing has since been partly answered by code. Measured on the reference
  install 2026-09-20: `memory_embeddings` holds 19,413 `text-embedding-bge-m3`
  and 3,528 `bge-m3-GGUF-Q4_K_M.gguf`; `entity_embeddings` is the reverse
  (6,226 GGUF, 1 LM Studio). So the LM Studio side is the bulk of memory rows
  and the GGUF side is the bulk of entity rows — a switch strands neither
  wholesale, but it does keep splitting both tables.

  `bin/doctor/embed_space_probe.py` now collapses both tags to one
  cosine-comparable FAMILY (`_family`) and reports
  `embed-space: ok (single space: bge-m3)`. That is a deliberate judgement that
  the two are same-dimension and comparable — read it as m3's answer to
  "verify parity first", not as evidence the tags were unified. Do NOT close
  this item on the strength of that green line alone (it was misread that way
  once); the tag split is real and visible in the tables above.

- [x] **Route cutover.** _Shipped_ — both stated gates are resolved in code.
  `M3_ROUTE_SHADOW_MODE=enforce` is implemented (`bin/auto_route.py:307`) and is
  now the **default whenever `m3_core_rs` is importable** (`:51`); it falls back
  to `off` without the native core. The branch-name mapping exists as
  `_map_rust_to_py_branch` (`:214`), and enforce keeps the post-retrieval sharp
  spike check ahead of the Rust decision so behaviour matches the Python decider
  on that branch.

  ⚠ This item still read "reserved but deliberately unimplemented" long after
  the cutover had shipped AND been made the default — worth noting because the
  corpus item below, which was supposed to GATE this, is still open. The cutover
  did not wait for it. If that ordering was intentional, say so there; if not,
  the corpus is now measuring a decider that is already live.

- [ ] **`m3-rank` disposition — DECIDED: drop, but it is not a clean excision.**
  The crate's `fuse` is two-list rank-fusion; m3-memory's FTS5+vector merge is a
  per-row scoring loop with no two lists to fuse. Confirmed unused 2026-09-20 —
  `fuse`, `fuse_then_mmr`, `RankRow` and `RankSource` each have **zero** call
  sites in `bin/` and `tests/`. The live hybrid path is
  `m3_core_rs.rank_hybrid_packed` (`bin/memory/search.py:1339`), which does NOT
  come from this crate.

  ⚠ The TODO framed this as "drop it from the workspace", which understates the
  blast radius. `m3-rank` is wired into the PyO3 layer (14 references in
  `crates/m3-core-py/src/lib.rs`, plus `pub use m3_rank`), and **`RankRow` is one
  of the 47 symbols the built wheel exports**. Removing it therefore changes the
  public API surface, not just an internal dependency — it needs a version bump
  and a note, and must not ride along in an unrelated release.

  Sequence when it is done: remove the PyO3 bindings and `pub use` first
  (that is what drops the export), then the workspace member and the
  `m3-core-py` dependency, then verify the export list shrinks by exactly the
  expected symbols. Keep `rank_hybrid*` — different code, actively used.

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

- [x] **Write-path chunking + mean-pool — MEASURED, NOT WORTH OXIDIZING.**
  _Measured 2026-09-20._ Both are pure Python on the per-write path and both
  look like obvious candidates. Neither is:

  | path | input | cost | share of the embed it precedes |
  |---|---|---|---|
  | `_chunk_for_sliding_window` | 400 KB prose (156 windows) | 0.17 ms | 0.01% |
  | `_chunk_for_sliding_window` | 400 KB dense CJK+UUID | 1.64 ms | 0.11% |
  | `_mean_pool` | 32 sub-chunks × 1024-dim | 0.75 ms | <1% |

  Every window costs one embed (~10 ms native, ~300 ms over HTTP), so the
  chunker is three to four orders of magnitude below the work it schedules.
  `estimate_tokens` — the hot inner call — is deliberately pure arithmetic
  (`max(bytes/3, chars)`), not a tokenizer, so there is no expensive kernel
  hiding inside it either.

  ⚠ Recorded so this is not re-proposed from inspection. Both were proposed on
  the strength of "nested Python loop on the write path" and both died to a
  one-minute measurement (§12c: prefer the cheap measurement over the plausible
  model). The write path is embed-bound; oxidizing around that does nothing.

- [x] **Candidate-Assembly Loop Oxidation.** Refactored `search.py` to use
  `rank_hybrid_packed` in Rust, eliminating thousands of dictionary allocations
  per search and moving content-dedup + MMR to the Rust core.
  (Completed 2026-05-23).

- [x] **Entity Resolution Speedup.** Moved 3-tier entity resolution (Exact →
  Token-Jaccard → Embedding) to use Rust-accelerated `token_jaccard_batch` and
  `cosine_batch`. (Completed 2026-05-23).

---

## Hygiene / smaller items

- [x] **Benchmark Runners.** Ported `bench_memory.py` to `bin/`; other
  benchmark harnesses are maintained internally. (Completed 2026-05-23).

- [ ] **Benchmark Datasets.** Restore `repo/data/longmemeval/` datasets for
  1M+ row evaluations. (Still missing from the primary repositories).

- [x] **macOS Wheel Automation.** _Done_ — `m3-core-rs`'s `release.yml` builds
  `macos-metal` on `macos-14` and publishes the wheels with the tagged release
  (v2026.9.16 shipped 4, one per interpreter). Xcode/Rust are no longer needed
  to install on macOS.
  ⚠ **Apple Silicon only** (`macosx_11_0_arm64`), not the "Apple Silicon +
  Intel" the original item asked for. An x86_64 mac still falls back to a source
  build. Reopen as a separate item if Intel macs need to be supported.

- [ ] **Full env-var reconcile re-sweep cadence.** `docs/tools/ENV_VAR_RECONCILE_REPORT.md`
  was fully re-swept 2026-05-14 (all three groups). Re-run when new `M3_*` vars are
  added or `INDEX.md` is regenerated — the report's own "Re-running the audit" section
  has the trigger conditions.

- [x] **`bin/_task_runtime.py` in tool inventory.** Generator tweaked to mark
  it private; documentation entry now generated correctly.
  (Completed 2026-05-23).

- [ ] **`m3-core-rs` extraction vs. plan §9.4.** The plan assumed the Rust workspace
  would be extracted to a *public* repo at the Phase 2→3 boundary. It was — but the
  earlier private `m3-memory-rs` repo (a full m3-memory copy + the workspace) still
  exists and is now redundant. Decide whether to delete/archive it.

- [ ] **`m3-core-py` async `Dispatcher` binding.** The PyO3 surface exposes
  `estimate_tokens` + `DispatcherConfig` but not the async `Dispatcher` itself
  (pyo3 + async + generics was deferred as a rabbit hole). Only needed if Python
  should *drive* the dispatcher rather than just configure it.

- [ ] **Dispatcher latency histogram is a §3 false signal — fix or stop publishing.**
  `DispatcherStats.p50_ms` / `p99_ms` are hardcoded `0.0`
  (`m3-dispatcher/src/lib.rs:471-472`, flagged by two in-code TODOs at `:94`/`:96`)
  and then **served on the embed server's metrics endpoint**
  (`m3-embed-server/src/server.rs:282-283`). Verified live 2026-09-20:
  `GET :8082/metrics` -> `{"in_flight":0,"p50_ms":0.0,"p99_ms":0.0,"queue_depth":0}`.

  A metric that always reads 0.0 is indistinguishable from a genuinely idle
  server, so it reads as observability while providing none — the §3 "declared
  limit with no enforcement site" pattern, on a production endpoint. This
  matters more since 3.9.16 made the per-wheel `streams` default a stated
  throughput trade whose named signal is this same endpoint: an operator told to
  watch latency here will watch a constant.

  Either wire the histogram, or drop the two fields from the JSON until it is
  wired. Dropping is the smaller change and is honest; keeping a 0.0 is not.
  ⚠ `in_flight` and `queue_depth` ARE real — do not remove those.

- [ ] **crates.io publishing.** Per plan §9.5, no rush — a crate publishes only
  after one stable release cycle. Likely order: `m3-error` → `m3-hash` →
  `m3-vector` → `m3-dispatcher` → backends. `m3-core-py` may never publish to
  crates.io (ships as a PyPI wheel instead).
