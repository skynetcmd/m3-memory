"""Retrieval and ranking — Phase 4.B of the modularization.

This module hosts the search hot path (`memory_search_scored_impl`,
`memory_search_routed_impl`, `memory_search_impl`, `memory_search_multi_db_impl`),
the per-batch scoring helpers, ranker (reranker model + MMR + temporal
boost + recency bonus + elbow trim), and the temporal-query router.

Phase 4.B is being extracted incrementally. Initial commit contains just
the scoring helpers (`_cosine_batch_packed`, `_hybrid_score_batch`,
`_recency_bonus_ranks`); search-impls and their support land in later
sub-commits. See `docs/MEMORY_CORE_MODULARIZATION.md`.

## Circular-import policy

`memory_search_routed_impl` will eventually call back into memory_core's
graph code (`_maybe_expand_routed`, `_graph_neighbor_ids`, etc.) — those
stay in memory_core until a future phase. The audit recommends lazy
imports inside the function body for those callbacks so this module
never imports `memory_core` at top level. Top-level imports here are
all stdlib + the `memory.*` package + a few external libs.
"""
from __future__ import annotations

import logging

from . import config
from .util import (
    _batch_cosine_py,
    _unpack_many,
    _HAS_NUMPY,
    _np,
)

logger = logging.getLogger("memory.search")


# Note: `_batch_cosine` does NOT live here — it moved to memory.util because
# the write path (`_check_contradictions`) also calls it. search.py uses it
# via `from .util import _batch_cosine` if a future block needs to.


def _cosine_batch_packed(query, blobs, dim: int) -> list[float]:
    """Score `query` against a list of packed-blob embeddings (the raw SQLite
    BLOB bytes). Single FFI hop when m3_core_rs is loaded; numpy zero-copy
    `frombuffer` fallback when not; pure-Python last-ditch fallback.

    A blob with the wrong byte length scores 0.0 in every path (Rust returns
    0.0; numpy/Python paths zero-fill via `_unpack_many`'s ragged branch).
    """
    if not blobs:
        return []
    if config.m3_core_rs is not None:
        try:
            return config.m3_core_rs.cosine_batch_packed(query, blobs, dim)
        except Exception as e:  # noqa: BLE001 — fall back rather than fail retrieval
            logger.debug(f"cosine_batch_packed Rust path failed, falling back: {e}")
    matrix = _unpack_many(blobs, dim=dim)
    # Lazy: avoid the circular by importing the writer-shared helper here.
    from .util import _batch_cosine
    return _batch_cosine(query, matrix)


def _hybrid_score_batch(
    vector_scores,
    bm25_scores,
    content_lens,
    importances,
    title_overlaps,
    vector_weight: float,
    importance_weight: float,
    title_match_boost: float,
    short_turn_threshold: int,
) -> list[float]:
    """Compute the per-row hybrid score for a batch of candidates.

    Equivalent to the body of the original per-row scoring loop:
        raw = vector * vw + bm25_norm * (1 - vw)
        penalty = max(0.3, len/STT) if len < STT else 1.0
        final = raw * penalty + title_match_boost * title_overlap + iw * importance

    Rust path: rayon-parallel SIMD-friendly arithmetic. Python fallback:
    numpy-vectorized when available, else pure-Python loop.
    """
    n = len(vector_scores)
    if n == 0:
        return []
    if config.m3_core_rs is not None:
        try:
            return config.m3_core_rs.hybrid_score_batch(
                [float(v) for v in vector_scores],
                [float(v) for v in bm25_scores],
                [int(v) for v in content_lens],
                [float(v) for v in importances],
                [float(v) for v in title_overlaps],
                float(vector_weight),
                float(importance_weight),
                float(title_match_boost),
                int(max(1, short_turn_threshold)),
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"hybrid_score_batch Rust path failed, falling back: {e}")
    if _HAS_NUMPY:
        vec = _np.asarray(vector_scores, dtype=_np.float32)
        bm = _np.asarray(bm25_scores, dtype=_np.float32)
        lens = _np.asarray(content_lens, dtype=_np.float32)
        imp = _np.asarray(importances, dtype=_np.float32)
        tit = _np.asarray(title_overlaps, dtype=_np.float32)
        bm25_norm = 1.0 / (1.0 + _np.abs(bm))
        raw = vec * vector_weight + bm25_norm * (1.0 - vector_weight)
        stt = float(max(1, short_turn_threshold))
        penalty = _np.where(lens < stt, _np.maximum(0.3, lens / stt), 1.0)
        out = raw * penalty + title_match_boost * tit + importance_weight * imp
        return out.tolist()
    # Pure-Python fallback
    stt = float(max(1, short_turn_threshold))
    out = []
    for i in range(n):
        bm25_norm = 1.0 / (1.0 + abs(bm25_scores[i]))
        raw = vector_scores[i] * vector_weight + bm25_norm * (1.0 - vector_weight)
        clen = float(content_lens[i])
        penalty = max(0.3, clen / stt) if clen < stt else 1.0
        out.append(
            raw * penalty
            + title_match_boost * title_overlaps[i]
            + importance_weight * float(importances[i])
        )
    return out


# Note: `_recency_bonus_ranks` is captured as a public symbol by the API
# parity snapshot but has ZERO callers inside memory_core (the actually-used
# path is `_apply_recency_bonus`, which lands in this module in a later
# sub-commit). Kept here for back-compat with any external introspection
# caller. Consider removal after Phase 5 if no external use surfaces.
def _recency_bonus_ranks(valid_froms, bias: float) -> list[float]:
    """Linear rank-based recency bonus aligned to ``valid_froms``.

    Same semantics as the legacy ``_apply_recency_bonus``: empty / missing
    ``valid_from`` -> 0.0; dated items get ``bias * rank / (n_dated - 1)`` after
    lex-sort. When fewer than two dated items exist, all zeros.

    Note: this function is captured as a public symbol by the API parity
    snapshot but has zero callers inside memory_core (the actually-used path
    is ``_apply_recency_bonus``, which lands in this module in a later
    sub-commit). Preserved here for back-compat with any external
    introspection caller.
    """
    n = len(valid_froms)
    if bias <= 0 or n < 2:
        return [0.0] * n
    if config.m3_core_rs is not None:
        try:
            return config.m3_core_rs.recency_bonus_ranks(
                [(v or None) for v in valid_froms], float(bias),
            )
        except Exception as e:  # noqa: BLE001
            logger.debug(f"recency_bonus_ranks Rust path failed, falling back: {e}")
    dated_idx = [i for i, v in enumerate(valid_froms) if v]
    if len(dated_idx) < 2:
        return [0.0] * n
    dated_idx.sort(key=lambda i: valid_froms[i])
    denom = len(dated_idx) - 1
    out = [0.0] * n
    for rank, orig in enumerate(dated_idx):
        out[orig] = bias * (rank / denom)
    return out
