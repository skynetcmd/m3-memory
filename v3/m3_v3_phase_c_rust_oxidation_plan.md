# M3-v3 Phase C — Rust Oxidation Implementation Plan

**Scope:** the two remaining Milestone 4 Rust items, both in the separate repo
`github.com/skynetcmd/m3-core-rs` (built from a local clone, base `main` @
`c34fa4a`):

1. Oxidize the Adaptive Governor cooldown/threshold checks into `m3-core-py`.
2. Create the `m3-ingest-rs` crate for the filesystem-watcher directory walk +
   hash checks.

Plus the wheel rebuild (local, via maturin) and — only on explicit user
approval — publish.

**Environment verified:** rustc 1.94.1, cargo 1.94.1, maturin 1.13.3, both
clones present. `m3_core_rs` wheel installed at the user site-packages.

**Governing conventions (from reading the repos):**
- Rust crates use workspace inheritance (`version.workspace = true`, etc.) —
  see `crates/m3-hash/Cargo.toml`.
- PyO3 bindings live in `crates/m3-core-py/src/lib.rs`: a `#[pyclass(name="X")]`
  wrapper holding `inner: <crate>::Type`, a `#[pymethods] impl`, and a line in
  the `#[pymodule] fn m3_core_rs(...)` registrar (`m.add_class::<PyX>()?;`).
- Python integration convention (mirror `migration_lock` in `bin/m3_sdk.py`):
  `if not M3_CORE_RS_DISABLE: try: import m3_core_rs; if hasattr(...): use native
  … except Exception: fall through to the pure-Python path`. The Python path
  always stays as a working fallback. **Never** make the Rust path mandatory.
- m3-memory CLAUDE.md rules still apply to the m3-memory-side wiring commits
  (no Co-Authored-By; regenerate tool catalog only if tool specs change — N/A
  here; bench data stays private).

---

## Item 1 — Adaptive Governor oxidation

### Current state (pure Python)
`bin/m3_sdk.py:62 get_governor_pacing(telemetry: dict) -> dict` plus module
globals `INITIAL_LIMIT` / `LIMIT_THRESHOLD` (env-driven, clamped, with an
`initial < limit` sanity fix) and `_LAST_USER_INTERACTION` /
`register_user_interaction()`. Logic is a pure decision function over
`(load, elapsed_since_interaction, thresholds)` returning a mode +
delay dict. **No I/O, no allocation pressure** — it is cheap already.

### Honest assessment / risk note
This function is **not** a hot path or CPU-bound — it runs once per task-pacing
decision, not per row. Oxidizing it is **spec-driven** (M4 checklist line 268),
not perf-driven. The win is consistency (one governor implementation, callable
from future Rust daemons) and removing per-call Python clamp/branch overhead in
tight governor loops. We implement it cleanly but should **not** claim a
measurable latency win in the commit message — claim correctness parity +
single-source-of-truth.

### Design
New crate `crates/m3-governor/` (pure logic, no PyO3 — mirrors how `m3-graph`
holds `CircuitBreaker` and `m3-core-py` wraps it):

```
crates/m3-governor/
  Cargo.toml            # workspace-inherited; deps: none (or m3-error only)
  src/lib.rs            # GovernorConfig, PacingMode enum, Pacing struct, decide()
  tests/parity.rs       # table-driven parity vs the Python truth table
```

- `GovernorConfig { initial_limit: u32, limit_threshold: u32 }` with a
  `::new(initial, limit)` that applies the SAME clamps as Python
  (`initial ∈ [10,99]`, `limit ∈ [20,100]`, and the `initial >= limit && limit
  != 100 → initial = limit - 5` sanity fix). One source of truth for the clamp.
- `enum PacingMode { Halted, Throttled, Tapered, Continuous }`.
- `struct Pacing { mode: PacingMode, background_delay: Option<f64>,
  interactive_delay: f64 }`.
- `fn decide(&self, load: f64, elapsed_since_interaction: f64) -> Pacing` — the
  exact branch ladder from `get_governor_pacing` (critical → throttled →
  normal[<30s halted / <60s tapered / else continuous]).

PyO3 binding in `m3-core-py/src/lib.rs`: `#[pyclass(name="Governor")]
PyGovernor { inner: m3_governor::GovernorConfig }` with:
- `#[new] fn new(initial_limit, limit_threshold)`,
- `fn decide(&self, load: f64, elapsed: f64) -> PyObject` returning a dict that
  is **key-for-key identical** to the Python return (`background`,
  `background_delay`, `interactive_delay`) so the Python call site is a drop-in.
- register `m.add_class::<PyGovernor>()?;` and add `m3-governor` to the
  `[workspace] members` list + `m3-core-py` deps.

### Python rewiring (m3-memory side, `bin/m3_sdk.py`)
Refactor `get_governor_pacing` to try the native path first, identical-output
fallback to the existing Python body:
```python
def get_governor_pacing(telemetry: dict) -> dict:
    load = max(telemetry.get("cpu_total",0.0), telemetry.get("ram_total",0.0), telemetry.get("gpu_total",0.0))
    elapsed = time.time() - _LAST_USER_INTERACTION
    if not M3_CORE_RS_DISABLE:
        try:
            import m3_core_rs
            if hasattr(m3_core_rs, "Governor"):
                gov = m3_core_rs.Governor(INITIAL_LIMIT, LIMIT_THRESHOLD)
                return gov.decide(load, elapsed)
        except Exception:
            pass
    # ... existing pure-Python ladder unchanged (the fallback) ...
```
Construction is cheap; if it ever shows up in profiles, cache one `Governor`
instance at module load guarded by the same thresholds.

### Tests
- Rust `tests/parity.rs`: a table of `(initial, limit, load, elapsed) ->
  expected mode/delays` covering every branch + the clamp edge cases
  (`initial>=limit`, `limit==100` disables critical mode).
- m3-memory side: a Python test that asserts native and fallback paths return
  **identical dicts** across the same table (run once with `M3_CORE_RS_DISABLE`
  unset, once set), so we prove the drop-in.

---

## Item 2 — `m3-ingest-rs` filesystem-watcher oxidation

### Current state (pure Python)
`bin/files_memory/walker.py` (303 lines): `walk()` does a recursive
`os.scandir` with gitignore-style include/exclude matching, per-entry
`entry.stat()`, and yields `WalkEntry(path, size, mtime, is_dir, ...)`. Hashing
+ "has this file changed since last index" lives in `ingest.py`/`staleness.py`
keyed off `(path, size, mtime)` and a content hash. The CPU cost on big trees is
the scandir+stat+hash sweep.

### Design
New crate `crates/m3-ingest/` (the spec name is `m3-ingest-rs`; crate package
name `m3-ingest` to match the `m3-<x>` workspace convention — directory mirrors
`m3-hash`):

```
crates/m3-ingest/
  Cargo.toml            # deps: walkdir or ignore (gitignore-aware), m3-hash (FIPS hash), m3-error, log
  src/lib.rs            # WalkConfig, Entry, walk_incremental(), changed-detection
  tests/parity.rs       # parity vs a fixture tree the Python walker also walks
```

- Use the `ignore` crate (ripgrep's gitignore engine) for include/exclude so we
  match the Python `_load_ignore_patterns` semantics without re-implementing
  glob matching; or `walkdir` + manual matching if we must mirror the exact
  pattern rules. **Decision to confirm during impl:** match Python's existing
  ignore semantics exactly (parity tests will catch divergence).
- `walk_incremental(root, patterns, prev: HashMap<path,(size,mtime,hash)>) ->
  Vec<Entry{path,size,mtime,is_dir,changed: bool}>` — does the scandir-equivalent
  walk, stats each entry, and flags `changed` by comparing `(size,mtime)` first
  (cheap) and only hashing when those differ (mirrors the Python staleness
  fast-path). Hash via `m3-hash` to stay FIPS-consistent with the rest of the
  workspace.
- Parallelism: `ignore::WalkParallel` or rayon over directory shards — but
  **bounded** to respect the M1 FD-semaphore philosophy (don't open unbounded
  files). Document the bound.

PyO3 binding: `#[pyclass(name="FsWalker")] PyFsWalker` exposing
`walk_incremental(root, patterns, prev_state) -> list[dict]`. Register it.

### Python rewiring (m3-memory side)
`bin/files_memory/walker.py walk()` gets a native fast-path with the SAME
`try m3_core_rs.FsWalker … except: pure-python` guard. The `WalkEntry`
dataclass shape must be preserved so `ingest.py` callers are untouched.

### Tests
- Rust parity test over a committed fixture tree (files with known
  size/mtime/content) asserting the entry set + changed-flags.
- m3-memory parity test: native vs Python `walk()` over `tests/` fixtures must
  yield the same `{path,size,is_dir}` set (mtime compared with tolerance).

---

## Item 3 — Wheel rebuild + publish

- **Rebuild (local, safe):** `maturin build --release` (or `develop --release`
  to install into the active venv) from `crates/m3-core-py`. Verify
  `python -c "import m3_core_rs; m3_core_rs.Governor; m3_core_rs.FsWalker"`.
- **Publish (OUTWARD-FACING — requires explicit user go-ahead):** follow
  `docs/PUBLISHING.md` in m3-core-rs. Do **not** run any publish/release step,
  tag, or push to the public `m3-core-rs` remote without in-the-moment user
  approval and the pre-push leakage/diff audit (m3-memory CLAUDE.md rules).

---

## Sequencing, risk, and verification

1. **Governor crate + binding + parity tests** (smallest, pure-logic, lowest
   risk) → build wheel locally → wire `bin/m3_sdk.py` → Python parity test.
   **Review checkpoint.**
2. **m3-ingest crate + binding + parity tests** (larger, has I/O + parallelism)
   → rebuild wheel → wire `bin/files_memory/walker.py` → Python parity test.
   **Review checkpoint.**
3. **Check off** M4 lines 268/269/271 in the master plan; Phase D benchmarks
   then measure any real-world delta from the ingest oxidation (the governor
   won't move retrieval numbers; the walker may move ingest wall-clock).

**Cross-repo commit hygiene:** m3-core-rs commits land on a `feature/` branch in
that repo; m3-memory wiring commits land on the current
`feature/m3v3-m5-sqlite-vec-dep` branch (or a fresh `feature/m3v3-m4-*` branch —
decide at impl time). Two repos = two PRs. Nothing is pushed to either public
remote without the per-CLAUDE.md audit + user confirmation.

**Fallback guarantee:** every oxidized path keeps its pure-Python implementation
as the `except`-branch fallback, gated by `M3_CORE_RS_DISABLE`. A missing/old
wheel must never break m3-memory — only make it slower.

---

## Phase D — benchmark results (pre-registered per DESIGN_PHILOSOPHIES §5)

Targets were registered *before* measuring. Results:

| Gate | Target | Result | Verdict |
|---|---|---|---|
| Retrieval do-no-harm | zero byte drift on `capture_retrieval_baseline.py` | byte-identical (60 queries × 3 variants), baseline captured on `main`, compared on the feature branch | ✅ PASS |
| Batch-hash speedup | native `hash_files` ≥ 2× serial Python on ≥ 200 files | 6.45× (500 files × 64 KiB), 6.96× (1000 × 128 KiB), parity OK | ✅ PASS |
| WriteQueueDaemon | concurrent wall-clock < per-write-commit baseline | ~100× **slower** on bursts (437 ms vs 5 ms / 200 writes) | ❌ FAIL → reverted |

### Why the WriteQueueDaemon was reverted (the instructive negative result)

The in-process `WriteQueueDaemon` failed because it sits in the gap between the
fast case and the slow case:

- **Intra-process bursts are already fast.** SQLite WAL on the single pooled
  `_db()` connection commits 200 rows in ~16 ms. The daemon's 100 ms
  aggregation window (× ⌈N/50⌉ batches) is pure added sleep latency — 4 batches
  × 100 ms = 400 ms, matching the measured 437 ms.
- **The real contention is multi-process.** A dedicated benchmark (W separate
  processes, each its own connection, same DB) reproduced the
  `database is locked` storm: 8 procs × 250 writes = 638 ms with 15 lock-retries
  at a 50 ms `busy_timeout`; 16 × 200 = 1073 ms / 93 retries at 20 ms. An
  in-process asyncio queue **cannot coordinate separate processes**, so it never
  reaches this workload.
- **m3 already handles the multi-process case.** Re-running the storm with m3's
  real `PRAGMA busy_timeout = 30000` (`sqlite_pragmas.py`) drove lock-retries to
  **zero** — writers wait politely instead of erroring. The residual throughput
  gap (per-write fsync across processes; serialized 1-writer batching is ~50×
  faster) is closed by the **existing** `memory_write_bulk_impl` /
  `memory_write_batch_impl` batch APIs.

**Lesson for a future engineer:** do NOT re-add an in-process write queue to
"scale concurrent writes." The intra-process path is WAL-fast already; the
slow path is cross-process and needs either a single cross-process writer
(daemon + IPC) or — the pragmatic answer m3 already ships — `busy_timeout` +
the bulk-write APIs for ingest sweeps.
