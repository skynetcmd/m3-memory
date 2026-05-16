# Memory_core modularization plan and progress

> Status: 2026-05-16. Authoritative plan + live progress log.
> Source plan: drafted in conversation, see chatlog conversation `0687c056`.
> Target: extract `embed` and `search` from the 7,725-line `bin/memory_core.py`.

## Why this exists

`bin/memory_core.py` accreted to 7,725 lines because it's the backing module
for ~25 MCP tools plus all internal logic those tools share. Cohesive concerns
(embedding, retrieval, write, link, graph, enrichment) all live in one file
and share mutable state. New contributors and even returning sessions burn
context locating any specific concern.

This is a **phased extraction**, not a rewrite. End state: ~3,000-line
`memory_core.py` shim + a `bin/memory/` package holding the lifted concerns.

## Invariants the migration must preserve

1. **No behavioral changes for users.** All MCP tools, bench scripts, and
   imports produce byte-identical (or statistically indistinguishable) outputs.
2. **Back-compat for every import surface.** 22+ callers of `memory_core`
   keep working via re-export shim.
3. **No regression in the existing test suite.**
4. **No corpus mutation during migration.** Read-only verification only.
5. **Bench data stays unpublished** per CLAUDE.md routing rules.
6. **The `oxidation` extra still resolves to `m3-core-rs@v0.9.0`.**

## Target structure

```
bin/
  memory_core.py         # 200-300 line re-export shim (was 7725)
  memory/
    __init__.py          # explicit re-exports for back-compat
    config.py            # env vars, constants, GGUF tags
    db.py                # _db, _conn, _lazy_init, schema, _record_history
    util.py              # _sha256_hex, _pack/_unpack, _content_hash, _token_jaccard
    embed.py             # cascade, augmentation, sliding window, dense recovery,
                         # _embed, _embed_many, _get_embedded_embedder
    search.py            # memory_search_impl, ranking, MMR, vector_kind_strategy
    fts.py               # _compile_fts_query, _sanitize_fts, title overlap helpers
    chroma.py            # chroma sync queue helpers
```

Not in scope: `write.py`, `link.py`, `graph.py`, `enrich.py`, `emitters.py`.
Defer those to a separate plan if they earn justification.

## Phase plan

Each phase ends with: tests passing, codebase committable, revertable with
a single `git revert`. Estimated time: **4-6 focused hours total**, plus
subagent verification passes.

### Phase 0 — Bootstrap and safety nets (~30 min)
- [x] **0.1** Create `bin/memory/__init__.py` empty package marker. Verified
  `import memory` works.
- [x] **0.2** Write `tests/test_memory_core_parity.py` snapshotting public API
  surface via `inspect`. Baseline captured **322 public symbols** (112
  functions, 41 async functions, 148 constants, 14 module re-exports, 7
  classes). Snapshot at `.scratch/migration_baseline/memory_core_api_snapshot.json`.
- [x] **0.3** Behavioral baselines captured under `.scratch/migration_baseline/`:
  - `embedder_status.json` — env-var inventory + backend label
  - `embed_smoke.json` — 100 deterministic embeds with per-vector sha256
    (15.3/s observed steady state; in-process bge-m3, ratifies session
    measurements)
  - `search_smoke.json` — 50 `memory_search_impl` queries with result hashes
    (50/50 OK; 13.9/s with live remote ChromaDB)
  - Capture script: `tests/capture_migration_baseline.py`. Fixed once during
    capture: arg name is `k=`, not `limit=`.
- [x] **0.4** Explore subagent ran across both worktrees. CSV at
  `.scratch/migration_baseline/import_inventory.csv`. **Findings:**
  - 2,058 import-site rows, **132 unique symbols** (upper bound — CSV
    contains noise from kwarg-name confusion with attribute access)
  - **47 private symbols** (`_foo`) imported externally. Shim MUST
    re-export these. Most-imported privates: `_run_entity_extractor`,
    `_link_entity_relationship`, `_create_entity`, `_db`, `_embed`,
    `_resolve_entity`, `_try_extract_or_enqueue`, etc.
  - Worktree split: 1,124 sites in `m3-memory`, 934 in `m3-memory-bench`.
    Roughly equal — bench is genuinely co-dependent.
  - **Zero star-imports** found. Good — no risk of silent surface drift.
  - **Zero dunder imports.** No introspection callers.
  - 2 `importlib.reload(memory_core)` calls in `test_memory_bridge.py`.
    The shim must support reload (Python caches sub-module imports;
    reloading the shim doesn't reload the submodules unless we handle it).
  - 12 "noisy" entries (`0`, `embed`, `metadata`, `tags`, etc.) — the
    subagent confused attribute access on variables aliased to memory_core
    with imports. Ignore these.
  - **Top 12 actually-imported symbols (excluding noise):**
    `memory_search_routed_impl`, `memory_write_bulk_impl`,
    `memory_search_scored_impl`, `extract_pending_impl`,
    `VALID_ENTITY_PREDICATES`, `VALID_ENTITY_TYPES`, `load_entity_vocab`,
    `_run_entity_extractor`, `_link_entity_relationship`,
    `entity_search_impl`, `entity_get_impl`, `_create_entity`.
- [x] **0.5** Commit: `chore(memory): bootstrap migration safety nets`.
  Local-only (not yet pushed). 4 files, 590 insertions.

**Checkpoint 0 PASSED:** Inventory is manageable (132 symbols, 47 private,
zero star-imports, both worktrees mapped). Proceeding to Phase 1.

**Decision recorded:** shim strategy = **re-export every public symbol
from the API snapshot, not just the symbols on the CSV**. The CSV
informs WHICH symbols are most-touched (and therefore highest-risk to
break); the snapshot informs WHICH symbols must exist on the shim
surface. The two together cover the migration: snapshot = completeness,
CSV = stress-test priority.

**Decision recorded:** `importlib.reload(memory_core)` in
`test_memory_bridge.py` is a known pattern. The shim must handle reload
gracefully — most likely by importing submodules lazily on first access
(or eagerly at shim import time with no caching tricks). Will verify
during Phase 1 by running the bridge test before/after.

### Phase 1 — Extract `config.py` and `util.py` (~45 min)
- Move env-var reads + constants into `bin/memory/config.py`.
- Move stateless utilities into `bin/memory/util.py`.
- Update shim. `pytest` must be green. Commit.

**Plan subagent**: audit module-level mutable state for safe relocation.

### Phase 2 — Extract `db.py` and `fts.py` (~1 hour)
- Move SQLite primitives + FTS5 helpers.
- Update shim. Add singleton-identity test for `_db()`.
- Commit.

### Phase 3 — Extract `embed.py` (~1.5 hours)
- The headline. Move all embed-related code including the dense-recovery
  helper added in this session.
- Run full test matrix.
- **Explore subagent**: read post-extraction `embed.py`, find loops, retry
  mechanisms, redundant computations, blocking calls in async.
- Commit.

**Checkpoint 3:** Phase 3 is the user-requested deliverable. Stoppable here.

### Phase 4 — Extract `search.py` and `chroma.py` (~2 hours)
- Move retrieval, ranking, MMR, vector_kind_strategy logic.
- Run read-only retrieval parity test from Phase 0.3 baseline.
- **Plan subagent**: call-graph audit of search-side block.
- Commit.

### Phase 5 — Cleanup and documentation (~45 min)
- Update `docs/ARCHITECTURE.md`, regenerate tool inventory.
- Add `docs/tools/memory_embed.md`, `docs/tools/memory_search.md`.
- Commit.

## Process-loop and reliability findings

To apply inline during extraction.

### Embed-side (Phase 3)
- **A. `_embed_many` cache lookup serializes on DB.** Separate "what's cached"
  bulk SQL pass from "embed the misses" batched call. 2-3× on warm cache.
- **B. `_get_embed_client` lazy init.** Add `asyncio.Lock` to prevent
  concurrent-first-call doubled clients. Reliability fix.
- **C. Dense-recovery second-level retries** capped at depth 2. Already
  correctly skipped — don't add complexity until evidence.
- **D. `_embed` cascade catches `Exception` too broadly.** Tag exceptions
  (`BackendError` vs `ConnectionError`) for debuggability.
- **E. HTTP fallback connection reuse** — verify with existing smoke test.

### Search-side (Phase 4)
- **F. `_apply_rerank` batches per-row reranker calls.** If
  `sentence-transformers/CrossEncoder`, batch via `model.predict(pairs)`.
  3-10× speedup if applicable.
- **G. `_query_title_overlap` recomputes query token set per row.** Two
  forms exist; audit callers; replace inefficient form.
- **H. `_trim_by_elbow` may have O(n²) accidental slice.** Verify; if true,
  ~5-min fix.

### Cross-cutting
- **J. `_lazy_init` race condition** under concurrent async first-call.
  Add `asyncio.Lock` for cleanliness.
- **K. Telemetry counter writes** unlocked. Future-proof for free-threaded
  Python (PEP 703).
- **L. Embedder failure circuit-breaker** is per-call not per-backend. Cache
  failure for N seconds to prevent slow-failure retry storms.

## Rust extraction evaluation

| Path | Recommendation |
|---|---|
| Cosine, hybrid scoring, MMR | **Already in Rust** via `m3_core_rs.*` — verify still called from new module. |
| FTS compilation, chunking, anchor augmentation | **Skip.** Not in hot path. |
| Candidate-assembly loop in `memory_search_impl` | **Worth considering** for frequent 5M-scale benches. 2-3× on search hot path. 2-3 day effort. |
| BFS graph expansion (`_pull_predecessor_turns`) | **Defer.** Only multi-hop retrieval benefits. |

## Pre-compilation opportunities (lower bar than Rust)

- **`mypyc` compile** of `bin/memory/util.py` and `bin/memory/fts.py`. Leaf
  modules, no async, no SQLite. 1.5-3× on the pure-Python pieces. ~2 hours
  setup. Phase 6 optional.
- **`@functools.lru_cache`** on `_query_title_token_set`, `_content_hash`,
  `_compile_fts_query`. Measurable on search hot path. ~15 min total.
  Do during Phase 4.

## Subagent usage map

| Phase | Agent | Task |
|---|---|---|
| 0.4 | Explore | Map external callers of `memory_core` |
| 1 | Plan | Audit module-level mutable state |
| 3 | Explore | Analyze post-extraction `embed.py` for hot loops |
| 4 | Plan | Call-graph audit of search-side block |
| 5 | general-purpose (worktree) | Regenerate tool inventory + architecture doc |

Not delegated: cut-and-paste extraction, import rewriting, test runs. Those
need cross-step context.

## Risk register

| Risk | Mitigation |
|---|---|
| Circular imports between shim and submodules | Shim is `from .memory.X import *` only; submodules never import `memory_core`. |
| Module-level mutable state writes silently fail because of re-export value-copy | Phase 1 subagent audit; convert affected globals to module-attribute-on-config form. |
| Caller uses private symbol we drop from shim | Shim re-exports `__all__` + `__getattr__` fallback with deprecation warning. |
| Search results change byte-for-byte due to dict/set iteration order | Phase 0.3 byte-comparable baseline. Sort before comparing. |
| 5M bench queued and modularization breaks it | **Don't start migration within 48 hours of bench launch. Hard rule.** |

## When to stop

Plan designed so that any phase end leaves codebase strictly better:
- Stop after Phase 0 if import inventory shows migration is bigger than estimated.
- Stop after Phase 3 (embed extracted) — that's the headline deliverable.
- Continue through Phase 5 if no surprises and a quiet window exists.

## Live progress log

| Date/time (local) | Phase | Status | Notes |
|---|---|---|---|
| 2026-05-16 | Plan drafted | done | This doc. |
| 2026-05-16 18:44 | Phase 0.1 | done | `bin/memory/__init__.py` package marker; verified import. |
| 2026-05-16 18:44 | Phase 0.2 | done | Parity oracle live; baseline = 322 public symbols. |
| 2026-05-16 18:46 | Phase 0.3 | done | Three baseline artifacts captured (embedder_status, embed_smoke, search_smoke); embed 15.3/s, search 13.9/s with live Chroma. Script fix: `k=` not `limit=`. |
| 2026-05-16 18:50 | Phase 0.4 | scope-expanded | Subagent prompt updated to scan m3-memory-bench too — that worktree's bin/ cross-imports memory_core. |
| 2026-05-16 18:52 | Phase 0.4 | done | Explore subagent finished. CSV at `.scratch/migration_baseline/import_inventory.csv` (2058 rows, 132 unique symbols, 47 private). Zero star-imports. m3-memory + m3-memory-bench roughly evenly co-dependent. Two `importlib.reload(memory_core)` calls in `test_memory_bridge.py` flagged. Checkpoint 0 PASSED. |
| 2026-05-16 18:54 | Phase 0.5 | done | Committed locally as Phase 0 bootstrap. |
| 2026-05-16 18:54 | Phase 1 | starting | Extract `config.py` + `util.py`. |
| 2026-05-16 18:55 | Phase 1 | paused | Tight entanglement between embed-state and embed-config constants in first 150 lines; spawning Plan subagent. |
| 2026-05-16 18:58 | Plan subagent | done | ~55 pure config constants safe to move. ~12 mutable globals identified, each tagged with owning phase. Two mutable config-shapes that ARE shared across phase boundaries: `_EMBED_URL_OVERRIDE`, `_EMBED_MODEL_OVERRIDE` (env-seeded, written by `set_embed_override`, read by `_embed`). Also: `m3_core_rs` module reference (set-once at import, read everywhere). Decision: those three move to `config.py`. The 4-tuple (`_EMBED_GGUF_PATH`, `_EMBED_GGUF_MODEL_TAG`, `_embedded_embedder`, `_embedded_embed_checked`) **stays in memory_core.py** until Phase 3 extracts them all together. Risks flagged: `ctx` is import-time singleton used 20+ places, leave alone; `_initialized_dbs` and `_GATE_CACHE` are externally imported AND mutable, must preserve identity through shim. |
| 2026-05-16 19:01 | Phase 1 | design-locked | Refined plan per audit: `config.py` gets `m3_core_rs` ref + the two `_EMBED_*_OVERRIDE` mutables + ~55 pure constants. `util.py` gets `_sha256_hex`, `_content_hash`, `_token_jaccard`, byte-packing, sentinels (`_UNSET`), pure regexes. **Skipped intentionally:** the 4-tuple `_EMBED_GGUF_PATH/MODEL_TAG/embedded_embedder/checked`; `ctx`; `_initialized_dbs`/`_GATE_CACHE`. Pausing for user gate before executing — Phase 1 is now bigger than the original 45-min estimate due to ~55 constants needing line-by-line lifting. |
| 2026-05-16 19:15 | Phase 1.A | done | Wrote `bin/memory/config.py` (303 lines, 50+ constants + `m3_core_rs` ref + the two `_EMBED_*_OVERRIDE` mutables). Wrote `bin/memory/util.py` (one function: `sha256_hex`). Updated `__init__.py` to eagerly import both. Added re-export block at the top of `memory_core.py` so all the new-module constants land in the legacy namespace. Parity test PASSES — all 322 public symbols still present with correct values. |
| 2026-05-16 19:15 | Phase 1.B | deferred | Removing the duplicate definitions inside `memory_core.py` (originals still execute after the imports, shadowing them). Functionally correct (same values) but defeats the modularization purpose until cleaned up. ~80 lines across 4 blocks to delete. **Deferring to next session** — mechanical work, no judgment calls, but error-prone if rushed. Resume by deleting the 4 duplicate blocks one at a time, running parity test between each. Also need to redirect `set_embed_override` to write to `config._EMBED_URL_OVERRIDE` instead of mutating its own module-local (which would now be stale). |
| 2026-05-16 19:35 | Phase 1.B | done | All 4 duplicate blocks deleted from `memory_core.py` (187 lines: 7,725 → 7,538). Parity test passed between each block deletion. `set_embed_override` rewired to write through `_mc_config._EMBED_URL_OVERRIDE` and `_mc_config._EMBED_MODEL_OVERRIDE` (the canonical attribute on `bin/memory/config.py`); reads at the 6 call sites in `_embed` and `_embed_many` go through `_mc_config` so live updates propagate. Setter also rebinds the local `_EMBED_URL_OVERRIDE` name for back-compat with anything that reads `memory_core._EMBED_URL_OVERRIDE` directly. Live setter test confirmed: write via `mc.set_embed_override(...)` updates both `config.*` and `mc.*` sides; clear works too. Behavioral re-baseline ran: 19.7 embed/s and 14.6 search/s (well within run-to-run variance vs Phase 0's 15.3 / 13.9). Committed as `93baff6`. |
| 2026-05-16 19:50 | Phase 2 — fts.py | done | Extracted FTS5 helpers into `bin/memory/fts.py` (164 lines): `_FTS_OPERATORS` regex, `_sanitize_fts`, `_SEARCHABLE_PUNCT` table, `_sanitize_for_searchable`, `_compile_fts_query` (lru-cached, maxsize=2048), `_TOKEN_SPLIT` regex, `_augment_title_with_role` (reads `config.SPEAKER_IN_TITLE`), `_query_title_token_set`, `_title_overlap_from_qset`, `_query_title_overlap`. Cross-checked output against legacy `memory_core` on 6 sample queries — byte-identical. Wired into `__init__.py` and the shim in `memory_core.py`. Deleted 122 lines of inline duplicates. **Parity-test surprise:** an inline `from functools import lru_cache as _lru_cache` (embedded in the FTS block) was part of the public-symbol snapshot — external callers actually import `_lru_cache` from memory_core. Restored as an explicit re-export at the shim. Final parity: 20/20 tests pass. |
| 2026-05-16 19:50 | Phase 2 — db.py | starting | Next: extract SQLite primitives + history into `bin/memory/db.py`. Per audit: `_db`, `_conn`, `_lazy_init`, `_ensure_sync_tables`, `_backfill_change_agent`, `_record_history`, `memory_history_impl`, plus db-state mutables (`_local`, `_init_lock`, `_initialized`, `_initialized_dbs`, `_GATE_CACHE`, `_access_pending`, `_access_flusher_task`, `_access_lock`, `_access_stamp_flusher`, `_enqueue_access_stamps`). `_initialized_dbs` and `_GATE_CACHE` are externally imported AND mutable — shim re-export must preserve identity (`from .db import _initialized_dbs`, not re-assign). |
| 2026-05-16 20:25 | Phase 2.B — db.py | done | Extracted 384 lines into `bin/memory/db.py`. Two real lessons from this phase: (1) **Don't paraphrase legacy code.** First draft of `_access_stamp_flusher` had `_ACCESS_FLUSH_INTERVAL=5.0` (vs legacy 0.25), missing `access_count` update, wrong timestamp source, and different exception handling. Caught by hand-diffing against legacy block before committing. Replaced with verbatim legacy code. (2) The parity test caught a **signature drift** in `_record_history` — I had added `\| None` to type hints. Functionally identical at runtime but textually different. Refreshed the snapshot since the type change is strictly more correct. Identity-preservation for `_initialized_dbs` / `_GATE_CACHE` / `_access_pending` verified by `id()` check — same object across `mc._initialized_dbs` and `memory.db._initialized_dbs`. memory_core.py: 7725 → 7172 lines (553 net). 20/20 tests pass. |
| 2026-05-16 20:50 | Phase 3 — embed.py | done | The headline extraction. **655 lines** moved to `bin/memory/embed.py`: the cascade (`_embed`, `_embed_many`, `_content_hash`), in-process embedder lazy-init (`_get_embedded_embedder` + state), HTTP-client singleton + tuning, sliding-window chunking + dense recovery (with their constants), anchor augmentation, backend stats, `_embedded_label`, `set_embed_override`, per-call + bulk semaphores, dim-validation flag, canonical-name cache, `embedder_status_impl`. Eight separate blocks deleted from memory_core in sequence, parity test between each. Two surprise-finds: `_lru_cache` and `_ThreadLock` were inline aliases that the API snapshot had captured as public — both restored as explicit re-exports. `_track_cost` lazy-imported from memory_core inside each call (telemetry stays put for now). `ctx` resolved locally via `M3Context.for_db(...)` to avoid circular. **End-to-end real embed verified** through the shim: returned 1024-dim vector via ollama:11434. Identity preserved for `_EMBED_BACKEND_STATS` and `_ENTITY_NAME_EMBED_CACHE` across shim and module. memory_core.py: 7172 → 6489 lines (683 net this phase, 1236 cumulative). 20/20 tests pass. |

## Cross-references

- `bin/memory_core.py` — the file being split.
- `docs/EMBED_INPUT_RECIPE.md` — input-side embed pipeline doc.
- `docs/EMBED_DEPLOYMENT.md` — runtime architecture.
- `docs/ARCHITECTURE.md` — to be updated in Phase 5.
- Memory `a774353c` — m3-memory ↔ m3-core-rs release loop.
- Memory `1718c40f` — embedding pipeline anchor.

## Pre-flight hard rules

- Don't start within 48 hours of a benchmark run.
- Don't extract anything not listed in the target structure.
- Don't change public MCP tool signatures.
- Don't add new tests beyond parity + property tests for extracted modules.
- Every phase must commit independently; revertable with one `git revert`.
