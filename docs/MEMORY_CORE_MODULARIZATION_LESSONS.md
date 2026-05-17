# memory_core modularization — session lessons (2026-05-16)

> **Provenance:** session lessons from the 9-commit modularization run.
> Intended for re-import into the m3 memory store as a `reference`-type
> memory next session (the m3 MCP server was disconnected when this note
> was written, so it landed as a doc instead).
>
> Authoritative plan + live progress log: `docs/MEMORY_CORE_MODULARIZATION.md`.
> Commit range: `9344407..849d76c`.

## Outcome

`bin/memory_core.py` went from **7,725 → 5,795 lines (-25%)**. 9 phase
commits, all pushed to both public `origin/main` and private
`private-m3-memory-rs/main`.

## Final module layout

```
bin/
  memory_core.py         # legacy module + shim — 5,795 lines (was 7,725)
  memory/
    __init__.py          # eager re-exports
    config.py            # 308 lines — env vars, constants, m3_core_rs ref, _EMBED_*_OVERRIDE mutables
    util.py              # 72 lines  — sha256_hex, _batch_cosine (write+search shared)
    fts.py               # 149 lines — FTS5 helpers, title overlap
    db.py                # 384 lines — _db, _conn, _lazy_init, schema, history, gates, access-stamp batcher
    embed.py             # 655 lines — the cascade, in-process Rust embedder, HTTP client, sliding window + dense recovery, backend stats, set_embed_override
    chroma.py            # 152 lines — federation, _queue_chroma, _query_chroma
    search.py            # 720 lines — scoring helpers, ranker post-proc, reranker, query routing, auto-route layer, is_temporal_query
```

## Deferred (NOT done — next-session work)

Phase 4.B sub-6+7: `memory_search_scored_impl` (**1,074 lines** — the
biggest single function in the codebase), `memory_search_routed_impl`
(598 lines), `memory_search_multi_db_impl`, `memory_search_impl`.

**Stop signal triggered** because:

1. Volume — `_scored_impl` is 3× the largest function I'd successfully copied.
2. Bench-critical retrieval hot path.
3. Silent-degradation surface (audit-flagged).
4. **No behavioral parity test exists yet** — only signature parity.

**Prerequisite before resuming:** build read-only retrieval-quality
regression test (capture 100 query→result-IDs before extraction; diff
after).

## Lessons learned — apply to any future module extraction

### 1. Don't paraphrase legacy code

First draft of `_access_stamp_flusher` had `_ACCESS_FLUSH_INTERVAL=5.0`
vs legacy `0.25` (20× slower flush), missing `access_count` update,
wrong timestamp source.

First draft of `_recency_bonus_ranks` had a stub returning zeros instead
of the real rank-based math.

First draft of `_hybrid_score_batch` dropped a `float()` cast on
`importances[i]`.

**All caught by hand-diffing against legacy before committing.**
Rule: **copy verbatim, modify only the imports.**

### 2. Beware "inline aliases" captured in the parity snapshot

`_lru_cache` (line 723 in legacy FTS block: `from functools import
lru_cache as _lru_cache`) and `_ThreadLock` (legacy embed-stats block:
`from threading import Lock as _ThreadLock`) were both inline aliases
that the API parity oracle captured as public symbols. External callers
actually import them.

After moving the surrounding code, the aliases were silently lost —
caught by parity test, restored as explicit shim re-exports.

**Rule:** when deleting a block of legacy code, check the parity
snapshot for any name that lives inside it.

### 3. Default-value signatures

`_enforce_expansion_displacement_guard(hits, *, protected_ranks: int =
EXPANSION_PROTECTED_RANKS, margin: float = EXPANSION_DISPLACEMENT_MARGIN)`
— the parity snapshot captures the **resolved values** at definition
time (literal `3` and `2.0`), not the constant names. If you refactor
to `None` sentinels with body-side defaults for "live env-var support,"
the signature string changes and parity breaks.

**Rule:** if a function's defaults pull from module constants,
preserve `config.X` direct access in the default expression so the
resolved literal still matches.

### 4. Mutable container identity must be preserved through the shim

`_initialized_dbs`, `_GATE_CACHE`, `_access_pending`,
`_EMBED_BACKEND_STATS`, `_ENTITY_NAME_EMBED_CACHE`,
`_CHROMA_COLLECTION_ID_CACHE`, `_UNSET`, `_ENTITY_MENTION_RE` — every
mutable global moved into a submodule must be re-exported as
`from .submodule import X` (NOT `X = .submodule.X`). The first preserves
object identity; the second copies.

Verified after every extraction with `id(mc.X) == id(memory.submodule.X)`.

**Rule:** use `from-import` re-exports, never rebind.

### 5. Lazy imports for circular callbacks

`memory.search` (when sub-6+7 land) will call back into memory_core for
`_maybe_expand_routed` and the graph-side helpers. Top-level
`from memory_core import _maybe_expand_routed` forms a cycle
(memory_core imports memory.search at top via the shim). Solution:
lazy import inside the function body.

`db.py` and `embed.py` both use this pattern for `_track_cost`
lazy-import from memory_core.

**Rule:** when a submodule needs something from memory_core that
memory_core can only access by re-importing from the submodule,
lazy-import at function-call time.

### 6. Parity test catches symbol/signature drift, NOT behavior drift

`tests/test_memory_core_parity.py` is the regression oracle — it
snapshots 322+ public symbols' names, kinds, and signatures.
**It does not** check behavior.

The 100-row `embed_smoke.json` baseline catches embed pipeline behavior
changes via per-vector sha256, but no equivalent search-quality
baseline exists yet.

**Rule for sub-6+7:** build the retrieval-quality regression baseline
BEFORE the extraction, not after.

### 7. Don't trust the audit's line-count estimates

The Plan subagent audited memory_core and estimated
`memory_search_scored_impl` at ~600 lines. Actual: 1,074. The audit
was right on structure (call graph, circular risks) but wrong on volume.

**Rule:** verify line counts via `grep -n "^async def "` before
scoping the extraction.

### 8. Cumulative bug-catch fatigue is real

Across 9 commits I caught 4-5 small transcription errors per ~500
lines moved. That bug-catch capacity isn't infinite — by sub-5
(Phase 4.B), willingness to spend 10+ minutes hand-diffing a 100-line
block was lower than at sub-1. The 1,074-line `_scored_impl`
extraction needs a fresh-session bug-catch budget.

**Rule:** Phases with multiple large extractions should each be a
separate session.

## What the migration delivered, customer-side

- **Lower cognitive load** for any future contributor reading the embed
  pipeline (now one 655-line module) or search (now 720-line module
  split between ranker and query routing) vs scrolling through 7,725
  lines.
- **Module-level testability** — sliding-window and dense-recovery
  unit tests work without setting up memory_core's full DB+context
  environment.
- **Identity-preserved compatibility** — all 22+ external callers (per
  the cross-worktree import inventory, 1,124 sites in m3-memory + 934
  in m3-memory-bench) continue to work via the shim. Zero star-imports
  were lost; two surprise inline aliases were restored.
- **Phase 1.5 dense-recovery** (the work that started this session) is
  durably committed and tested against the two known-bad rows
  (`778e7500`, `7127bb1e`).

## How to resume Phase 4.B sub-6+7 (next session)

1. **Read `docs/MEMORY_CORE_MODULARIZATION.md` first** — full live log.
2. **Build the retrieval-quality regression test.** Use
   `tests/capture_migration_baseline.py` as the template; sample ~100
   queries from chatlog, run `memory_search_scored_impl(q, k=20)`,
   snapshot the result list. Then after extraction, diff identical-input
   output. **Required to detect silent-degradation bugs.**
3. **Lazy-import the callbacks.** `memory_search_routed_impl` will need
   `from memory_core import _maybe_expand_routed` inside the function
   body, not top-level — to avoid the memory_core → memory.search →
   memory_core cycle.
4. **Move `_prefer_observations_gate`, `_two_stage_observations_gate`,
   `_enable_entity_graph_gate`** at the same time. Three-line wrappers
   around `_gate_active`. Logical home with the search code.
5. **The retrieval-impl extraction is one focused session**, not a
   tail-of-session task. 1,800+ lines of careful copy + parity test +
   behavioral parity test.
6. **memory_core would land around 4,000 lines** after sub-6+7 — still
   big but functional for the write/link/enrich/graph code that stays.

## Cross-references

- `docs/MEMORY_CORE_MODULARIZATION.md` — the plan + log
- Commit range: `9344407..849d76c` on `main`
- Memory `a774353c` — m3-memory ↔ m3-core-rs release loop (not affected
  by this migration)
- Memory `1718c40f` — embedding pipeline anchor (still authoritative;
  `embed.py` is now its concrete home)
