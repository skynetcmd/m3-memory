# Phase 6+ — extract `entity.py` from `memory_core.py`

> Status: 2026-05-17. Plan only — not yet started.
> Predecessor: `docs/MEMORY_CORE_MODULARIZATION.md` (Phases 0–5, complete).
> Lessons doc: `docs/MEMORY_CORE_MODULARIZATION_LESSONS.md`.

## Why this exists

The Phase 0–5 modularization explicitly scoped `write.py`, `link.py`,
`graph.py`, `enrich.py`, `emitters.py`, and `entity.py` **out** of the
original plan. Now that Phase 5 closes out the original scope at
`bin/memory_core.py` = 4,429 lines (was 7,725, –44 %), the entity-extraction
subsystem is the next-best self-contained subsystem to lift out. It's a
clean ~700-line chunk with a well-defined surface: vocab loading,
canonical-name resolution, entity creation, memory↔entity linking,
extraction-queue runners, and the `entity_search` / `entity_get`
read-side impls.

This is a **separate project from Phase 0–5** — its own plan, its own
commit range, its own pre-flight gate. Do not bundle with unrelated work.

## Target structure

```
bin/memory/
  entity.py              # NEW — extracted concerns (target ~700 lines)
  ...                    # all other submodules unchanged
bin/
  memory_core.py         # remains the legacy shim + write/link/graph/conv/agents/tasks
```

## In-scope functions (verified via AST walk on commit `b8c447d`)

| Function                              | Range         | Lines | Notes |
|---------------------------------------|---------------|-------|-------|
| `load_entity_vocab`                   | 452–491       | 40    | Reads YAML, sets `VALID_ENTITY_TYPES` + `_PREDICATES` module globals at import time |
| `_resolve_entity`                     | 1674–1702     | 29    | Sync canonical-name → entity_id lookup |
| `_resolve_entity_async`               | 1719–1753     | 35    | Async wrapper + fuzzy-match path |
| `_create_entity`                      | 1756–1768     | 13    | INSERT INTO entities |
| `_link_memory_to_entity`              | 1771–1785     | 15    | INSERT INTO memory_entity_links |
| `_link_entity_relationship`           | 1788–1807     | 20    | INSERT INTO entity_relationships |
| `_enqueue_entity_extraction`          | 1810–1818     | 9     | Enqueue row in entity_extraction_queue |
| `_run_entity_extractor`               | 1821–1976     | 156   | The headline: SLM call, vocab filter, idempotent write |
| `_try_extract_or_enqueue`             | 1979–2028     | 50    | Gate-aware dispatcher (inline vs enqueued) |
| `_select_pending_entity_extraction`   | 4090–4144     | 55    | Drain helper for the background runner |
| `extract_pending_impl`                | 4147–4215     | 69    | MCP-exposed batch-process tool |
| `entity_extractor_health`             | 4218–4267     | 50    | Stats + queue depth + dead-letter count |
| `entity_search_impl`                  | 4271–4332     | 62    | MCP read-side: search entities by name/type |
| `entity_get_impl`                     | 4335–4429     | 95    | MCP read-side: one entity + its links + neighbors |
| **Total**                             |               | **698** | |

Also in-scope (config constants):

- `VALID_ENTITY_TYPES`, `VALID_ENTITY_PREDICATES` (module globals set by
  `load_entity_vocab` at import; preserve identity through shim — they're
  externally imported per the Phase 0.4 inventory).
- `_DEFAULT_VALID_ENTITY_TYPES`, `_DEFAULT_VALID_ENTITY_PREDICATES`,
  `DEFAULT_ENTITY_VOCAB_YAML`, `_ENV_ENTITY_VOCAB_YAML` (already in
  `bin/memory/config.py` — no move needed, just verify they're imported
  by `entity.py`).
- `ENTITY_RESOLVE_FUZZY_MIN`, `ENTITY_RESOLVE_COSINE_MIN`,
  `ENTITY_EXTRACT_CONCURRENCY`, `ENTITY_EXTRACT_MAX_ATTEMPTS`,
  `ENABLE_ENTITY_GRAPH` (already in config.py).

## Out of scope (do NOT touch)

- `_entity_graph_neighbor_ids` (line 2878–3079 of the legacy ordering —
  graph traversal, currently in memory_core, called from `search.py` via
  the lazy-binding shim). **Stays in memory_core** for the same reason
  the other graph helpers do: it's part of the graph-traversal subsystem,
  not the entity-CRUD subsystem.
- `_ENTITY_MENTION_RE` regex — already lives in `search.py`. memory_core
  re-imports it through the shim for `_entity_graph_neighbor_ids`. No
  move.
- `_create_event_row`, event-extraction code — different subsystem
  (`_extract_event_sentences` lives in memory_core, calls
  `_EVENT_PROPER_NOUN` from search.py through the shim). Unrelated to
  entity-CRUD.

## Cycle-breaking analysis (the load-bearing question)

Per lesson #5 in `MEMORY_CORE_MODULARIZATION_LESSONS.md`, we need to know
what `entity.py` will need from memory_core that memory_core can't give
it at submodule-load time.

**Calls back into memory_core that `_run_entity_extractor` makes:**
- `_track_cost(...)` — telemetry. Same pattern as embed.py and db.py
  (lazy import inside function body, single call site).
- `get_smallest_llm` / `get_best_llm` from `llm_failover` — third-party,
  not memory_core. Top-level import is fine.
- `_get_embed_client()` — lives in `memory/embed.py`. Top-level import
  from `.embed` is fine; entity.py loads AFTER embed.py per the
  `__init__.py` eager-import order.
- Direct sqlite via `_db()` — lives in `memory/db.py`. Top-level import.
- `_record_history` — `memory/db.py`. Top-level import.
- No graph helpers. No search helpers. **No circular risk.**

**Symbols `_run_entity_extractor` exposes that memory_core needs at top
level (NOT lazy):**

Per the Phase 0.4 import inventory, the most-imported entity privates
are `_run_entity_extractor`, `_link_entity_relationship`, `_create_entity`,
`_resolve_entity`. memory_core's `memory_write_bulk_impl` and
`_try_enrich_or_enqueue` call them as bare names. After extraction, the
shim re-exports them; memory_core itself imports them from
`memory.entity` at top of file (same pattern as the other submodules).

**Conclusion: entity.py is the simplest extraction in the project so
far.** It has zero functions that call back into memory_core at module
level. The only lazy import needed is `_track_cost` (telemetry), same
pattern already in use in db.py and embed.py.

## Pre-flight hard rules

Same as Phase 0–5:
- Don't start within 48 hours of a benchmark run.
- Don't change public MCP tool signatures (`entity_search_impl`,
  `entity_get_impl`, `extract_pending_impl`, `entity_extractor_health`).
- Preserve `VALID_ENTITY_TYPES`, `VALID_ENTITY_PREDICATES` object
  identity through the shim — these are externally imported.
- Every phase must commit independently; revertable with one `git revert`.
- Pre-extraction baseline: run `tests/capture_retrieval_baseline.py` and
  `tests/test_memory_core_parity.py`; both must be clean.

## Behavioral parity oracle — does one exist?

Yes for signatures (`tests/test_memory_core_parity.py`). **No for entity
behavior.** The retrieval baseline catches search-side drift via byte
hashes on result lists, but it doesn't cover `entity_search_impl` or
`entity_get_impl`. Before starting Phase 6, write `tests/capture_entity_baseline.py`
that:

1. Picks N=30 deterministic entity names from `entities` table
   (`ORDER BY id LIMIT 1000`, seeded random sample).
2. For each, runs `entity_search_impl(name, k=10)` and `entity_get_impl(id, depth=1)`.
3. Fingerprints the result rows (sorted ids + their `entity_type` + neighbor counts)
   into `.scratch/migration_baseline/entity_baseline.json`.
4. Diffs current vs baseline; exit 0 on match, 1 with detailed drift report.

This follows the pattern of `tests/capture_retrieval_baseline.py` (commit
`03f0c0b`). Lesson #6 from the modularization run is "build the
behavior baseline BEFORE the extraction, not after" — that's
non-negotiable for any function-extraction phase whose functions are
publicly exposed.

## Phase 6 plan

### Phase 6.0 — Bootstrap (~20 min)

- [ ] Write `tests/capture_entity_baseline.py` per the schema above. Run
      with `M3_ENTITY_REFRESH_BASELINE=1` to capture; re-run without to
      confirm clean.
- [ ] Confirm `tests/test_memory_core_parity.py` is green on the current
      commit.
- [ ] Commit: `test(memory): entity behavior regression baseline`.

### Phase 6.1 — Extract entity.py (~90 min)

- [ ] AST-walk `bin/memory_core.py` to confirm function ranges match
      the table above (line numbers will have shifted from `b8c447d` —
      verify before slicing). Lesson #7: don't trust documented line
      numbers; AST first.
- [ ] Create `bin/memory/entity.py` with:
      - Top-level imports: `asyncio`, `json`, `logging`, `sqlite3`,
        `uuid`, `yaml` (for vocab loading), `pathlib.Path`, `datetime`,
        the relevant config constants, `_db` / `_record_history` from
        `memory.db`, `_get_embed_client` / `_embed` from `memory.embed`,
        `_token_jaccard` from `memory.util` (verify this is already there;
        otherwise add it).
      - Lazy-import shim for `_track_cost` (mirror the pattern from
        `db.py`).
      - The 14 functions in the in-scope table, copied verbatim. Modify
        only the imports.
- [ ] Add `entity.py` to `bin/memory/__init__.py` eager-import block.
- [ ] Wire shim re-exports in `bin/memory_core.py`:
      ```
      from memory import entity as _mc_entity  # noqa: F401
      from memory.entity import (  # noqa: F401 — re-exports
          load_entity_vocab, VALID_ENTITY_TYPES, VALID_ENTITY_PREDICATES,
          _resolve_entity, _resolve_entity_async, _create_entity,
          _link_memory_to_entity, _link_entity_relationship,
          _enqueue_entity_extraction, _run_entity_extractor,
          _try_extract_or_enqueue, _select_pending_entity_extraction,
          extract_pending_impl, entity_extractor_health,
          entity_search_impl, entity_get_impl,
      )
      ```
- [ ] Delete the 14 functions from `memory_core.py` (preserve the
      `VALID_ENTITY_TYPES = load_entity_vocab(None)` invocation site as a
      single re-export line if needed for back-compat — but it should
      come automatically through the `from memory.entity import ...`).
- [ ] Identity-check externally-imported mutable globals:
      ```python
      assert id(mc.VALID_ENTITY_TYPES) == id(memory.entity.VALID_ENTITY_TYPES)
      ```
- [ ] Run `tests/test_memory_core_parity.py` — green.
- [ ] Run `tests/capture_entity_baseline.py` — green.
- [ ] Run `tests/capture_retrieval_baseline.py` (no entity-impl change
      should drift it) — green.
- [ ] Commit: `refactor(memory): Phase 6 — extract entity.py`.

### Phase 6.2 — Cleanup + docs (~30 min)

- [ ] Add `docs/tools/entity_extraction.md` describing the extractor
      pipeline (SLM call, vocab filter, dedup, queue runner).
- [ ] Update `docs/ARCHITECTURE.md` "Module layout" section to add
      `entity.py` to the table.
- [ ] Append a row to `docs/MEMORY_CORE_MODULARIZATION.md` live log
      noting Phase 6 done with the final line count.
- [ ] Commit: `docs(memory): document entity.py extraction`.

## Expected end state

- `bin/memory_core.py`: 4,429 → ~3,730 lines (–698, cumulative –52 %).
- `bin/memory/entity.py`: ~700 lines.
- Remaining `memory_core` content: write path (`memory_write_bulk_impl`,
  `_check_contradictions`, `_try_enrich_or_enqueue`,
  `_run_fact_enricher`, `_write_fact_rows`, ~1400 lines), conversation
  impls (~300 lines), agent/task/notification CRUD (~1000 lines), graph
  helpers (~330 lines), enrichment select-pending + extract-pending
  (~300 lines), misc.

If the project continues past Phase 6, the next-best candidate is
`write.py` (the write path + contradiction-check + auto-classify +
auto-title + auto-entities glue). That's ~1500 lines, more entangled
with embedding and entity code, and probably deserves its own plan
doc before starting.

## Risk register

| Risk | Mitigation |
|------|------------|
| `load_entity_vocab` runs at module import time and sets `VALID_ENTITY_TYPES` / `_PREDICATES`. Moving the call may change import-time side effects | Preserve the `load_entity_vocab(None)` call in `entity.py` at module scope. Identity-check the resulting frozensets. |
| `_run_entity_extractor` SLM call is the biggest single function (156 lines) — high transcription-error risk | Copy verbatim. Hand-diff against legacy before commit (lesson #1). |
| External callers (m3-memory-bench) import `_create_entity`, `_link_entity_relationship` etc. — must keep working through the shim | Same `from .entity import ...` shim pattern that worked for embed.py / search.py. |
| `entity_search_impl` and `entity_get_impl` are public MCP tools — behavior must not drift | The `tests/capture_entity_baseline.py` oracle catches this if you build it BEFORE the extraction. |
| `entities` table schema may differ in archive DB | `_db()` already resolves the correct path. No special handling. |

## Cross-references

- `docs/MEMORY_CORE_MODULARIZATION.md` — Phases 0–5 plan + log
- `docs/MEMORY_CORE_MODULARIZATION_LESSONS.md` — transferable rules
- `docs/MCP_TOOL_PERF_LESSONS.md` — diagnose-the-tool-shape (orthogonal,
  but the same author-discipline applies)
- Memory `9f47dceb` — modularization session lessons (searchable form)
- Memory `e098dd28` — full 2026-05-17 session summary
- Commit `b8c447d` — Phase 0–5 closing commit (the line numbers in this
  plan are relative to this commit; will need re-verification at Phase
  6 kickoff)
