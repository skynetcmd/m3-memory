# Dual-Embedding Ingest + Max-Kind Retrieval Fusion

*New in v2026.4.24.1 · opt-in · zero effect on default callers*

## TL;DR

Write **two embedding vectors per turn** — one from the raw content, one
from an SLM-enriched summary — and let retrieval pick whichever vector
scores higher against the query. Useful when no single text
representation wins for every question shape: short / entity-heavy
queries tend to win on the raw vector, while aggregation-style queries
across long histories tend to win on the enriched vector.

```python
# Ingest: write both vectors per turn
await memory_write_bulk_impl(
    items,
    embed_key_enricher=my_enricher,  # async (content, metadata) -> str
    dual_embed=True,
)

# Retrieve: score both, take the winner per memory_id
ranked = await memory_search_scored_impl(
    query,
    vector_kind_strategy="max",
)
```

## Why you might want this

A single embedding vector per turn forces you to choose which textual
representation the search should see:

- **Raw content** retains the original wording and vocabulary the user
  actually used. Good when the query is keyword-adjacent.
- **A fact-dense summary** (extracted by a small LM at ingest time)
  concentrates the semantic signal and ignores filler words. Good when
  the query asks for aggregation ("all the places I've lived") across
  many turns where any single turn is buried in small talk.

Neither wins universally. Dual embedding stores both and lets the query
vote at retrieval time. The fusion rule is simple: for every
`memory_id` that came back, keep the row whose vector has the highest
cosine against the query. `bm25` is per-item (not per-kind), so nothing
is lost in the dedup — only the losing vector's similarity signal is
discarded.

## How it works

### Schema (migration v022)

`memory_embeddings` gains a `vector_kind TEXT NOT NULL DEFAULT 'default'`
column. All pre-v022 rows migrate to `'default'` via the column default,
so any existing caller continues to see byte-identical rows. Dual ingest
writes a second row with `vector_kind='enriched'`.

An index on `(memory_id, vector_kind)` serves the per-kind lookup
pattern.

### Ingest (`memory_write_bulk_impl`)

Two new kwargs:

| Kwarg | Default | Effect |
|---|---|---|
| `embed_key_enricher` | `None` | `async (content, metadata_dict) -> str` hook. When returning a transformed string, it becomes the new `embed_text`. Raising falls back to the anchor-augmented baseline. |
| `dual_embed` | `False` | When `True` **and** the enricher actually transforms `embed_text`, Phase 2 emits two rows per item: `default` from the raw pre-enrichment text, `enriched` from the SLM output. |

Pass-through enrichment (enricher returns `content` unchanged — e.g. a
short-turn shortcut) and `dual_embed=False` both emit a single
`'default'` row. Pre-v022 callers who don't pass `dual_embed` at all
are byte-identical to before.

### Retrieval (`memory_search_scored_impl`)

One new kwarg:

| Kwarg | Default | Effect |
|---|---|---|
| `vector_kind_strategy` | `"default"` | `"default"` pins the SQL join to `vector_kind='default'` (preserves pre-v022 behavior). `"max"` lets all kinds through, then dedupes by `memory_id` keeping the row with the highest query-vector cosine. |

Invalid values raise `ValueError`.

## Worked example

```python
import httpx
from memory_core import memory_write_bulk_impl, memory_search_scored_impl
from slm_intent import extract_text

# 1. Build an enricher that turns a turn into a fact-dense summary.
client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

async def fact_enricher(content: str, metadata: dict) -> str:
    if len(content) < 30:
        return content  # too short, pass through — single-kind row
    facts = await extract_text(content, profile="contextual_keys", client=client)
    if not facts:
        return content  # SLM declined, pass through
    return f"{facts}\n\n{content}"  # prepend facts; original content preserved

# 2. Ingest with dual vectors.
await memory_write_bulk_impl(
    items,                            # same shape as before
    variant="my-dual-experiment",     # isolate from other ingests
    embed_key_enricher=fact_enricher,
    dual_embed=True,
)

# 3. Query with max-kind fusion.
ranked = await memory_search_scored_impl(
    "Where have I lived over the years?",
    k=20,
    variant="my-dual-experiment",
    vector_kind_strategy="max",
)
```

The cost is one extra embedding call per eligible turn at ingest time,
and one extra DB row per eligible turn. Retrieval pays a tiny dedup pass
(linear in the returned candidate pool, bounded by `SEARCH_ROW_CAP`).

## When NOT to use it

- **No enricher available.** `dual_embed=True` without an enricher is a
  no-op — nothing to write a second vector from. The code handles this
  gracefully but there's no benefit.
- **Queries are always keyword-adjacent.** If your retrieval pattern
  consistently wins on raw vectors, the enriched vector just adds storage
  and ingest cost without upside.
- **Storage is tight.** Dual-embed roughly doubles `memory_embeddings`
  row count for eligible turns. Budget accordingly.
- **You need a stable ranking order between runs.** Max-kind fusion is
  deterministic given a fixed query vector, but introducing a new kind
  (or changing the enricher prompt) shifts which vectors exist and
  therefore which one wins per turn.

## Profile setup for the SLM enricher

See [`SLM_INTENT.md`](SLM_INTENT.md) for the full profile schema. The
shipped `config/slm/contextual_keys.yaml` profile is a working starting
point targeting a local LM Studio endpoint. For a cloud backend (opt-in),
see the "Cloud backends are opt-in" section in the same doc.

## Migration compatibility

v022 (`vector_kind` column) is an `ALTER TABLE ADD COLUMN` with
`NOT NULL DEFAULT 'default'`. On current SQLite it's a metadata-only
operation — no row rewrite. The down-migration cleanly drops the column
and its index. If non-`default` rows exist when you run the down, they
collapse into the single pre-v022 shape — you lose the distinction but
not the vectors.

## Tests

- `tests/test_embed_key_enricher.py` covers the enricher hook +
  `dual_embed` flag (back-compat, pass-through, two-row emission,
  no-enricher no-op).
- `tests/test_vector_kind_strategy.py` covers the retrieval kwarg
  (signature, invalid-value, SQL filter presence, max-kind dedupe).
