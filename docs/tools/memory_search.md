# memory_search — retrieval and ranking

> Status: 2026-05-17. Per-tool doc, Phase 5 of the `memory_core` modularization.
> Audience: someone debugging a retrieval miss, tuning ranking, or porting
> the hybrid scorer to a new corpus. Companion to `docs/EMBED_INPUT_RECIPE.md`
> (write-side recipe), `docs/MEMORY_CORE_MODULARIZATION.md` (plan), and
> `docs/MEMORY_CORE_MODULARIZATION_LESSONS.md` (extraction war stories).

---

## Where it lives

`bin/memory/search.py` — **2,308 lines**, the largest single module in the
post-modularization tree. Re-exported through `bin/memory_core.py` as a
shim so external callers continue to import from `memory_core`.

### The four retrieval impls

| Function | Body lines | Role |
|---|---|---|
| `memory_search_scored_impl` | 745 | The headline hybrid scorer (FTS5 + vector, MMR, federation, recency, temporal boost, expansion). Returns `list[tuple[score, item]]`. |
| `memory_search_routed_impl` | 475 | Production entry point. AUTO routing layer + overshoot/re-rank wrapper around `_scored_impl`. |
| `_maybe_expand_routed` | 120 | Expansion helper — pulls graph / session / entity-graph neighbors and merges them into a routed result. |
| `memory_search_multi_db_impl` | 87 | Reciprocal-rank-fusion (RRF) across multiple databases (handoff / federation peers). |
| `memory_search_impl` | 62 | String-formatting wrapper. The MCP-tool surface most callers actually hit. |

Audit-derived legacy line ranges and counts: see
`docs/MEMORY_CORE_MODULARIZATION_LESSONS.md` §Phase 4.B sub-6+7. Graph
helpers (`_graph_neighbor_ids`, `_session_neighbor_ids`,
`_entity_graph_neighbor_ids`, `_score_extra_rows`) **stay in
`memory_core`** — see "Cycle-breaking" below.

---

## Cycle-breaking — `_resolve_mc_callbacks`

`search.py` cannot `from memory_core import _track_cost` at top level:
`memory_core` does `from memory import search` near its own top, so
`search.py`'s body executes **before** `memory_core` has defined those
symbols. A top-level import here would raise `ImportError` against a
partial module.

Solution: lazy bind at first call (`search.py:81-109`).

```python
_MC_CALLBACK_NAMES = (
    "_cosine",
    "_prefer_observations_gate",
    "_two_stage_observations_gate",
    "_track_cost",
    "_graph_neighbor_ids",
    "_session_neighbor_ids",
    "_entity_graph_neighbor_ids",
    "_score_extra_rows",
    "memory_graph_impl",
)

def _resolve_mc_callbacks() -> None:
    global _MC_CALLBACKS_BOUND
    if _MC_CALLBACKS_BOUND:
        return
    import memory_core as _mc
    g = globals()
    for n in _MC_CALLBACK_NAMES:
        g[n] = getattr(_mc, n)
    _MC_CALLBACKS_BOUND = True
```

Every retrieval impl calls `_resolve_mc_callbacks()` once before its
first reference to a bound name. By that point `memory_core` is fully
loaded, so binding always succeeds.

### Why PEP 562 `__getattr__` does NOT work here

A module-level `__getattr__` only fires for **external attribute
access** on the module object (e.g. `memory.search._cosine` from
another file). It does **not** fire for `LOAD_GLOBAL` opcodes inside
`search.py`'s own functions, because the interpreter resolves bare
names against the module's `globals()` dict directly — `__getattr__`
is never consulted for an own-module lookup. The bare-name references
in `_scored_impl` (`_cosine(...)`, `_track_cost(...)`, etc.) would
still raise `NameError`.

The explicit-binding approach also has a debuggability win: after the
first call, `dir(memory.search)` shows the 9 bound symbols, and
`memory.search._cosine is memory_core._cosine` returns True. Lazy
dispatch via `__getattr__` would hide them.

Full rationale: `MEMORY_CORE_MODULARIZATION_LESSONS.md` §5.

---

## Hybrid scoring

`memory_search_scored_impl` runs FTS5 (BM25) and vector cosine in one
SQL query (LEFT JOIN on `memory_embeddings`), then blends per-row.

### `_hybrid_score_batch` (`search.py:688-752`)

```python
raw       = vector * vw + bm25_norm * (1 - vw)         # vw = vector_weight
bm25_norm = 1.0 / (1.0 + abs(bm25))                    # rank-stable normalization
penalty   = max(0.3, len/STT) if len < STT else 1.0    # short-turn penalty
final     = raw * penalty + title_match_boost * title_overlap
                          + importance_weight * importance
```

Three implementations in fall-through order:

1. **Rust** — `m3_core_rs.hybrid_score_batch(...)` (rayon-parallel,
   SIMD-friendly). Authoritative when `config.m3_core_rs is not None`.
2. **numpy-vectorized** — when `_HAS_NUMPY` and Rust is absent.
3. **Pure-Python loop** — last-resort fallback.

### Tunables (env-driven; see `bin/memory/config.py`)

| Knob | Default | Role |
|---|---|---|
| `vector_weight` | 0.7 | Per-call kwarg. Higher → more semantic. Router shifts to 0.3 for temporal queries. |
| `IMPORTANCE_WEIGHT` | low | Multiplier on `memory_items.importance`. |
| `TITLE_MATCH_BOOST` | low | Additive boost when query tokens overlap `title`. |
| `SHORT_TURN_THRESHOLD` | (env) | Length below which the short-turn penalty fires. |
| `SUPERSEDES_PENALTY` | — | Down-weight applied to memories that have been superseded. |

### `_cosine_batch_packed` (`search.py:667-685`)

Scores a query vector against a list of raw SQLite BLOB rows in one FFI
hop. Three paths: Rust (`m3_core_rs.cosine_batch_packed`), numpy
zero-copy `frombuffer` reshape, pure-Python last-ditch. Wrong-length
blobs score 0.0 in every path (Rust returns 0.0; numpy/Python zero-fill
via `_unpack_many`'s ragged branch).

### `_pull_predecessor_turns` (`search.py:140-211`)

When `M3_INTENT_ROUTING` is on and `intent_hint == "user-fact"`, append
turn N-1 to the scored list whenever turn N is already present. Bridges
the gap where the assistant echo is the best FTS hit but the user's
original statement (one turn earlier) carries the actual fact. Capped
to top-10 current hits, single batched DB query, predecessor scored at
~85% of the original turn so it competes without auto-displacing.

---

## MMR — diversity rerank

`_MMR_LAMBDA = 0.7` (`search.py:1184`, hardcoded — not env-tunable).
The selection runs after hybrid scoring + adaptive-k trim, before
federation.

```
mmr_score = λ * relevance − (1 − λ) * max_sim_to_selected
```

Three paths (`search.py:1219-1293`):

1. **Rust** — `m3_core_rs.mmr_rerank_scored(relevance, vectors, λ, k,
   skip_first=True)`. Authoritative when every candidate has an
   embedding AND explanations weren't requested (the Rust path can't
   write per-item `_explanation`).
2. **numpy** — one batched cosine per outer iteration (`_batch_cosine`
   computes max-sim against all already-selected vectors in one gemv).
3. **Pure-Python** — per-pair cosine fallback. Hit only when both Rust
   and numpy are absent.

Pre-MMR, the candidate set is de-duplicated by `content` (string
equality, post-strip) and truncated to `k * 3` (`search.py:1206-1216`).
MMR runs against this pool, returns top-`k`.

---

## Ranker post-processing

Applied **after** MMR, in this order:

### `_apply_recency_bonus` (`search.py:244-271`)

Rank-based, **no timestamp parsing**. Items are lex-sorted by
`valid_from` (ISO-8601 sorts correctly as strings), then each gets
`bias * (rank / (n_dated - 1))`. Oldest → 0, newest → `recency_bias`.
Items with empty `valid_from` get 0. No-op when fewer than two dated
items exist.

Used to break ties in favor of supersession evidence on "what is my
current X" queries without doing expensive timestamp math per row.

### `_trim_by_elbow` (`search.py:274-299`)

Score drop-off detector. Guards:

| Env var | Default | Role |
|---|---|---|
| `M3_ELBOW_MIN_INPUT` | 5 | Skip pools smaller than this (too few points to estimate avg). |
| `M3_ELBOW_ABS_THRESHOLD` | 0.01 | Drop must exceed this in absolute terms (floating-point noise floor). |
| `M3_ELBOW_MIN_RETURN` | 3 | Always return at least this many (prevents 1-hit collapse when top dominates the avg). |

Sensitivity multiplier (`sensitivity=1.5` default) scales the average-
diff threshold. First diff exceeding `max(ABS_THRESHOLD, avg_diff *
sensitivity)` is the elbow; trim there.

### `_apply_temporal_boost` (`search.py:302-362`)

Parses ISO `YYYY-MM-DD` and `D Month YYYY` patterns in the query
(`_DATE_RE_ISO`, `_DATE_RE_LONG`, `_DATE_MONTHS` at module scope —
compiled once, not per-call). For each item, compares `valid_from` to
each query date:

| `|valid_from − query_date|` | Boost |
|---|---|
| 0 days (exact match) | +0.25 (short-circuit) |
| ≤ 2 days | +0.15 |
| ≤ 7 days | +0.05 |
| > 7 days | 0 |

Per-`valid_from` parses are cached in a local dict (typical pool has
many turns from the same conversation/day → high hit rate).

---

## Reranker — cross-encoder

`_apply_rerank(hits, query, pool_k, final_k, model_name, blend)`
(`search.py:477-535`).

`_get_reranker(model_name)` (`search.py:380-409`) is lazy-loaded — the
cross-encoder model loads the first time `rerank=True` is hit, cached
in `_RERANKER_MODEL` / `_RERANKER_MODEL_NAME`. Default model is
`DEFAULT_RERANK_MODEL` (typically `cross-encoder/ms-marco-MiniLM-L-6-v2`,
~120 MB on disk, ~12 MB resident, ~50 ms/pair GPU / ~200 ms/pair CPU).
GPU is used when `torch.cuda.is_available()`.

`sentence-transformers` is a hard dep — missing import raises a clear
install hint (`pip install -r requirements.txt`).

### Blend semantics

```python
final = blend * ce_score + (1 - blend) * hybrid_score
```

| `blend` | Behavior |
|---|---|
| 1.0 | Pure CE replacement (default when `rerank=True`). |
| 0.5 | Average of CE and hybrid. |
| 0.0 | No-op — short-circuit at `blend <= 0.0`, returns `hits[:final_k]` unmodified, **no CE call made**. |

Pool size is `max(pool_k, final_k)` — never truncate below `final_k`.
Rows with empty `content` are skipped from CE scoring and fall back to
hybrid via the blend. After CE blend, `_enforce_expansion_displacement_guard`
re-runs (CE with `blend=1.0` would otherwise undo the same invariant
applied at fusion).

---

## Query routing

### Temporal classifier

`is_temporal_query(query)` (`search.py:655-659`) — regex-only, no SLM.
Patterns at `_TEMPORAL_ROUTER_PATTERNS` (`search.py:543-552`): `when`,
`how long`, `what date/day/month/year/time`, `before/after/since/until`,
`days/weeks/months/years ago`, `first/last/recent/earliest/latest`,
ordering questions, weekday names, common holidays.

Compiled once as `_TEMPORAL_ROUTER_RE`. From memory `2d1d5812`: 100%
recall on LongMemEval temporal-reasoning, low FPR on others.

### `_maybe_route_query` (`search.py:213-238`)

Shifts `vector_weight` toward BM25 for queries that look temporal AND
contain a proper noun:

| Signal | New `vector_weight` |
|---|---|
| `intent_hint in {"temporal-reasoning", "multi-session"}` (with `M3_INTENT_ROUTING` or `M3_QUERY_TYPE_ROUTING`) | 0.3 |
| Heuristic: `M3_QUERY_TYPE_ROUTING` on AND query matches `_TEMPORAL_QUERY_RE` AND contains `_EVENT_PROPER_NOUN` | 0.3 |
| else | unchanged |

The intent-hint path wins over the heuristic. Both require an env gate
— routing is opt-in.

### AUTO layer

`memory_search_routed_impl(...auto_route=False)` is the default and a
strict no-op (the helpers below are only invoked when
`auto_route=True`).

| Helper | Role |
|---|---|
| `_extract_caller_overrides(local_args, sig_defaults)` (`search.py:577`) | Distinguishes "caller didn't pass X" from "caller passed the default value of X". Uses `_UNSET` sentinel (`search.py:574`). |
| `_apply_auto_layer(...)` (`search.py:598-636`) | Branches on classifier output (temporal / multi-session / entity-anchored) and applies branch-specific knobs (`auto_temporal_expand_sessions`, `auto_multi_expand_sessions`, `auto_entity_graph_depth`, etc.). Caller overrides win over AUTO defaults. |
| `_apply_sharp_trim(hits, threshold_ratio, k_min, k_max)` (`search.py:636`) | Aggressive post-filter for AUTO branches that need tight precision. |

### Entity-mention patterns

`_ENTITY_MENTION_PATTERNS` (`search.py:559-566`): quoted strings,
4-digit years, `Month Day`, capitalized noun phrases. Compiled once as
`_ENTITY_MENTION_RE`. Read through the `memory_core` shim by
`_entity_graph_neighbor_ids` (graph code that stays in memory_core).

---

## Expansion — graph / session / entity-graph

`_maybe_expand_routed` (`search.py:2033-2155`) pulls additional rows
into the result set after the routed search returns. Three expansion
sources, each independently toggleable:

| Source | Knob | Helper (stays in memory_core, lazy-bound) |
|---|---|---|
| Graph neighbors | `graph_depth: int = 0` | `_graph_neighbor_ids` |
| Session siblings | `expand_sessions: bool = False`, `session_cap: int = 12` | `_session_neighbor_ids` |
| Entity-graph neighbors | `entity_graph_depth: int = 1` | `_entity_graph_neighbor_ids` |

Expansion rows are scored via `_score_extra_rows` (memory_core) and
merged with the primary hits. Each expansion row carries
`_expanded_via in {"graph", "session", "entity"}` so the displacement
guard can identify it.

### `_enforce_expansion_displacement_guard` (`search.py:412-474`)

```
At ranks 1..protected_ranks, an expansion row may only outrank a
primary row if expansion_score >= margin * primary_score.
```

Defaults: `EXPANSION_PROTECTED_RANKS = 3`, `EXPANSION_DISPLACEMENT_MARGIN
= 2.0` (snapshotted at import). Walks ranks 1..protected, finds the
next primary candidate, swaps the pair if the expansion fails the
margin test. Idempotent on already-conforming lists. No-op when
`protected_ranks <= 0` or `margin <= 1.0`.

Rust path: `m3_core_rs.enforce_displacement_guard(typed, protected, margin)`
returns the reordering permutation; Python applies it. Without this,
`rerank=True` with `blend=1.0` would freely undo the invariant applied
at fusion — same reason the guard re-runs inside `_apply_rerank`.

---

## Federation — remote Chroma fallback

`_query_chroma` (re-exported from `memory.chroma`, called from
`search.py:1308-1316`) fires when local results are weak:

```python
_local_weak = (
    len(ranked) < 3
    or local_top_score < FEDERATION_LOW_SCORE_THRESHOLD
)
```

Hard skips: `conversation_id` filter set (strict scope boundary,
never cross-peer) or `type_filter` set (avoid type pollution from
remote stores).

Federation hits are tagged so audit tooling can identify them. See
`bin/memory/chroma.py` (152 lines) for the HTTP client and result-shape
adapters. Env: `FEDERATION_LOW_SCORE_THRESHOLD` (config.py),
`M3_CHROMA_URL` and friends (env-vars doc).

---

## Vector-kind strategy (dual-embed v022)

| `vector_kind_strategy` | Behavior |
|---|---|
| `"default"` (default) | Score only `memory_embeddings.vector_kind = 'default'` rows. Back-compat with pre-v022 corpora. |
| `"max"` | Score every kind, dedupe by `memory_id` keeping the highest cosine. Use this when the corpus was ingested with `dual_embed=True`. |

SQL row cap differs: `5000` for `"max"`, `2000` for `"default"`
(`search.py:987`) — `"max"` lets through multiple rows per
`(memory_id, vector_kind)` pair before fusion, so the cap has to be
higher to avoid LIMIT-induced bias.

`memory_search_routed_impl` runs a `"max"` overshoot pass on the second
attempt when overshoot is enabled, so dual-embed corpora benefit
automatically. Schema dependency: `vector_kind` column was added in
v022; older databases need migration.

See `docs/EMBED_INPUT_RECIPE.md` §Variants and dual_embed for the
write-side story.

---

## Multi-DB fusion

`memory_search_multi_db_impl` (`search.py:2156-2245`) runs the same
query across multiple SQLite DBs and fuses via reciprocal-rank fusion
(RRF). Used for handoff scenarios (per-agent DBs) and for federation
peers that ship their corpus rather than serving HTTP.

RRF score: `sum(1 / (k + rank_i))` over each DB's ranked list. Stable
under different per-DB score scales — that's why this path doesn't
re-score, it re-ranks.

---

## Behavior-parity baseline

`tests/capture_retrieval_baseline.py` — **60 queries × 3 variants**,
byte-fingerprinted (`capture_retrieval_baseline.py:104,207,217`).
Run before any refactor of the retrieval path; re-run after to
compare.

```
M3_RETRIEVAL_REFRESH_BASELINE=1 python tests/capture_retrieval_baseline.py
# regenerate baseline (use after intentional behavior changes)

python tests/capture_retrieval_baseline.py
# compare against baseline (CI mode)
```

A fingerprint mismatch on any query is a stop signal. Drift means the
extraction changed retrieval order or score, not just structure.

The baseline harness was the prereq for Phase 4.B sub-6+7 — built
**before** the extraction, per Lesson 6 in
`MEMORY_CORE_MODULARIZATION_LESSONS.md` ("Parity test catches
symbol/signature drift, NOT behavior drift"). Cross-ref memory
`a5b5c8ca`.

---

## Cross-references

- Modularization plan + status:
  [`docs/MEMORY_CORE_MODULARIZATION.md`](../MEMORY_CORE_MODULARIZATION.md)
- Extraction lessons (cycle-breaking, parity, identity preservation):
  [`docs/MEMORY_CORE_MODULARIZATION_LESSONS.md`](../MEMORY_CORE_MODULARIZATION_LESSONS.md)
- Embed pipeline (companion to this doc):
  [`docs/tools/memory_embed.md`](memory_embed.md)
- Write-side recipe (cascade, anchors, variants):
  [`docs/EMBED_INPUT_RECIPE.md`](../EMBED_INPUT_RECIPE.md)
- Env-var canonical list:
  [`docs/ENVIRONMENT_VARIABLES.md`](../ENVIRONMENT_VARIABLES.md)
- Rust hot paths: `m3_core_rs.hybrid_score_batch`,
  `m3_core_rs.cosine_batch_packed`, `m3_core_rs.mmr_rerank_scored`,
  `m3_core_rs.enforce_displacement_guard` (sibling repo `m3-core-rs`)
- Decision memories: `2d1d5812` (temporal-router regex), `a5b5c8ca`
  (retrieval baseline prereq), `9f47dceb` (sub-6+7 line-count audit)
