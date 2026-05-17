# Kickoff: Phase 4.B sub-6+7 extraction

Paste this whole file (or its body) as the first message in a fresh Claude Code session started in the `m3-memory` working tree.

---

Resume Phase 4.B sub-6+7 of the `memory_core` modularization. This is the deferred-from-last-session extraction of the four search impls — the biggest and most retrieval-critical move in the project.

## Read first (in this order)

1. `docs/MEMORY_CORE_MODULARIZATION.md` — live plan + per-phase log
2. `docs/MEMORY_CORE_MODULARIZATION_LESSONS.md` — 8 transferable lessons from the 9-commit run that got us here
3. Recall memory `a5b5c8ca` — the retrieval-quality regression test that gates this work
4. Recall memory `9f47dceb` — the broader session lessons

## What to extract

From `bin/memory_core.py` into `bin/memory/search.py`:

| Function | Approx lines | Notes |
|---|---|---|
| `memory_search_scored_impl` | 1,074 | Largest single function in the codebase. Bench-critical retrieval hot path. |
| `memory_search_routed_impl` | 598 | Production search entrypoint. |
| `memory_search_multi_db_impl` | smaller | Thin wrapper. |
| `memory_search_impl` | smaller | Thin wrapper. |
| `_prefer_observations_gate`, `_two_stage_observations_gate`, `_enable_entity_graph_gate` | tiny | Three-line wrappers around `_gate_active`; logical home is with the search code. Move at same time. |

Verify line counts with `grep -n "^async def " bin/memory_core.py` before scoping — last audit underestimated.

## Hard rules (from lessons doc — read them, don't paraphrase)

1. **Copy verbatim, modify only the imports.** Paraphrasing broke `_access_stamp_flusher`, `_recency_bonus_ranks`, `_hybrid_score_batch` in prior phases.
2. **Use `from .search import X` re-exports in `memory_core.py`**, never `X = .search.X` rebind — preserves mutable container identity.
3. **Lazy-import the callbacks.** `_routed_impl` needs `_maybe_expand_routed` and graph helpers from `memory_core`. Top-level import = cycle. Import inside the function body.
4. **Default-value signatures** that resolve module constants at definition time (e.g., `protected_ranks: int = config.EXPANSION_PROTECTED_RANKS`) must keep that pattern — the parity oracle snapshots the resolved literal.
5. **Watch for inline aliases** like `from functools import lru_cache as _lru_cache` buried in a block being moved. Check the parity snapshot for any name in the deleted range.

## Gating tests — both must pass before commit

```
# 1. Signature parity (322+ public symbols)
python tests/test_memory_core_parity.py

# 2. Retrieval quality (60 queries × 3 variants, fingerprinted)
M3_EMBED_GGUF=<your local bge-m3 GGUF path> \
  python tests/capture_retrieval_baseline.py
```

The retrieval baseline at `.scratch/migration_baseline/retrieval_baseline.json` was captured on commit `03f0c0b` (pre-extraction). Compare mode must report **"OK — retrieval output is byte-identical to baseline."** Any drift = stop, investigate.

If the local Rust embedder isn't configured: `M3_RETRIEVAL_REFRESH_BASELINE=1` will recapture, but only do this on the unchanged pre-extraction tree — and only if you're sure the prior baseline is environmentally invalid (e.g., remote ChromaDB host changed). Default: don't refresh.

## Commit cadence

One commit per impl, in this order — smallest first so the trickier moves benefit from a known-good shim pattern:

1. `_multi_db_impl` + the three `_gate` wrappers
2. `_impl`
3. `_routed_impl` (with lazy `_maybe_expand_routed` import)
4. `_scored_impl` (the 1,074-line one — fresh bug-catch budget required)

After each: run both gating tests, then commit. Don't batch.

## Stop signals

Pause and ask the user if any of these fire:

- A gating test fails after a transcription pass and you've spent >15 min hand-diffing without finding the cause.
- A function pulls in a symbol that needs to move at the same time but wasn't in the scope above.
- Bug-catch fatigue: by the time you start `_scored_impl`, ask yourself if you're still spotting drift on hand-diff. If not, stop and resume next session.

## Push policy

`origin` (public `skynetcmd/m3-memory`) AND `private-m3-memory-rs` (private mirror) both receive every commit on `main`. Before each push:

```
git diff origin/main..HEAD -- '**' | grep -iE 'C:/Users|/Users/[a-z]+|bhaba|10\.[0-9]+\.[0-9]+|192\.168|sk-ant-|AIza|lme-m/v3|lme_m\.db|smoke-142'
```

Non-empty = stop and scrub before pushing.

## Expected outcome

`bin/memory_core.py`: 5,795 → ~4,000 lines. `bin/memory/search.py`: 720 → ~2,500 lines. All 322+ public symbols still importable from `memory_core` via shim. Retrieval byte-identical to baseline.

After this, write/link/enrich/graph/emitters remain in `memory_core` and are out of scope unless the user explicitly opens Phase 5.
