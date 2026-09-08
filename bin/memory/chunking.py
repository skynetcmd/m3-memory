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

# Floor for the token-aware split: below this a window is handed back whole even
# if it still estimates over budget. Guards the degenerate case (a few astral
# characters whose byte length dominates) from recursing to single characters.
_MIN_SPLIT_CHARS = 256
# Depth cap: with halving, 20 levels covers ~1M characters. A belt-and-braces
# bound so a pathological input can never spin (§3 fail safe).
_MAX_SPLIT_DEPTH = 20

DENSE_TARGET_TOKENS = 7000
DENSE_TOKEN_OVERLAP = 500
DENSE_MIN_SUB_CHARS = 2000
_DENSE_ERR_RE = re.compile(r"(\d+)\s*tokens\s*>\s*n_ctx")


def _chunk_for_sliding_window(text: str) -> list[tuple[str, int]]:
    """Split text into overlapping windows that each fit the model's n_ctx.

    The character window (``MAX_CHARS_PER_CHUNK``) is retained as the coarse
    outer bound, but it is NOT sufficient on its own: characters are not tokens.
    A row of dense content -- code, JSON, CJK, base64 -- can exceed n_ctx while
    sitting well under 28000 characters, and this function used to hand such a
    row back as ONE window. Measured: a realistic mixed CJK+UUID row of 11,379
    chars is ~11,381 tokens against a ceiling of 8,192, and returning a single
    window meant the caller's oversize recovery had nothing to split (it bails
    on ``len(sub_texts) <= 1``), so the row was dropped.

    So every window is additionally bounded in TOKENS: any window still over
    budget is split in half, recursively, until it fits or hits a floor. The
    recursion terminates because each split strictly halves the character count
    and the floor stops degenerate cases (a single astral-plane character that
    somehow exceeds the budget cannot be split further).

    Returns ``[(window_text, index), ...]``. Short text still returns exactly
    one window with index 0, so the single-vector back-compat path is unchanged.
    """
    n = len(text or "")
    if n == 0:
        return [("", 0)]

    # Coarse pass: the existing character sliding window.
    if n <= MAX_CHARS_PER_CHUNK:
        coarse = [text]
    else:
        coarse = []
        start = 0
        while True:
            end = start + MAX_CHARS_PER_CHUNK
            if end >= n:
                coarse.append(text[start:n])
                break
            coarse.append(text[start:end])
            start += STRIDE_CHARS

    # Fine pass: bound each window in TOKENS. Imported lazily to keep this
    # module import-cycle-free (tokens.py is pure, but the lazy import also
    # keeps `chunking` usable in contexts where config has not been set up).
    from .tokens import TOKEN_BUDGET, estimate_tokens

    def _split_to_budget(part: str, depth: int = 0) -> list[str]:
        # estimate_tokens (never count_tokens) on purpose: this is a hot,
        # per-window decision and the estimator is conservative -- it may
        # over-split, never under-split. Exactness here would cost a tokenizer
        # call per candidate boundary for no correctness gain.
        if estimate_tokens(part) <= TOKEN_BUDGET or len(part) <= _MIN_SPLIT_CHARS:
            return [part]
        if depth >= _MAX_SPLIT_DEPTH:
            return [part]  # pathological input: hand it back rather than loop
        mid = len(part) // 2
        return _split_to_budget(part[:mid], depth + 1) + \
            _split_to_budget(part[mid:], depth + 1)

    out: list[tuple[str, int]] = []
    for part in coarse:
        for piece in _split_to_budget(part):
            out.append((piece, len(out)))
    return out


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


def _verify_fits_budget(parts: "list[str]") -> "list[str]":
    """Halve any piece still over the token budget, recursively, with a floor.

    The sizing in :func:`_subdivide_dense_chunk` derives ONE chars-per-token
    ratio for a whole chunk and applies a flat 0.90 margin. The original code
    justified never re-checking by saying that margin "should already cover" any
    error — but density is not uniform WITHIN a chunk, and the measured spread
    across content types is 4x (English 4.18 chars/token, base64 1.00). A chunk
    averaging 2.4 can contain a base64 blob at 1.0, so a 10% margin does not
    cover a 4x variance and a sub-chunk could still overflow, leaving the row
    dropped — the exact failure the subdivision exists to prevent.

    `estimate_tokens` never under-counts, so a piece that passes here genuinely
    fits. `DENSE_MIN_SUB_CHARS` and `_MAX_SPLIT_DEPTH` stop a pathological input
    from recursing toward single characters (§3 fail safe).
    """
    from .tokens import TOKEN_BUDGET, estimate_tokens

    def _fit(part: str, depth: int = 0) -> "list[str]":
        if estimate_tokens(part) <= TOKEN_BUDGET or len(part) <= DENSE_MIN_SUB_CHARS:
            return [part]
        if depth >= _MAX_SPLIT_DEPTH:
            return [part]
        mid = len(part) // 2
        return _fit(part[:mid], depth + 1) + _fit(part[mid:], depth + 1)

    out: "list[str]" = []
    for part in parts:
        out.extend(_fit(part))
    return [p for p in out if p]


def _subdivide_dense_chunk(text: str, observed_tokens: int) -> list[str]:
    """Re-split a chunk that overflowed the bge-m3 token ceiling.

    ``observed_tokens`` may be 0 when the server reported the CLASS of error
    without a count (a differently-worded llama.cpp build, or a proxy that
    rewrote the body). Falling back to `[text]` there would return the chunk
    UNSPLIT and the row would be dropped -- recovery must depend on knowing the
    input is too long, not on the server having been chatty about it. So an
    absent count is replaced by the conservative estimate, which is exactly what
    the estimator exists for.
    """
    if not text:
        return [text]
    if observed_tokens <= 0:
        from .tokens import estimate_tokens
        observed_tokens = estimate_tokens(text)
        if observed_tokens <= 0:  # unreachable for non-empty text; belt-and-braces
            return [text]
    chars_per_token = len(text) / observed_tokens
    sub_chars = int(DENSE_TARGET_TOKENS * chars_per_token * 0.90)
    sub_chars = max(sub_chars, DENSE_MIN_SUB_CHARS)
    if sub_chars >= len(text):
        # The derived window covers the whole chunk, i.e. `observed_tokens`
        # claims this text already fits. Do NOT return it unverified: a server
        # that UNDER-reports (an older build, a proxy that rewrote the body, or
        # a count taken before anchor augmentation) would get the chunk back
        # whole and it would overflow again. Verify against the text itself.
        return _verify_fits_budget([text])
    overlap_chars = int(DENSE_TOKEN_OVERLAP * chars_per_token)
    stride = max(sub_chars - overlap_chars, sub_chars // 2)
    out: list[str] = []
    start = 0
    n = len(text)
    while True:
        end = start + sub_chars
        if end >= n:
            out.append(text[start:n])
            break
        out.append(text[start:end])
        start += stride

    return _verify_fits_budget(out)


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
