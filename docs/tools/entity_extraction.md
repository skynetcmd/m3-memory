# entity_extraction — entity subsystem

> Status: 2026-05-17. Per-tool doc, Phase 6.2 of the `memory_core` modularization.
> Audience: someone debugging entity-side bugs — extraction misses, fuzzy
> resolution drift, queue runner stuck, MCP read-side anomalies. Companion to
> `docs/MEMORY_ENTITY_EXTRACTION_PLAN.md` (the Phase 6 plan) and
> `docs/MEMORY_CORE_MODULARIZATION.md` (per-commit log).

---

## Where it lives

| File | Lines | Role |
|---|---|---|
| `bin/memory/entity.py` | 853 | Authoritative module. Vocab loading, canonical-name resolution (3 tiers), entity CRUD, memory↔entity links, extraction-queue runners, MCP read-side impls. |
| `bin/memory_core.py` | 3,716 | Legacy shim. Re-exports every public symbol via `from memory import entity as _mc_entity` + a `from .entity import …` block (`memory_core.py:295-324`). |

Re-exports in the shim (object-identity preserved, not copies):

```
load_entity_vocab, VALID_ENTITY_TYPES, VALID_ENTITY_PREDICATES,
_TOKEN_PUNCT_RE, _token_jaccard,
_ENTITY_EXTRACT_SEM, _PENDING_ENTITY_TASKS,
_resolve_entity, _resolve_entity_async,
_create_entity, _link_memory_to_entity, _link_entity_relationship,
_enqueue_entity_extraction, _run_entity_extractor,
_try_extract_or_enqueue, _select_pending_entity_extraction,
extract_pending_impl, entity_extractor_health,
entity_search_impl, entity_get_impl
```

Legacy callers like `from memory_core import _create_entity` keep working
unchanged. Identity is the load-bearing invariant:
`id(memory_core._ENTITY_EXTRACT_SEM) == id(memory.entity._ENTITY_EXTRACT_SEM)`
must hold — without it, the write path's "is the semaphore full?" check
diverges from what the queue runner sees. See
`docs/MEMORY_CORE_MODULARIZATION_LESSONS.md` §4 (mutable container identity).

---

## The extraction cascade

A memory write that should produce entity rows traverses, in order:

1. **Write path call site** in `memory_core.memory_write_bulk_impl` invokes
   `_try_extract_or_enqueue(memory_id, content, db, …)`
   (`entity.py:463-513`).
2. **Gate check.** `_try_extract_or_enqueue` reads
   `_ENTITY_EXTRACT_SEM._value` (current semaphore capacity). If non-zero,
   the inline path is taken — `_run_entity_extractor` is scheduled as an
   `asyncio.Task` and added to `_PENDING_ENTITY_TASKS` so the event loop
   keeps a hard reference until it finishes.
3. **Inline.** `_run_entity_extractor` (`entity.py:305-461`) acquires
   `_ENTITY_EXTRACT_SEM`, calls the SLM, parses JSON, filters by vocab,
   dedups against existing entities, writes via `_create_entity` +
   `_link_memory_to_entity` + (optionally) `_link_entity_relationship`,
   and releases the semaphore in `finally`.
4. **Enqueue.** When the semaphore is empty, `_enqueue_entity_extraction`
   (`entity.py:294-303`) inserts a row into `entity_extraction_queue`
   keyed on `(memory_id, attempts, last_attempt_at)` and returns
   immediately. The write completes without blocking on the SLM.
5. **Background drain.** `extract_pending_impl` (`entity.py:572-641`) —
   the MCP-exposed batch tool — selects pending rows via
   `_select_pending_entity_extraction` and runs `_run_entity_extractor`
   on each, bumping `attempts` on failure.

The verbatim memory row is **always** persisted before extraction is
attempted. Extractor failure cannot corrupt the primary write.

---

## Vocabulary loading

`load_entity_vocab(yaml_path)` (`entity.py:122-165`) returns
`(VALID_ENTITY_TYPES, VALID_ENTITY_PREDICATES)` as **frozensets**.

Resolution order:

| Tier | Source | Notes |
|---|---|---|
| 1 | `M3_ENTITY_VOCAB_YAML` env var | Operator override. Path to a YAML with `entity_types: [...]` and `entity_predicates: [...]`. |
| 2 | `DEFAULT_ENTITY_VOCAB_YAML` | Project-shipped default (in `bin/memory/config.py`). Resolves relative to the install. |
| 3 | `_DEFAULT_VALID_ENTITY_TYPES` / `_DEFAULT_VALID_ENTITY_PREDICATES` | Hardcoded fallbacks in `bin/memory/config.py`. Only used when both YAMLs are missing/malformed. |

The call happens at module import time (`entity.py:167`), so
**modifying vocab requires a process restart.** The resulting frozensets
are externally imported via the shim — preserving their `id()` across
`memory.entity.VALID_ENTITY_TYPES` and
`memory_core.VALID_ENTITY_TYPES` was checked at extraction time.

Default types: `{person, place, organization, event, concept, object, date}`.
Default predicates: `{works_at, located_in, before, after, same_as, contradicts, mentions, relates_to}`.

---

## Resolution tiers — canonical_name → entity_id

The lookup that decides "is this a new entity or one we already have?" is
a three-tier cascade. Tier 1 is sync (`_resolve_entity`, `entity.py:172-201`);
all three run in the async wrapper (`_resolve_entity_async`,
`entity.py:203-238`).

| Tier | Match rule | Env knob | Path |
|---|---|---|---|
| 1 | Exact `(canonical_name, entity_type)` in `entities` | — | Single SQL `SELECT id FROM entities WHERE canonical_name = ? AND entity_type = ?`. |
| 2 | Token Jaccard ≥ `ENTITY_RESOLVE_FUZZY_MIN` | `M3_ENTITY_RESOLVE_FUZZY_MIN` (default in `config.py`) | `_token_jaccard(a, b)` lower-cases, strips via `_TOKEN_PUNCT_RE = re.compile(r"[^\w\s]")`, then `|A∩B| / |A∪B|`. Iterates same-type entities. |
| 3 | Embedding cosine ≥ `ENTITY_RESOLVE_COSINE_MIN` | `M3_ENTITY_RESOLVE_COSINE_MIN` (default in `config.py`) | Async only. Embeds the canonical name once and cosines against `_ENTITY_NAME_EMBED_CACHE` (in `memory/embed.py`, capped at `ENTITY_NAME_EMBED_CACHE_MAX`, default 50,000). |

This is the path that lets `"Alex Johnson"` and `"Alex Johnson,"`
collapse via tier 2, and `"AlexJ"` ↔ `"Alex Johnson"` collapse via
tier 3.

---

## The extractor — `_run_entity_extractor`

The headline function. 156 lines. `entity.py:305-461`.

Sequence (per `(memory_id, content)`):

1. **Acquire `_ENTITY_EXTRACT_SEM`** — `await semaphore.acquire()`.
   Bounded by `ENTITY_EXTRACT_CONCURRENCY` (default 2). Released in
   `finally`.
2. **SLM call** via `get_smallest_llm` (from `bin/llm_failover.py`).
   Prompt is the entity-extraction template; expects JSON `{entities: [...], relationships: [...]}`.
   Telemetry through lazy-imported `_track_cost` — `from memory_core import _track_cost`
   inside the function body, exactly the pattern `embed.py` and `db.py` use.
3. **JSON parse.** Tolerant of fenced code blocks (`json … `) and
   leading prose. Malformed JSON → marks row as poisoned, sets
   `last_extracted_at`, returns. No retry inside the function.
4. **Vocab filter.** Entities whose `entity_type` is not in
   `VALID_ENTITY_TYPES` are dropped silently. Same for predicates not in
   `VALID_ENTITY_PREDICATES`.
5. **Dedup against existing rows.** For each candidate entity, calls
   `_resolve_entity_async` (3-tier). Match → reuse the existing
   `entity_id`. Miss → `_create_entity` to mint a UUID, then
   `_link_memory_to_entity`.
6. **Relationship writes.** For each `(subject, predicate, object)` in the
   parsed relationships, both subject and object must have resolved to
   entity_ids in step 5. `_link_entity_relationship` writes the
   typed edge.
7. **Idempotency.** Memory_id is stamped with `last_extracted_at` at
   the end. Re-running on the same memory is a no-op once stamped.

**Poisoned-row detection.** The bottom of the function (around
`entity.py:445-460`) sets `last_extracted_at` even when the SLM
returned malformed JSON or raised. Without this, a content payload the
SLM consistently chokes on would re-enter the queue indefinitely and
generate cost on every drain.

---

## Queue runner

`_select_pending_entity_extraction(db, batch_size)`
(`entity.py:515-570`) drains `entity_extraction_queue` in batches:

```sql
SELECT memory_id, attempts FROM entity_extraction_queue
WHERE attempts < ?   -- ENTITY_EXTRACT_MAX_ATTEMPTS, default in config.py
ORDER BY enqueued_at
LIMIT ?
```

Returned rows feed `extract_pending_impl(batch_size, dry_run)`
(`entity.py:572-641`). For each:

- Run `_run_entity_extractor`.
- On success, delete the queue row.
- On failure, bump `attempts`, set `last_attempt_at`. After
  `ENTITY_EXTRACT_MAX_ATTEMPTS`, the row is **dead-lettered** (left in
  place with `attempts >= max`, no longer selected).

`dry_run=True` returns the candidate set without invoking the SLM —
useful for inspecting queue depth or rehearsing a drain.

The runner is the path the operator hits when a queue accumulates after
a write spike. It is also driven on schedule via the CLI command
`m3-memory enrich-pending --entities` (see `bin/m3_enrich.py`).

---

## MCP read surface

| Tool | Function | Signature |
|---|---|---|
| `entity_search` | `entity_search_impl` (`entity.py:695-757`) | `entity_search_impl(query, entity_type=None, k=10, with_neighbors=False) -> list[dict]` |
| `entity_get` | `entity_get_impl` (`entity.py:759-853`) | `entity_get_impl(entity_id, depth=1) -> dict` |
| `extract_pending` | `extract_pending_impl` (`entity.py:572-641`) | `extract_pending_impl(batch_size=20, dry_run=False) -> dict` |
| `entity_extractor_health` | `entity_extractor_health` (`entity.py:643-693`) | `entity_extractor_health() -> dict` (queue depth, dead-letter count, sem capacity) |

Return shapes are pinned by `tests/test_memory_core_parity.py` (signature
snapshot) and `tests/capture_entity_baseline.py` (behavior — see below).
See [`docs/MCP_TOOLS.md`](../MCP_TOOLS.md) for the catalog row.

`entity_search_impl` matches on `canonical_name LIKE ?` first, falls
back to the embedding cosine path on miss. `with_neighbors=True` joins
through `entity_relationships` to inline 1-hop neighbors per result.

`entity_get_impl` returns the entity row + its `memory_entity_links`
(back-references to memories that mention it) + its
`entity_relationships` out to `depth` hops (clamped at 3).

---

## Concurrency / module state

Two mutables matter, both externally imported through the shim with
identity preserved:

| Symbol | Type | Where set | Identity invariant |
|---|---|---|---|
| `_ENTITY_EXTRACT_SEM` | `asyncio.Semaphore(ENTITY_EXTRACT_CONCURRENCY)` | `entity.py:118` | `id(memory_core._ENTITY_EXTRACT_SEM) == id(memory.entity._ENTITY_EXTRACT_SEM)` |
| `_PENDING_ENTITY_TASKS` | `set[asyncio.Task]` | `entity.py:119` | same |
| `VALID_ENTITY_TYPES` / `_PREDICATES` | `frozenset[str]` | `entity.py:167` (return of `load_entity_vocab`) | same |

The semaphore default is `ENTITY_EXTRACT_CONCURRENCY = 2` (env-tunable
via `M3_ENTITY_EXTRACT_CONCURRENCY`). Two concurrent SLM calls is
deliberately conservative — the extractor is the second-largest SLM
consumer in the system after fact enrichment.

`_PENDING_ENTITY_TASKS` exists because `asyncio.create_task` returns a
weakref; without a hard reference the GC can collect a pending task
before it runs. Tasks self-discard from the set in their `done`
callback.

---

## Cycle-breaking

`entity.py` follows the same pattern as `db.py` and `embed.py`:

- **Top-level imports from sibling submodules** (`memory.db._db`,
  `memory.embed._get_embed_client`, `memory.util.sha256_hex`) — allowed
  because the eager-import order in `bin/memory/__init__.py` puts
  `entity` after `db` and `embed`.
- **One lazy import** for the callback into `memory_core`:
  `_track_cost`. Imported inside `_run_entity_extractor` and
  `extract_pending_impl` at first telemetry call. Same single-line
  pattern as the embed-side telemetry hook.
- **No `_resolve_mc_callbacks()` globals shim needed.** This is what
  makes Phase 6 the simplest extraction in the project — `search.py`
  needed a 9-symbol globals-binding shim because the search impls call
  back into 4 graph helpers + 5 misc functions that stayed in
  `memory_core`. Entity has exactly one such callback, and it's
  telemetry-only.

See `docs/MEMORY_CORE_MODULARIZATION_LESSONS.md` §5 (cycle-breaking
patterns) for the comparative analysis.

---

## Out of scope (lives elsewhere)

| Symbol | Lives in | Why it stayed |
|---|---|---|
| `_entity_graph_neighbor_ids` | `bin/memory_core.py` | Part of the graph-traversal subsystem read by `search.py` via a separate shim. Not entity-CRUD. |
| `_ENTITY_MENTION_RE` | `bin/memory/search.py` | Query-side entity mention regex; used by the routed search path, not the extractor. |
| `entity_relationships` table reads | `bin/memory_core.py` graph helpers | The table is **written** by `_link_entity_relationship` here, but **read** by graph traversal helpers that stay in memory_core. |
| `_extract_event_sentences`, event-extraction code | `bin/memory_core.py` | Different subsystem; uses `_EVENT_PROPER_NOUN` from `search.py` via the shim. |

If a fix touches anything in this table, the surface is bigger than
`entity.py` alone — start by reading
`docs/MEMORY_ENTITY_EXTRACTION_PLAN.md` §"Out of scope".

---

## Behavior-parity oracle

`tests/capture_entity_baseline.py` — built before the extraction per
lesson #6 ("build the behavior baseline BEFORE the extraction").

- **Sample**: 30 entities × 3 query variants (exact canonical, lowercased,
  with-trailing-noise). Seeded random sample over
  `entities ORDER BY id LIMIT 1000`.
- **Coverage**: `entity_search_impl(name)`,
  `entity_search_impl(name, entity_type=…)`,
  `entity_get_impl(id, depth=1)`.
- **Fingerprint**: sorted result-ids + `entity_type` + neighbor-count
  per row, byte-hashed into
  `.scratch/migration_baseline/entity_baseline.json`.
- **Workflow**:
  - Capture: `M3_ENTITY_REFRESH_BASELINE=1 python tests/capture_entity_baseline.py`
  - Verify: bare `python tests/capture_entity_baseline.py` — exits 0 on
    match, 1 with a per-query drift report.

**Out of scope for the baseline** — both intentionally:

- `_run_entity_extractor`: SLM call is non-deterministic across runs.
- `extract_pending_impl`: mutates the queue table.

These are covered structurally by `tests/test_memory_core_parity.py`
(signatures) and the unit suite (mocked-SLM cases).

A retrieval-baseline drift caveat hit during Phase 6.1: the
underlying corpus had shifted due to a 287-row curation delete earlier
in the session, so a stale baseline captured pre-delete will flag
spurious differences. Refresh the baseline after corpus mutations
unrelated to the extraction itself.

---

## Cross-references

- Plan + per-commit log:
  [`docs/MEMORY_ENTITY_EXTRACTION_PLAN.md`](../MEMORY_ENTITY_EXTRACTION_PLAN.md)
- Modularization status (now includes Phase 6):
  [`docs/MEMORY_CORE_MODULARIZATION.md`](../MEMORY_CORE_MODULARIZATION.md)
- Lessons (mutable-identity, lazy imports, parity snapshot):
  [`docs/MEMORY_CORE_MODULARIZATION_LESSONS.md`](../MEMORY_CORE_MODULARIZATION_LESSONS.md)
- MCP catalog row:
  [`docs/MCP_TOOLS.md`](../MCP_TOOLS.md)
- Env vars:
  [`docs/ENVIRONMENT_VARIABLES.md`](../ENVIRONMENT_VARIABLES.md#entity-relation-graph)
- Sibling per-tool docs:
  [`docs/tools/memory_embed.md`](memory_embed.md),
  [`docs/tools/memory_search.md`](memory_search.md)
- Commits: `b05c821` (baseline tooling), `6cdd8a3` (extraction)
