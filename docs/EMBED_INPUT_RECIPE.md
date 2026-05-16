# Embed Input Recipe

> Status: 2026-05-16. Companion to `docs/EMBED_DEPLOYMENT.md`. Where
> EMBED_DEPLOYMENT.md explains *how* the embedder is built and which backend
> serves a call, this doc explains *what text* gets sent to the embedder for a
> given memory write or search query.
>
> Operator-facing. Read this when retrieval results surprise you, when
> onboarding a new model, or before changing how callers populate
> `embed_text`.

---

## Table of contents

1. [The cascade — what text actually gets embedded](#the-cascade)
2. [Anchor augmentation (passage-side only, on purpose)](#anchor-augmentation)
3. [Variants and dual_embed](#variants-and-dual_embed)
4. [Cache key — content_hash semantics](#cache-key)
5. [Model-tag namespacing](#model-tag-namespacing)
6. [The pooling/BOS/attention triplet (per-model)](#the-poolingbosattention-triplet)
7. [Cross-references](#cross-references)

---

## The cascade

For every `memory_write` call, the text handed to the embedder is **not** the
raw `content` field. The resolution happens in `bin/memory_core.py` at the
write site (around line 6328):

```python
_et = _augment_embed_text_with_anchors(
    embed_text or content or title, metadata
)
vec, model = await _embed(_et)
```

Precedence:

1. `embed_text` (caller-supplied) — used when present and non-empty
2. `content` — default for most writes
3. `title` — last-resort fallback when both above are empty

Implication: two memories with identical `content` produce **different
vectors** if one caller set `embed_text` and another didn't. The
`memory_write` MCP tool accepts `embed_text` as a parameter — callers that
care about retrieval quality should set it deliberately, not let it default.

At the **enrichment / ingest** path (`memory_core.py:2592-2593`), the
default `embed_text` is computed as
`_augment_embed_text_with_anchors(p["content"] or p["title"], p["metadata"])`
— i.e. enrichment normalizes the cascade to start from `content`, never
from a stale `embed_text`.

---

## Anchor augmentation

`_augment_embed_text_with_anchors` (`memory_core.py:1028`) prepends temporal
anchors from `metadata["temporal_anchors"]` to the embedded text. Format:

```
[2026-05-16, 2026-Q2] <original embed_text>
```

A no-op when `temporal_anchors` is absent or empty. Anchors are truncated to
10 characters each (ISO date prefix).

**Asymmetry is intentional.** Anchors are applied to **passages** (writes,
enrichment), **not to queries** at search time. Reason:

- Anchor tokens are low-mass per-token but proportionally dominate short
  query vectors (3-token query with 3 anchor tokens = 50% anchor mass) far
  more than they dominate 200-token passages (~1.5% anchor mass).
- Symmetric augmentation would make queries spuriously match passages
  whose anchors happen to contain the same date string, regardless of
  semantic relevance.
- Temporal filtering at query time is handled by the SQL `WHERE` clause,
  not by vector cosine. The anchors exist so that semantic queries about
  content from a given period still pull the right passages — they're not
  meant to be a vector-space date filter.

Do **not** "fix" this by augmenting queries too. The asymmetry is the
feature.

---

## Variants and dual_embed

The `memory_write` MCP tool accepts a `variant` parameter. The production
default is `heuristic_c1c4` (speaker prefix + short-turn merge +
entity-enriched embed_text) — verified to give +10.7pp r@10 over baseline
for zero ingest cost (0 LLM calls, ~0.03s/turn).

`dual_embed=True` (default `False`) writes **two** vector rows per memory:

| `vector_kind` | source | notes                                               |
|---------------|--------|-----------------------------------------------------|
| `default`     | pre-enrichment `embed_text` (`_dual_default_embed_text`) | Raw cascade output before any SLM rewriter runs |
| `enriched`    | post-enrichment `embed_text`                       | After `embed_key_enricher` rewrite, anchors re-applied |

Retrieval-side fusion is controlled by `vector_kind_strategy` kwarg on
`memory_search`:

- `"default"` (default) — score only `vector_kind='default'` rows
- `"max"` — score every kind, dedupe by `memory_id` keeping the highest
  cosine. Use this when the corpus was ingested with `dual_embed=True`.

The mid-tier search path (`memory_core.py:5289`) runs `"max"` automatically
on the second pass when overshoot is enabled. Schema dependency:
`vector_kind` column was added in v022; older databases need migration.

---

## Cache key

Embedding cache lookup keys on `(content_hash, embed_model)` where
`content_hash = sha256(text_actually_embedded)` — i.e. the augmented
post-cascade string, not the raw `content` field.

Consequences operators should know:

- Two memories with the same `content` but different `metadata`
  (especially different `temporal_anchors`) will get **different**
  `content_hash` values and therefore separate cache entries.
- The `backfill_content_hash.py` tool re-hashes the corpus periodically. It
  uses the same recipe as the live write path; if the recipe ever changes,
  the backfill tool must change in lockstep or the audit will report
  spurious drift.
- The cache does **not** dedupe across `variant` values — a memory written
  twice with different variants produces two cache entries even when the
  embedded text happens to be identical, because the variant feeds back
  into the `embed_text` derivation upstream.

---

## Model-tag namespacing

Vectors are tagged with the embedding model that produced them, and the
tags form independent cache namespaces:

| Source                        | `embed_model` tag                | Notes                                          |
|-------------------------------|----------------------------------|------------------------------------------------|
| In-process llama.cpp (bge-m3) | `bge-m3-GGUF-Q4_K_M.gguf`        | Default tag; parity-verified cosine ~0.996 vs llama-server bge-m3 |
| llama-server bge-m3 (HTTP)    | `bge-m3-GGUF-Q4_K_M.gguf`        | Same tag — same model, same quant              |
| LM Studio bge-m3              | `text-embedding-bge-m3`          | **Different tag** — different cache namespace, but vectors are cosine-comparable |
| CPU HTTP fallback             | `bge-m3-GGUF-Q4_K_M.gguf`        | Inherits the GGUF tag                          |
| Qwen3-Embedding (bench-only)  | `qwen3-embedding`                | Different vector space — do **not** mix with bge-m3 rows |

Override via `M3_EMBED_GGUF_MODEL_TAG` env var if your deployment uses a
non-default GGUF.

The hard rule from memory `042fd2a3` still applies: **never mix embeddings
from different models or quants in the same database.** Cosine across
spaces is meaningless. If the model changes, all ~24k+ existing rows must
be re-embedded — a one-time cost the operator pays explicitly, not a
silent drift.

---

## The pooling/BOS/attention triplet

The Rust embedder (`crates/m3-embed-llamacpp/src/lib.rs`) currently
hardcodes three parameters that are correct for bge-m3 and **must move
together** if the GGUF model family changes:

| Param                         | Current value             | Right for bge-m3? | What to set for Qwen3-Embedding |
|-------------------------------|---------------------------|-------------------|----------------------------------|
| `LlamaPoolingType`            | `Cls`                     | Yes (BERT, CLS)   | `Mean` (matches Qwen3-Embedding canonical launch `--pooling mean`) |
| `AddBos`                      | `Always`                  | Yes (BGE BOS = [CLS]) | `Never` — Qwen3 doesn't prepend BOS for embedding inputs |
| Attention type (implicit)     | Causal-vs-non-causal inferred from GGUF `general.architecture` | Yes (bge-m3 is encoder, non-causal) | Decoder-only model uses causal attention; `Mean`-over-attention-mask is still well-defined |
| `with_flash_attention_policy` | `ENABLED`                 | Yes (BERT FA OK)  | Verify per llama.cpp version; Qwen3 FA support is version-dependent |

**Today this is fine because the corpus is bge-m3.** A model swap is a
project: change the GGUF, change all three params, re-embed the corpus,
re-tag everything. There is no in-place swap that doesn't corrupt the
index.

When this becomes the right time to refactor: surface
`M3_EMBED_POOLING` ∈ `cls|mean|last|none|auto` (default `auto` = skip
`with_pooling_type` and let GGUF metadata win) and a matching
`M3_EMBED_ADD_BOS` ∈ `always|never|auto`. The `auto` defaults produce
bit-identical behavior on bge-m3 today and correct behavior on
Qwen3/E5/GTE GGUFs out of the box.

---

## Cross-references

- `docs/EMBED_DEPLOYMENT.md` — runtime architecture, build matrix, env vars
- `docs/ENVIRONMENT_VARIABLES.md` — full env var reference
- `bin/memory_core.py` — `_augment_embed_text_with_anchors` (1028),
  enrichment cascade (2592-2593), write-site cascade (6326-6330),
  `_embed`/`_embed_many` (2110/2230), model-tag override (113-115)
- `crates/m3-embed-llamacpp/src/lib.rs` — pooling/BOS/FA hardcodes
  (706-708, 725, 822)
- Decision memories: `042fd2a3` (embedding consistency rule),
  `3827aaff` (variant selection — heuristic_c1c4 is production default),
  `1718c40f` (pooling/caller-audit refactor intentionally deferred)
