# memory_core modularization — session lessons (2026-05-16)

> **Provenance:** session lessons from the 9-commit modularization run.
> Intended for re-import into the m3 memory store as a `reference`-type
> memory next session (the m3 MCP server was disconnected when this note
> was written, so it landed as a doc instead).
>
> Authoritative plan + live progress log: `docs/MEMORY_CORE_MODULARIZATION.md`.
> Commit range: `9344407..849d76c`.

## Outcome

`bin/memory_core.py` went from **7,725 → 4,312 lines (-44%)** across the
full run, ending with the Phase 4.B sub-6+7 retrieval-impl extraction
(commit `b6efe8e`). 10 phase commits, all pushed to both public
`origin/main` and private `private-m3-memory-rs/main`.

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
    search.py            # 2,308 lines — scoring helpers, ranker post-proc, reranker, query routing, auto-route layer, is_temporal_query, plus the four retrieval impls (scored / routed / multi-db / search / _maybe_expand_routed)
```

## Phase 4.B sub-6+7 — done

Extracted the four retrieval impls + `_maybe_expand_routed` into
`bin/memory/search.py` (commit `b6efe8e`). Actual line counts (audit's
"1,074-line `_scored_impl`" estimate was wrong — the audit had treated
the four graph helpers that sit between `_scored_impl` and `_routed_impl`
as part of `_scored_impl`):

| Function                          | Legacy lines (range)        | Body lines |
|-----------------------------------|-----------------------------|------------|
| `memory_search_scored_impl`       | 2045–2789                   | 745        |
| `memory_search_routed_impl`       | 3120–3594                   | 475        |
| `_maybe_expand_routed`            | 3597–3716                   | 120        |
| `memory_search_multi_db_impl`     | 3719–3805                   | 87         |
| `memory_search_impl`              | 3808–3869                   | 62         |
| **Total**                         |                             | **1,489**  |

Graph helpers (`_graph_neighbor_ids`, `_session_neighbor_ids`,
`_entity_graph_neighbor_ids`, `_score_extra_rows`) stay in `memory_core`
per audit recommendation #5. `memory_core.py`: 5,795 → 4,312 lines.

**Prereq satisfied:** `tests/capture_retrieval_baseline.py` (60 queries
× 3 variants, byte-fingerprinted) passed before and after — drift-free
on extraction.

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

### 7. Don't trust the audit's line-count estimates — AND don't trust the prior session's estimate either

The Plan subagent audited memory_core and estimated
`memory_search_scored_impl` at ~600 lines. The prior session's stop-signal
note revised that to "1,074 lines — the biggest single function in the
codebase." Both were wrong: **actual is 745 lines** (2045–2789). The
extra 329 lines were the four graph helpers (`_graph_neighbor_ids`,
`_session_neighbor_ids`, `_entity_graph_neighbor_ids`, `_score_extra_rows`)
that sit between `_scored_impl` and `_routed_impl` — they're top-level
`def`/`async def` at column 0, not nested inside `_scored_impl`.

`grep -n "^async def "` would have caught this; reading the source at
the assumed end-line would have caught this; AST-walking would have
caught this. The 1,074 number in the prior session's lessons doc came
from line-arithmetic on a misread of the function boundary, and that
inflated number propagated to a memory record (`9f47dceb`) before
verification.

**Rule:** for any function-extraction phase, the FIRST step is an AST
walk that prints `(name, lineno, end_lineno)` for every function in the
target range. Never extract on the basis of a remembered or estimated
line range — especially one that came from a different session.

```python
import ast
tree = ast.parse(open('bin/memory_core.py').read())
for n in ast.walk(tree):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.col_offset == 0:
        print(f'{n.name:40s} {n.lineno}-{n.end_lineno}')
```

### 8. Bare-name stdlib imports are easy to miss

The five retrieval impls used `sqlite3.OperationalError` (in scored_impl's
FTS error catch) and `asyncio.gather` / `asyncio.Semaphore` as bare module
names. Top-of-file search.py imported `json`, `logging`, `os`, `re` — but
not `sqlite3` or `asyncio`. Both worked at import time (the names are
referenced lazily inside function bodies), and the parity test passed
because module bindings don't affect signatures. The retrieval baseline
caught it as `NameError` inside the captured per-query error string —
which the baseline test stores under `{"error": str(e)[:200]}` and then
emits as `current sha: None` on drift.

**Rule:** before deleting any block from legacy `memory_core.py`, grep
the extracted text for stdlib module names (`sqlite3`, `asyncio`, `uuid`,
`threading`, `platform`, `hashlib`, `time`, `copy`, etc.) and ensure
each is imported at the top of the destination module. The legacy file
has them at the top; the new module must too.

### 9. Cumulative bug-catch fatigue is real

Across 10 commits I caught 6 small transcription errors per ~500
lines moved (4 in Phase 1–4.A, 2 more in sub-6+7). That bug-catch
capacity isn't infinite — by sub-5 (Phase 4.B), willingness to spend
10+ minutes hand-diffing a 100-line block was lower than at sub-1.

The sub-6+7 retrieval extraction was the largest in the run (1,489 net
lines across 5 functions) and needed a focused fresh-session budget —
both the boundary-detection bug (lesson #7) and the bare-name stdlib
imports bug (lesson #8) were caught BY THE TESTS, not by hand-diffing.
That's the saving grace of having both a signature-parity oracle and a
behavior-parity baseline: even if eyes glaze over, the tests don't.

**Rule:** Phases with multiple large extractions should each be a
separate session — and the tests must be the gating signal, not "did
the diff look right." Trust hand-diffing for small blocks; trust the
parity + baseline tests for large ones.

## What the migration delivered, customer-side

- **Lower cognitive load** for any future contributor reading the embed
  pipeline (one 655-line module), the search hot path (one 2,308-line
  module containing the four impls + scoring/ranker/router helpers), or
  any of the smaller submodules — vs scrolling through 7,725 lines of
  legacy `memory_core.py`. The remaining 4,312-line memory_core is
  write / link / enrich / graph / entity-extraction / agents / tasks
  / notifications.
- **Module-level testability** — sliding-window, dense-recovery, and
  retrieval-baseline tests run without setting up memory_core's full
  DB+context environment for the parts that don't need it.
- **Identity-preserved compatibility** — all 22+ external callers (per
  the cross-worktree import inventory, 1,124 sites in m3-memory + 934
  in m3-memory-bench) continue to work via the shim. Zero star-imports
  were lost; two surprise inline aliases were restored.
- **Behavior-parity oracle** — `tests/capture_retrieval_baseline.py`
  (60 deterministic queries × 3 variants, byte-fingerprinted) is now
  the gate for any future change touching the retrieval hot path. The
  embed-side equivalent is `tests/capture_migration_baseline.py`'s
  `embed_smoke.json` (100 deterministic embeds, per-vector sha256).
- **Phase 1.5 dense-recovery** (the work that started this session) is
  durably committed and tested against the two known-bad rows
  (`778e7500`, `7127bb1e`).

## Cycle-breaking pattern adopted in sub-6+7

The four retrieval impls call back into memory_core for nine symbols
that stay there: `_cosine`, `_track_cost`, `_prefer_observations_gate`,
`_two_stage_observations_gate`, the four graph helpers, and
`memory_graph_impl`. Memory_core imports `memory.search` near its top,
so a top-level `from memory_core import _track_cost` in search.py would
hit the partial module and fail.

Solution: `_resolve_mc_callbacks()` — an idempotent module-global
binder that lazy-imports memory_core on first call and stuffs the nine
attributes into `memory.search`'s `globals()`. A single line
`_resolve_mc_callbacks()` is injected (via AST, immediately after each
impl's docstring) at the top of each impl body. After the first call
in a process, subsequent calls short-circuit on the
`_MC_CALLBACKS_BOUND` flag.

This keeps the function bodies **verbatim** (bare-name references like
`_track_cost(...)` keep working because the names are bound in the
module's globals by call time) while breaking the import cycle. Note:
module-level `__getattr__` (PEP 562) does NOT work for this — it only
fires for `module.X` attribute access, not for `LOAD_GLOBAL` inside
functions defined in the module. Verified empirically before settling
on the globals-binding approach.

## Cross-references

- `docs/MEMORY_CORE_MODULARIZATION.md` — the plan + live progress log
- Commit range: `9344407..b6efe8e` on `main` (10 phase commits)
- Sub-6+7 final commit: `b6efe8e` — extract the four retrieval impls
- Memory `a774353c` — m3-memory ↔ m3-core-rs release loop (not affected
  by this migration)
- Memory `1718c40f` — embedding pipeline anchor (still authoritative;
  `embed.py` is now its concrete home)
- Memory `a5b5c8ca` — retrieval-quality regression baseline
  (`tests/capture_retrieval_baseline.py`) that gated sub-6+7
