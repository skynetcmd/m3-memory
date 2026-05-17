# memory_embed — embedding pipeline

> Status: 2026-05-17. Per-tool doc, Phase 5 of the `memory_core` modularization.
> Audience: someone debugging an embedding miss/drift or porting the pipeline
> to a new GGUF model. Companion to `docs/EMBED_INPUT_RECIPE.md` (input
> recipe) and `docs/EMBED_DEPLOYMENT.md` (runtime architecture, builds, env).
> This file points at the *concrete homes* in the post-modularization tree.

---

## Where it lives

| File | Lines | Role |
|---|---|---|
| `bin/memory/embed.py` | 655 | Authoritative module. Cascade, in-process Rust embedder, HTTP fallback, sliding window + dense recovery, anchor augmentation, backend stats, content_hash, canonical-name cache. |
| `bin/memory_core.py` | 4,429 | Legacy shim. Re-exports every public symbol below via `from memory import embed as _mc_embed` + a `from .embed import …` block (`memory_core.py:199-239`). |

Re-exports in the shim (object-identity preserved, not copies):

```
_EMBED_GGUF_PATH, _EMBED_GGUF_MODEL_TAG, _get_embedded_embedder,
_chunk_for_sliding_window, MAX_CHARS_PER_CHUNK, STRIDE_CHARS,
DENSE_TARGET_TOKENS, DENSE_TOKEN_OVERLAP, DENSE_MIN_SUB_CHARS,
_subdivide_dense_chunk, _augment_embed_text_with_anchors, _content_hash,
_get_embed_client, _EMBED_FALLBACK_URL,
_EMBED_BACKEND_STATS, _EMBED_BACKEND_STATS_LOCK,
get_embed_backend_stats, reset_embed_backend_stats,
_EMBED_SEM, _EMBED_BULK_SEM, _embed, _embed_many,
_embed_canonical_cached, _ENTITY_NAME_EMBED_CACHE,
embedder_status_impl, set_embed_override
```

The shim is intentional and load-bearing: external callers (chatlog ingest,
backfill scripts, the API parity test) import these names from
`memory_core`. Do not break the re-exports without updating the parity
snapshot — see `docs/MEMORY_CORE_MODULARIZATION_LESSONS.md` §4 (mutable
container identity).

---

## The cascade

For a single embed call, `_embed(text)` (`embed.py:320-431`) walks:

1. **Cache lookup** keyed on `(content_hash, embed_model)` —
   `memory_embeddings` SELECT, returns immediately on hit. `embed_model`
   is `_EMBED_GGUF_MODEL_TAG` when the in-process embedder is active,
   else `config.EMBED_MODEL`.
2. **In-process Rust embedder** (`_get_embedded_embedder`) — preferred
   path. Calls `embedded.embed([text])[0]` in a thread (Rust releases the
   GIL). Dim-validates once via `_EMBED_DIM_VALIDATED`. Stats label is
   `<backend>-inprocess` from `m3_core_rs.embed_backend_label()`.
3. **CPU HTTP fallback** — only attempted when `_EMBED_GGUF_PATH` is set
   (i.e. the operator opted into the embedded path but the in-process
   instance failed mid-call). POSTs `{"input": [text]}` to
   `{_EMBED_FALLBACK_URL}/embedding` (singular path, default
   `http://127.0.0.1:8082`). Stats label `cpu-http-fallback`.
4. **Primary HTTP** (`M3_EMBED_URL` / LM Studio / llama-server). Last
   resort. Acquires `_EMBED_SEM` (asyncio.Semaphore(4)), retries 3× with
   exponential backoff (2/4/8 s). Stats label `http-primary`.

The text that actually reaches the embedder is **not** raw `content` —
it's the post-cascade, post-anchor-augmentation string. See
[`EMBED_INPUT_RECIPE.md`](../EMBED_INPUT_RECIPE.md) for the
`embed_text → content → title` precedence and call sites in
`memory_core.py`.

### `_embed_many` (batched)

`_embed_many(texts)` (`embed.py:434-596`) is the bulk variant:

- One bulk cache lookup via `SELECT ... WHERE embed_model = ? AND
  content_hash IN (?, ?, …)`. Misses become `miss_texts`.
- In-process path embeds the whole `miss_texts` list in a single
  `embedded.embed(miss_texts)` call (rayon-parallel inside Rust).
- HTTP paths chunk by `EMBED_BULK_CHUNK` (default 1024), each chunk runs
  under `_EMBED_BULK_SEM` (asyncio.Semaphore(`EMBED_BULK_CONCURRENCY`),
  default 4).
- On retry-exhaustion the primary-HTTP path **bisects** the failing
  chunk (`_post_chunk`) — single-row terminal failures are logged and
  the slot is set to `None` rather than crashing the batch.

---

## Anchor augmentation (passage-side only)

`_augment_embed_text_with_anchors(embed_text, metadata)`
(`embed.py:163-188`). Prepends `[anchor1, anchor2] ` to `embed_text`
when `metadata["temporal_anchors"]` is a non-empty list. Each anchor is
truncated to its 10-char ISO prefix.

```python
_augment_embed_text_with_anchors("user got a new laptop",
    {"temporal_anchors": ["2026-05-16", "2026-Q2"]})
# -> "[2026-05-16, 2026-Q2] user got a new laptop"
```

**Asymmetry is intentional.** Anchors apply to passages at write +
enrichment, never to queries at search time. Full rationale in
`docs/EMBED_INPUT_RECIPE.md` (Anchor augmentation section). Short
version: anchor mass dominates short query vectors and produces
spurious matches; date filtering belongs in SQL `WHERE`, not in cosine.

The augmented string is also what feeds `_content_hash`, so the cache
key reflects what was actually embedded — not the raw `content` field.

---

## In-process Rust embedder

Opt-in via env var:

| Env var | Default | Role |
|---|---|---|
| `M3_EMBED_GGUF` | unset | Path to a GGUF model file. Setting this enables the in-process embedder. Empty/unset → fall back to HTTP. |
| `M3_EMBED_GGUF_MODEL_TAG` | `bge-m3-GGUF-Q4_K_M.gguf` | Cache-namespace tag stamped into `memory_embeddings.embed_model`. |

Lazy init (`_get_embedded_embedder`, `embed.py:71-102`):

- Single-shot — `_embedded_embed_checked` ensures we only attempt
  construction once per process. Failure is sticky; we don't re-probe.
- Validates `embedded.embedding_dim() == config.EMBED_DIM`. A mismatch
  demotes to HTTP — preferable to corrupting the index with wrong-dim
  vectors.
- Logs `"embedded llama.cpp embedder active (<path>, dim=<N>)"` on
  success; `"embedded embedder init failed (...) — using HTTP"` on
  failure (typically: GGUF missing, wheel built without
  `--features embedded`, or CUDA OOM during init).

GPU/CPU selection happens inside the Rust crate at wheel build time —
see `docs/EMBED_DEPLOYMENT.md` build matrix (`embedded` / `embedded-cuda`
/ `embedded-vulkan` / `embedded-metal`). At runtime the active backend
is reported by `m3_core_rs.embed_backend_label()` (`"cuda"`/`"vulkan"`/
`"metal"`/`"cpu"`), and `_embedded_label()` (`embed.py:283-290`) appends
`-inprocess` for the stats counter.

---

## HTTP fallback

`_get_embed_client()` (`embed.py:210-250`) returns the process-wide
`httpx.AsyncClient` singleton, pool-tuned via:

| Env var | Default |
|---|---|
| `M3_EMBED_HTTP_MAX_CONNS` | 32 |
| `M3_EMBED_HTTP_MAX_KEEPALIVE` | 16 |
| `M3_EMBED_HTTP_KEEPALIVE_EXPIRY` | 60 s |

The client is loop-aware — `_EMBED_CLIENT_LOOP_ID` tracks
`id(asyncio.get_running_loop())` and the client is rebuilt if the loop
changes (e.g. between pytest async tests). HTTP/2 is disabled
deliberately; llama-server / LM Studio negotiate poorly with h2 in
practice.

`_EMBED_FALLBACK_URL` (`embed.py:253-255`) defaults to
`http://127.0.0.1:8082` — the `m3-embed-server` Windows Service. See
`EMBED_DEPLOYMENT.md` for install steps and the `m3-embed-server`
binary.

### Backend stats

`_EMBED_BACKEND_STATS` (`embed.py:261-262`) is a thread-safe `dict[str,
int]` of `{label: served_call_count}`. Both `_embed` (one bump per call)
and `_embed_many` (one bump per text along the served path) feed it.

```python
from memory_core import get_embed_backend_stats, reset_embed_backend_stats
reset_embed_backend_stats()
# ... do work ...
get_embed_backend_stats()
# -> {'cuda-inprocess': 1234, 'cpu-http-fallback': 7}
```

Typical distributions and what they mean: see `EMBED_DEPLOYMENT.md`
§Observability.

---

## Sliding window + dense recovery

bge-m3 has an 8 k token context. Long inputs are split client-side
before embedding.

| Constant | Default | Env var |
|---|---|---|
| `MAX_CHARS_PER_CHUNK` | 28,000 | `M3_EMBED_CHUNK_MAX_CHARS` |
| `MIN_OVERLAP_CHARS` | 8,000 | `M3_EMBED_CHUNK_OVERLAP_CHARS` |
| `STRIDE_CHARS` | 20,000 | (derived: `MAX - MIN_OVERLAP`) |
| `DENSE_TARGET_TOKENS` | 7,000 | — |
| `DENSE_TOKEN_OVERLAP` | 500 | — |
| `DENSE_MIN_SUB_CHARS` | 2,000 | — |

`_chunk_for_sliding_window(text)` (`embed.py:113-128`) returns
`[(chunk, idx), …]` — single-shot when `len(text) <= MAX_CHARS_PER_CHUNK`,
otherwise overlapping windows.

`_subdivide_dense_chunk(text, observed_tokens)` (`embed.py:137-157`)
handles the rare case where a chunk that fit the char budget still
overflowed bge-m3's token ceiling (CJK, code with no whitespace, base64).
It re-estimates chars-per-token from the failure signal and re-splits
to ~90% of `DENSE_TARGET_TOKENS`. The regex `_DENSE_ERR_RE`
(`embed.py:134`) parses the `"N tokens > n_ctx"` error string from
llama.cpp / llama-server.

The chunking and dense-recovery callers live in `memory_core.py` (write
+ summarize paths) — `embed.py` only exposes the helpers.

---

## Cache key — `_content_hash`

```python
_content_hash(text) == sha256_hex(text.encode("utf-8"))
```

(`embed.py:191-192`, delegates to `memory.util.sha256_hex`.)

**The hashed string is the text actually embedded** — i.e. post-cascade,
post-anchor-augmentation. Two memories with the same `content` but
different `metadata["temporal_anchors"]` will get different
`content_hash` values and therefore separate cache entries. The
`backfill_content_hash.py` tool re-hashes the corpus periodically using
the same recipe; if this recipe changes, the backfill tool must change
in lockstep.

---

## Batched embedding + semaphores

| Knob | Constant | Default | Env var |
|---|---|---|---|
| Per-call serialization (primary HTTP) | `_EMBED_SEM` | 4 | — |
| Bulk-chunk concurrency | `_EMBED_BULK_SEM` | 4 | `EMBED_BULK_CONCURRENCY` |
| Chunk size | `EMBED_BULK_CHUNK` | 1024 | `EMBED_BULK_CHUNK` |

`_EMBED_SEM` only gates the primary-HTTP retry loop in `_embed`. The
in-process and CPU-fallback paths run unbounded — concurrency is gated
inside the embedded dispatcher (`M3_EMBED_STREAMS` etc., see
`EMBED_DEPLOYMENT.md`).

`_EMBED_BULK_SEM` caps how many concurrent chunks `_embed_many` POSTs to
the primary HTTP endpoint. Bisection on failure (`_post_chunk`,
`embed.py:545-570`) recurses under the same semaphore.

---

## The pooling/BOS/attention triplet

The Rust embedder hardcodes three parameters that are correct for bge-m3
and **must move together** if the GGUF model family changes. The
triplet lives at `crates/m3-embed-llamacpp/src/lib.rs:706-708` (in the
sibling repo `m3-core-rs`):

| Param | Value | Why for bge-m3 |
|---|---|---|
| `LlamaPoolingType` | `Cls` | BERT-family, CLS-pooled |
| `AddBos` | `Always` | BGE's BOS == `[CLS]` |
| `with_flash_attention_policy` | `ENABLED` | BERT FA is OK |
| Attention (implicit) | non-causal | bge-m3 is encoder (inferred from GGUF `general.architecture`) |

The first three are intentionally hardcoded for now; surfacing them as
`M3_EMBED_POOLING` / `M3_EMBED_ADD_BOS` env vars is the model-swap
project. See memory `1718c40f` (pooling/caller-audit refactor
intentionally deferred) and `EMBED_INPUT_RECIPE.md` for the full
Qwen3-Embedding swap table.

A swap is **not** in-place — change the GGUF, change all three params,
re-embed the corpus, re-tag everything. There is no migration path
that preserves the index.

---

## Model-tag namespacing

`embed_model` is the cache namespace. Two configurations with the same
model + same quant but different servers share a tag — and therefore
share cache entries:

| Source | `embed_model` tag |
|---|---|
| In-process llama.cpp (bge-m3 Q4_K_M) | `bge-m3-GGUF-Q4_K_M.gguf` |
| `m3-embed-server` HTTP fallback (same GGUF) | `bge-m3-GGUF-Q4_K_M.gguf` |
| LM Studio bge-m3 | `text-embedding-bge-m3` (different namespace) |
| Qwen3-Embedding (bench-only, different model) | `qwen3-embedding` (different vector space — **never mix**) |

Canonical tag: `bge-m3-GGUF-Q4_K_M.gguf`. Override via
`M3_EMBED_GGUF_MODEL_TAG`. The hard rule from memory `042fd2a3`: never
mix embeddings from different models or quants in the same database.
Cosine across spaces is meaningless. Model change → re-embed the entire
corpus.

---

## Smoke test — pipeline-drift detector

`tests/capture_migration_baseline.py` writes
`tests/baselines/embed_smoke.json`: 100 deterministic embeds with
per-vector sha256 fingerprints (`N_EMBED_ROWS = 100`,
`capture_migration_baseline.py:37,81`). Run before any refactor of the
embed path; re-run after to compare.

A single per-vector sha256 mismatch is a stop signal — the pipeline is
producing different bytes for the same input, which means cache rows
written before the change won't match cache rows written after.

This is the embed-side counterpart to the retrieval-baseline harness
described in `docs/tools/memory_search.md`.

---

## Entity-name cache

`_ENTITY_NAME_EMBED_CACHE` (`embed.py:602-617`) is a process-local
`dict[canonical_name, vec]` used by the entity resolver. Bounded by
`ENTITY_NAME_EMBED_CACHE_MAX` (default 50,000, env-tunable); on
overflow the whole dict is dropped and rebuilt rather than evicted
LRU-style — the resolver tolerates cold rebuilds and the overflow case
is rare.

Misses fall through to `_embed`, so this cache *also* benefits from the
`memory_embeddings` content-hash cache.

---

## Status probe

`embedder_status_impl()` (`embed.py:623-655`) — exposed via MCP as
`embedder_status`. Returns:

```python
{"status": "online" | "offline" | "error-<code>",
 "port": 8081,
 "models": [...],
 "binary_found": True,
 "error": None}
```

Targets the **legacy LM Studio path** on port 8081 — *not* the in-process
embedder or the 8082 fallback service. Reads `lms.exe` / `lms` from
`<BASE_DIR>/.m3-lmstudio/bin/`. Use `m3_core_rs.embed_backend_label()`
and `get_embed_backend_stats()` for the in-process / fallback story.

---

## Cross-references

- Runtime architecture, build matrix, env vars:
  [`docs/EMBED_DEPLOYMENT.md`](../EMBED_DEPLOYMENT.md)
- Input-side recipe (cascade, anchors, variants, model tags, pooling):
  [`docs/EMBED_INPUT_RECIPE.md`](../EMBED_INPUT_RECIPE.md)
- Env var canonical list:
  [`docs/ENVIRONMENT_VARIABLES.md`](../ENVIRONMENT_VARIABLES.md)
- Modularization plan + status:
  [`docs/MEMORY_CORE_MODULARIZATION.md`](../MEMORY_CORE_MODULARIZATION.md)
- Lessons (mutable-identity, lazy imports, parity snapshot):
  [`docs/MEMORY_CORE_MODULARIZATION_LESSONS.md`](../MEMORY_CORE_MODULARIZATION_LESSONS.md)
- Pooling/BOS hardcodes: `crates/m3-embed-llamacpp/src/lib.rs:706-708`
  (sibling repo `m3-core-rs`)
- Decision memories: `042fd2a3` (consistency rule), `1718c40f`
  (pooling refactor deferred), `3827aaff` (variant default)
