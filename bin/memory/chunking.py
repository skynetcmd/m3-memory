"""Pure text-window + vector helpers for the embed pipeline.

Split out of embed.py per DESIGN_PHILOSOPHIES §2: these functions read no
module-level MUTABLE state (no breaker, no client/cache/semaphore, no
`global`), so they're safe to live in their own module. The stateful
cascade (breakers, embedder singleton, HTTP client, caches, semaphores)
stays in embed.py, which re-imports these names for backward compatibility
(`from memory.embed import _chunk_for_sliding_window`, etc.) and via
memory_core's lazy registry (`memory_core._chunk_for_sliding_window`).

The chunk-size / dense-recovery constants below are read-only config
(set once from os.environ at import time, never reassigned) — copied
here verbatim from embed.py so this module has no import-time coupling
back to embed.py. Do NOT import `embed` here — that would create a cycle.
"""
from __future__ import annotations

import math
import os
import re

# ──────────────────────────────────────────────────────────────────────────────
# Sliding-window chunking + dense-content recovery
# ──────────────────────────────────────────────────────────────────────────────
MAX_CHARS_PER_CHUNK = int(os.environ.get("M3_EMBED_CHUNK_MAX_CHARS", 28000))
MIN_OVERLAP_CHARS = int(os.environ.get("M3_EMBED_CHUNK_OVERLAP_CHARS", 8000))
STRIDE_CHARS = MAX_CHARS_PER_CHUNK - MIN_OVERLAP_CHARS

DENSE_TARGET_TOKENS = 7000
DENSE_TOKEN_OVERLAP = 500
DENSE_MIN_SUB_CHARS = 2000
_DENSE_ERR_RE = re.compile(r"(\d+)\s*tokens\s*>\s*n_ctx")


def _chunk_for_sliding_window(text: str) -> list[tuple[str, int]]:
    """Split text into overlapping windows for embedding."""
    n = len(text or "")
    if n <= MAX_CHARS_PER_CHUNK:
        return [(text or "", 0)]
    out: list[tuple[str, int]] = []
    idx = 0
    start = 0
    while True:
        end = start + MAX_CHARS_PER_CHUNK
        if end >= n:
            out.append((text[start:n], idx))
            return out
        out.append((text[start:end], idx))
        idx += 1
        start += STRIDE_CHARS


def _order_embeddings(data: list[dict], n_inputs: int) -> list[list[float]] | None:
    """Return embeddings in INPUT order, or None if the response can't be safely
    aligned. An OpenAI-style embeddings response carries a per-item `index`; we
    sort by it. But a server that OMITS index (every item defaults to 0) would
    pass a naive len-check while the vectors are in arbitrary order — storing a
    semantically-WRONG vector under a memory id with no error. Require `index` to
    be a complete permutation of range(n_inputs) before trusting order; reject
    (treat as failure) otherwise."""
    if len(data) != n_inputs:
        return None
    seen = [d.get("index") for d in data]
    if any(ix is None for ix in seen) or sorted(seen) != list(range(n_inputs)):
        return None  # missing / duplicate / out-of-range index -> not alignable
    ordered = sorted(data, key=lambda d: d["index"])
    return [d["embedding"] for d in ordered]


def _subdivide_dense_chunk(text: str, observed_tokens: int) -> list[str]:
    """Re-split a chunk that overflowed the bge-m3 token ceiling."""
    if observed_tokens <= 0 or not text:
        return [text]
    chars_per_token = len(text) / observed_tokens
    sub_chars = int(DENSE_TARGET_TOKENS * chars_per_token * 0.90)
    sub_chars = max(sub_chars, DENSE_MIN_SUB_CHARS)
    if sub_chars >= len(text):
        return [text]
    overlap_chars = int(DENSE_TOKEN_OVERLAP * chars_per_token)
    stride = max(sub_chars - overlap_chars, sub_chars // 2)
    out: list[str] = []
    start = 0
    n = len(text)
    while True:
        end = start + sub_chars
        if end >= n:
            out.append(text[start:n])
            return out
        out.append(text[start:end])
        start += stride


def _mean_pool(vecs: list[list[float]]) -> list[float] | None:
    """Average several sub-chunk vectors into one (standard long-doc embedding),
    then L2-NORMALIZE the result. bge-m3 vectors are unit-length and the store /
    cosine paths assume that invariant — mean-pooling alone yields a sub-unit
    vector (norm < 1), so it MUST be renormalized or it is incomparable to every
    other vector in the store. Returns None if there's nothing to pool."""
    if not vecs:
        return None
    if len(vecs) == 1:
        return vecs[0]  # already a normalized model output
    dim = len(vecs[0])
    acc = [0.0] * dim
    n = 0
    for v in vecs:
        if len(v) != dim:  # defensive: skip a malformed sub-vector
            continue
        for k in range(dim):
            acc[k] += v[k]
        n += 1
    if n == 0:
        return None
    norm = math.sqrt(sum(x * x for x in acc))
    if norm == 0.0:
        return [0.0] * dim  # degenerate (opposing vectors); avoid /0
    return [x / norm for x in acc]  # mean then L2-normalize (the /n cancels)
