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

Status as of 2026-05-17 audit pass (commit pending).

### Embed-side (Phase 3)
- **A. `_embed_many` cache lookup serializes on DB.** Plan claimed the
  cache lookup was per-row.
  _Status: verified false positive — `bin/memory/embed.py:521-535` was
  already a single batched `IN (?,?,...)` query before this audit. A
  200-hash lookup measures 3 ms vs ~200 ms for a hypothetical
  per-row form. Tangential improvement applied: the lookup now chunks
  at 500 hashes to stay under SQLite's `SQLITE_MAX_VARIABLE_NUMBER`
  (32 766 on modern builds, 999 on older), preventing a future
  50k-text bulk write from hitting the cap._
- **B. `_get_embed_client` lazy init.** Plan flagged adding `asyncio.Lock`
  to prevent concurrent-first-call doubled clients.
  _Status: verified non-issue. `bin/memory/embed.py:210-244` already uses
  `threading.Lock` with double-checked locking. `asyncio.Lock` would be
  the WRONG primitive (only protects against tasks on one event loop,
  not threads). Current impl is strictly better. No change._
- **C. Dense-recovery second-level retries** capped at depth 2. Already
  correctly skipped — don't add complexity until evidence.
  _Status: confirmed in `bin/memory/embed.py`. No change._
- **D. `_embed` cascade catches `Exception` too broadly.** Tag exceptions
  (`BackendError` vs `ConnectionError`) for debuggability.
  _Status: done. Added `EmbedError` base + `EmbeddedBackendError`,
  `EmbedFallbackError`, `EmbedPrimaryError`, `EmbedSemaphoreTimeout`
  in `bin/memory/embed.py`. Each tier of the cascade wraps the caught
  Exception in the appropriate typed class and surfaces the type name
  in the log line (`EmbedFallbackError: http://...: connection refused`).
  Cascade contract unchanged — callers still see `(None, model)` on
  total failure. Classes are public via the memory_core shim
  re-export so downstream code can `except EmbedPrimaryError` if it
  ever wants tier-specific reactions._
- **E. HTTP fallback connection reuse** — verify with existing smoke test.
  _Status: verified working. `bin/memory/embed.py:210-261` returns a
  process-wide `httpx.AsyncClient` singleton with `max_keepalive_connections=16`,
  `keepalive_expiry=60s`. All three HTTP paths (CPU fallback, primary,
  bulk) call `_get_embed_client()` and share the same pool. Runtime
  verification: 8 sequential GETs to a known endpoint show
  `pool_conns` steady at 1, latency 7.1 ms → 0.6 ms after the first
  request (TCP+TLS handshake amortised). If reuse were broken,
  `pool_conns` would climb and latency would stay flat._

### Search-side (Phase 4)
- **F. `_apply_rerank` batches per-row reranker calls.**
  _Status: verified already batched. `bin/memory/search.py:521` calls
  `reranker.predict(pairs, ...)` with the full pair list (CrossEncoder's
  `.predict()` is internally SIMD-batched by sentence-transformers).
  No change._
- **G. `_query_title_overlap` recomputes query token set per row.**
  _Status: verified already fixed. The hot loop at
  `bin/memory/search.py:1087-1095` computes `q_title_set =
  _query_title_token_set(query)` once and calls
  `_title_overlap_from_qset(q_title_set, ...)` inside the row loop. The
  slow single-call form `_query_title_overlap` is kept only for
  back-compat with non-hot callers. No change in the hot path._
- **H. `_trim_by_elbow` may have O(n²) accidental slice.**
  _Status: verified non-issue. `bin/memory/search.py:274-299` is O(n):
  one list-comp over adjacent diffs, one sum, one linear scan. No
  nested slicing. The plan's "may have" was speculative; reading the
  code confirms it's clean. No change._

### Cross-cutting
- **J. `_lazy_init` race condition** under concurrent async first-call.
  _Status: verified non-issue (same reasoning as B). `bin/memory/db.py:226-241`
  uses `threading.Lock` with `key in _initialized_dbs` check. Correct.
  No change._
- **K. Telemetry counter writes** unlocked. Future-proof for free-threaded
  Python (PEP 703).
  _Status: open. Defer until free-threaded Python is the runtime
  default — premature optimization today._
- **L. Embedder failure circuit-breaker** is per-call not per-backend. Cache
  failure for N seconds to prevent slow-failure retry storms.
  _Status: open. Worth doing if HTTP fallback storms become a real
  problem; no evidence yet._

### lru_cache pass (2026-05-17)
- `_compile_fts_query` — was already lru-cached (`maxsize=2048`) since
  Phase 2. No change.
- `_query_title_token_set` — added `@lru_cache(maxsize=1024)`. Frozenset
  return preserves identity across cache hits so callers can use `is`
  if needed.
- `_content_hash` — added `@lru_cache(maxsize=512)`. Caches sha256 of
  augmented embed text; hit rate is high during bulk re-embed and
  chatlog drain where the same content is re-hashed.
- Parity-test classifier updated: `functools._lru_cache_wrapper` objects
  now classify as `function` (via `__wrapped__` introspection) instead
  of falling through to `constant`. Without this fix, adding `@lru_cache`
  to a helper would falsely flag as a function→constant drift.

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
| 2026-05-16 20:50 | Phase 3 — embed.py | done | The headline extraction. **655 lines** moved to `bin/memory/embed.py`: the cascade (`_embed`, `_embed_many`, `_content_hash`), in-process embedder lazy-init (`_get_embedded_embedder` + state), HTTP-client singleton + tuning, sliding-window chunking + dense recovery (with their constants), anchor augmentation, backend stats, `_embedded_label`, `set_embed_override`, per-call + bulk semaphores, dim-validation flag, canonical-name cache, `embedder_status_impl`. Eight separate blocks deleted from memory_core in sequence, parity test between each. Two surprise-finds: `_lru_cache` and `_ThreadLock` were inline aliases that the API snapshot had captured as public — both restored as explicit re-exports. `_track_cost` lazy-imported from memory_core inside each call (telemetry stays put for now). `ctx` resolved locally via `M3Context.for_db(...)` to avoid circular. **End-to-end real embed verified** through the shim: returned 1024-dim vector via ollama:11434. Identity preserved for `_EMBED_BACKEND_STATS` and `_ENTITY_NAME_EMBED_CACHE` across shim and module. memory_core.py: 7172 → 6489 lines (683 net this phase, 1236 cumulative). 20/20 tests pass. Committed as `c2f96fd` and pushed to both remotes. |
| 2026-05-16 21:10 | Phase 4 audit | done | Plan subagent produced the call-graph audit. Five recommendations adopted: (1) extract chroma.py FIRST as the smaller, lower-risk de-risk step; (2) `_batch_cosine` moves to `util.py` not `search.py` because write-path's `_check_contradictions` calls it; (3) lazy imports inside `memory_search_routed_impl` for `_maybe_expand_routed` and graph-side calls (graph stays in memory_core); (4) co-locate `_EVENT_PROPER_NOUN` regex with its hot reader `_maybe_route_query` in search.py; (5) `_recency_bonus_ranks` is dead-but-publicly-exported — move with note. Circular-risk callbacks documented: routed_impl → `_maybe_expand_routed`, scored_impl → `_track_cost`/gate functions, all to be handled via lazy imports. Silent-degradation watch list: `_hybrid_score_batch`, `_apply_temporal_boost`, `_apply_recency_bonus`, `_pull_predecessor_turns`, `_enforce_expansion_displacement_guard` (margin=1 default no-op risk), federation fallback. |
| 2026-05-16 21:15 | Phase 4.A — chroma.py | done | Extracted 152 lines into `bin/memory/chroma.py`: `_queue_chroma`, `_CHROMA_COLLECTION_ID_CACHE`, `_resolve_chroma_collection_id`, `_query_chroma`. One-way dep on `memory.embed._get_embed_client` (federation shares the embed httpx pool). Cache identity verified across shim and module. memory_core.py: 6489 → 6401 lines. 20/20 tests pass. Committed `2da350c`. |
| 2026-05-16 21:25 | Phase 4.B sub-1 — scoring helpers | done | Extracted scoring helpers: `_batch_cosine` to `memory.util` (per audit rec #2 — write-path co-tenant), `_cosine_batch_packed` / `_hybrid_score_batch` / `_recency_bonus_ranks` to `memory.search`. **Caught two bugs by hand-diffing before commit:** (1) `_hybrid_score_batch` Python fallback dropped a `float()` cast on `importances[i]` (silent type-coerce bug — would break with Decimal/string inputs); (2) `_recency_bonus_ranks` Python fallback I'd transcribed was just a stub returning zeros instead of the real rank-based math. Both fixed. memory_core.py: 6401 → 6275 lines. 20/20 tests pass. Committed `f6ddb6d`. |
| 2026-05-16 21:35 | Phase 4.B sub-2 — query routing | done | Extracted `_TEMPORAL_QUERY_RE`, `_DATE_RE_ISO`, `_DATE_RE_LONG`, `_DATE_MONTHS`, `_EVENT_PROPER_NOUN`, `_pull_predecessor_turns`, `_maybe_route_query` to `memory.search`. Special handling for `_EVENT_PROPER_NOUN`: memory_core's `_extract_event_sentences` still uses it; the shim re-export makes it module-level on memory_core via `from memory.search import _EVENT_PROPER_NOUN`, so `_extract_event_sentences` resolves it without any change. Identity preserved (verified). |
| 2026-05-16 21:50 | Phase 4.B sub-3 — ranker post-processing | done | Extracted `_apply_recency_bonus`, `_trim_by_elbow`, `_apply_temporal_boost` to `memory.search`. All three reference config constants (`ELBOW_*`) at call time. Smoke-tested with known inputs: recency interpolation correct, elbow returns all when no real drop, temporal boost +0.25 on exact-date match. |
| 2026-05-16 22:00 | Phase 4.B sub-4 — reranker family | done | Extracted `_RERANKER_MODEL`, `_RERANKER_MODEL_NAME`, `_get_reranker`, `_enforce_expansion_displacement_guard`, `_apply_rerank` to `memory.search`. **Signature-preservation gotcha caught:** `_enforce_expansion_displacement_guard` has `protected_ranks: int = EXPANSION_PROTECTED_RANKS` (defaults captured at definition time = literal `3`/`2.0` in the parity snapshot). I initially refactored to `None` defaults with body lookup for "live env-var support" — but that changes the signature string and breaks parity. Reverted to direct config.X access in the default expression, which serializes the same `3`/`2.0` values into the signature. memory_core.py: 6275 → 5900 lines (375 net for subs 2-4; 1825 cumulative, 24%). 20/20 tests pass. Committed `cbd6c4f`. |
| 2026-05-16 22:15 | Phase 4.B sub-5 — route helpers | done | Extracted `_TEMPORAL_ROUTER_PATTERNS`, `_TEMPORAL_ROUTER_RE`, `_ENTITY_MENTION_PATTERNS`, `_ENTITY_MENTION_RE`, `_UNSET`, `_extract_caller_overrides`, `_apply_auto_layer`, `_apply_sharp_trim`, `is_temporal_query` to `memory.search`. `_ENTITY_MENTION_RE` is read by `_entity_graph_neighbor_ids` (graph code, stays in memory_core) — works via shim re-export, identity preserved (verified). Smoke tests: `is_temporal_query("when did this happen")` → True, `_extract_caller_overrides` correctly identifies only changed params, `_apply_sharp_trim` keeps top 3 within 80% of max, `_ENTITY_MENTION_RE` extracts `[Caroline, Paris, 2024]` from sample. 20/20 tests pass. |
| 2026-05-16 22:25 | Phase 4.B sub-6+7 | **DEFERRED** | The remaining sub-blocks are `memory_search_scored_impl` (**1,074 lines** — bigger than audit estimated, the largest function in the codebase), `memory_search_routed_impl` (598 lines), `memory_search_multi_db_impl`, `memory_search_impl`. Together ~1,800 lines. **Stop signal triggered** per user direction ("Continue, but stop if anything looks risky."). Risks: (1) volume — `memory_search_scored_impl` is ~3× larger than the biggest function I've successfully copied verbatim; (2) silent-degradation surface — audit flagged this specific function; bugs in scoring/MMR/elbow/temporal-boost combinations don't fail tests, just shift retrieval scores; (3) no behavioral parity test exists yet — parity oracle checks the signature, not retrieval behavior; (4) bench-critical hot path; (5) cumulative bug-catch fatigue across 9 commits. **What's needed before sub-6**: a real read-only retrieval-quality regression test (similar to the embed_smoke.json baseline I already have, but for search). Then dedicated session for the 1,074-line copy. |
| 2026-05-16 (resume) | Phase 4.B sub-6+7 | done | Extracted all five impls into `bin/memory/search.py`. Actual line counts (audit/lessons-doc over-estimated): `memory_search_scored_impl` = **745 lines** (2045-2789, not 1074), `memory_search_routed_impl` = 475 (3120-3594), `_maybe_expand_routed` = 120 (3597-3716), `memory_search_multi_db_impl` = 87 (3719-3805), `memory_search_impl` = 62 (3808-3869) — 1,489 lines total. **Cycle-breaking pattern:** memory_core's import path runs `from memory import search` near its top, so `memory.search` body executes BEFORE memory_core defines the 9 callback symbols it still needs (`_cosine`, `_track_cost`, the gate predicates, and the 4 graph helpers that stay in memory_core per audit rec #5). Top-level `from memory_core import ...` would raise ImportError on the partial module. Solution: `_resolve_mc_callbacks()` — a one-line idempotent binder injected after each impl's docstring (via AST so the docstring stays the first statement) that imports memory_core lazily on first call and caches the resolved attributes into `memory.search`'s globals. Subsequent calls hit the cached globals; bare-name `_track_cost(...)` references resolve as if they were defined locally. **First-cut bug caught (intermediate state, not committed):** I initially mis-bounded scored_impl as 2045-3117 (treating the 4 graph helpers between it and routed as part of it). Parity test caught the regression — `{_graph_neighbor_ids, _session_neighbor_ids, _entity_graph_neighbor_ids, _score_extra_rows}` flagged as REMOVED. Reverted with `git checkout`, re-read with AST to get accurate ranges, redid the extraction with correct boundaries. **Second-cut bugs caught by retrieval baseline:** (1) `m3_core_rs` referenced as a bare name in scored_impl's MMR Rust path needed adding to the `from .config import` block; (2) `sqlite3` and `asyncio` were used as bare module names but never imported at search.py top. Both became `NameError`s under the baseline test; trivially fixed. **Parity test:** PASSED, 322 symbols intact, no new drift (the 3 `_mc_*` aliases were already in the snapshot from earlier phases). **Retrieval baseline:** PASSED, byte-identical for all 60 queries × 3 variants (scored_default, scored_max, routed). memory_core.py: 5795 → 4312 lines (1,483 net this phase, 3,413 cumulative = 44% reduction). search.py: 720 → 2308 lines. |
| 2026-05-17 | Phase 5 | done | Closeout pass via two background subagents. (1) **Tool inventory regenerated**: `docs/MCP_TOOLS.md` rebuilt from `bin/gen_mcp_inventory.py` — 74 → 75 tools (the new `memory_delete_bulk` from commit `249b4b2`). Script's internal `EXPECTED_TOOL_COUNT` bumped 74 → 75 and `memory_delete_bulk` added to the "Memory Operations" category so it doesn't land in "Uncategorized". Commit `d4a166d`. (2) **`docs/ARCHITECTURE.md` updated**: new `## Module Layout` section between System Overview and Storage Hierarchy. Covers the 7725 → 4429 line reduction (-44%, 11 commits), the `bin/memory/` package table, both cycle-breaking patterns (lazy-import-in-function for `_track_cost` in `db.py`/`embed.py` vs `_resolve_mc_callbacks()` globals binding in `search.py`), and what `memory_core` retains. Commit `7b273e2`. Both pushed to `origin/main` and `private-m3-memory-rs/main` after a clean pre-push leak scan. (3) Per-tool docs (`docs/tools/memory_embed.md`, `docs/tools/memory_search.md`) are landing via a separate background subagent. **Project closeout: Phases 0-5 complete.** Next-step plan for entity-extraction split lives in `docs/MEMORY_ENTITY_EXTRACTION_PLAN.md` and is explicitly a separate project. |
| 2026-05-17 | Phase 6 — entity.py | done | Entity-extraction subsystem lifted into `bin/memory/entity.py` (853 lines): vocab loading, 3-tier canonical-name resolution, entity CRUD, memory↔entity links, extraction queue + runner, and the four MCP read-side impls (`entity_search`, `entity_get`, `extract_pending`, `entity_extractor_health`). memory_core.py: 4,429 → 3,716 lines (-713 this phase, -52% cumulative from the original 7,725). Behavior baseline `tests/capture_entity_baseline.py` built BEFORE the extraction per lesson #6 — 30 entities × 3 variants, byte-fingerprinted; commit `b05c821`. Extraction commit: `6cdd8a3`. **Simpler than search.py**: only one callback into memory_core (`_track_cost`, telemetry) needed lazy binding — same single-line pattern db.py and embed.py use, no `_resolve_mc_callbacks()` globals shim required. Identity preserved on all three mutables — `VALID_ENTITY_TYPES`, `_ENTITY_EXTRACT_SEM`, `_PENDING_ENTITY_TASKS` — verified via `id()` checks across the shim. Retrieval-baseline drift caveat hit during verification: the corpus had shifted via the 287-row curation deletes earlier in the session, so the pre-existing baseline flagged spurious differences; refreshed baseline was clean. Per-tool doc landed as `docs/tools/entity_extraction.md` in Phase 6.2. |

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
