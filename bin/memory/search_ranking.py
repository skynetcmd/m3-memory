"""Pure ranking helpers extracted from `memory.search` (Phase 4.B follow-up).

Holds the score-shaping helpers that operate purely on already-computed
(score, item) pairs / raw score arrays: recency bonus, elbow trim, temporal
boost, and the batch hybrid-score math. None of these touch memory_core
callbacks, the reranker singleton, or any `_resolve_*` shim — they are
call-in / call-out pure functions safe to import standalone.

CONTRACT: this module must NOT import `memory.search` (that would create a
cycle, since search.py re-imports these names at its top to keep the
memory_core lazy-registry shim resolving to the same function objects).
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from . import config
from .fts import _DATE_RE_ISO, _DATE_RE_LONG
from .util import _HAS_NUMPY, _np

logger = logging.getLogger("memory.search_ranking")


# Hoisted out of _apply_temporal_boost so it isn't re-compiled per search call.
# These match ISO `YYYY-MM-DD` and `D Month YYYY` shapes inside the query.
_DATE_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


def _apply_recency_bonus(scored, recency_bias, explain=False):
    """Add a rank-based recency bonus to each (score, item) pair.

    Items are ranked lexicographically by `valid_from` (ISO-8601 sorts
    correctly as strings). The oldest dated item receives bonus 0, the
    newest receives `recency_bias`, with linear interpolation between.
    Items with empty `valid_from` receive bonus 0. If fewer than two dated
    items exist, the input is returned unchanged.

    Used to break ties in favor of supersession evidence for "what is my
    current X" queries without parsing timestamps.
    """
    if not scored or recency_bias <= 0:
        return scored
    with_vf = [(i, (it.get("valid_from") or "")) for i, (_, it) in enumerate(scored)]
    dated = [(i, v) for i, v in with_vf if v]
    if len(dated) < 2:
        return scored
    dated.sort(key=lambda x: x[1])
    n = len(dated) - 1
    rank_of = {idx: rank for rank, (idx, _) in enumerate(dated)}
    rescored = []
    for i, (s, it) in enumerate(scored):
        bonus = recency_bias * (rank_of[i] / n) if i in rank_of else 0.0
        if explain and "_explanation" in it:
            it["_explanation"]["recency_bonus"] = bonus
        rescored.append((s + bonus, it))
    return rescored


def _trim_by_elbow(ranked: list[tuple[float, dict]], sensitivity: float = 1.5) -> list[tuple[float, dict]]:
    """Trims results where the score drop-off is significantly higher than average.

    Scale-aware (see M3_ELBOW_* env vars):
      * skip pools smaller than ELBOW_MIN_INPUT (default 5) — too few points to estimate avg
      * require the drop to exceed ELBOW_ABS_THRESHOLD in absolute terms
        (default 0.01) — guards against floating-point noise in big haystacks
      * always return at least ELBOW_MIN_RETURN (default 3) — prevents
        catastrophic 1-hit collapse when the top item dominates the average
    """
    if len(ranked) < config.ELBOW_MIN_INPUT:
        return ranked

    # Absolute Quality Gating Floor: only trim if the top result indicates a strong semantic anchor
    if ranked[0][0] < 0.75:
        return ranked

    # Calculate score differences between consecutive results
    diffs = [ranked[i][0] - ranked[i + 1][0] for i in range(len(ranked) - 1)]
    avg_diff = sum(diffs) / len(diffs)
    threshold = max(config.ELBOW_ABS_THRESHOLD, avg_diff * sensitivity)

    # Find the first 'elbow' where the drop is significantly larger than the average,
    # subject to the absolute-threshold guard.
    for i, d in enumerate(diffs):
        if d > threshold:
            # We found an elbow, trim here. Preserve at least ELBOW_MIN_RETURN items.
            return ranked[: max(config.ELBOW_MIN_RETURN, i + 1)]

    return ranked


def _apply_temporal_boost(scored, query, explain=False):
    """Detects dates in query and boosts items with matching or nearby valid_from dates.

    Compiled regexes are module-level; `query.lower()` runs once; each unique
    `valid_from` string is parsed at most once per call via a small dict cache
    (typical retrieval pool has many turns from the same conversation/day, so
    cache hit-rate is high).
    """
    if not scored or not query:
        return scored
    q_lower = query.lower()
    query_dates: list = []
    for mobj in _DATE_RE_ISO.finditer(q_lower):
        try:
            query_dates.append(date(int(mobj.group(1)), int(mobj.group(2)), int(mobj.group(3))))
        except Exception:
            continue
    for mobj in _DATE_RE_LONG.finditer(q_lower):
        try:
            d_, mo, y_ = mobj.groups()
            query_dates.append(date(int(y_), _DATE_MONTHS.index(mo) + 1, int(d_)))
        except Exception:
            continue
    if not query_dates:
        return scored

    vf_cache: dict[str, "date | None"] = {}

    def _parse_vf(vf_str: str):
        cached = vf_cache.get(vf_str)
        if cached is not None or vf_str in vf_cache:
            return cached
        try:
            parsed = datetime.fromisoformat(vf_str.split("T")[0]).date()
        except Exception:
            parsed = None
        vf_cache[vf_str] = parsed
        return parsed

    rescored = []
    for s, it in scored:
        boost = 0.0
        vf_str = it.get("valid_from", "")
        if vf_str:
            vf_date = _parse_vf(vf_str)
            if vf_date is not None:
                for qd in query_dates:
                    diff = abs((vf_date - qd).days)
                    if diff == 0:
                        boost = 0.25
                        break  # max possible -> short-circuit
                    if diff <= 2 and boost < 0.15:
                        boost = 0.15
                    elif diff <= 7 and boost < 0.05:
                        boost = 0.05
        if explain and boost > 0:
            if "_explanation" not in it:
                it["_explanation"] = {}
            it["_explanation"]["temporal_boost"] = boost
        rescored.append((s + boost, it))
    return rescored


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
        len_penalty: float = max(0.3, clen / stt) if clen < stt else 1.0
        out.append(
            raw * len_penalty
            + title_match_boost * title_overlaps[i]
            + importance_weight * float(importances[i])
        )
    return out


# Note: `_recency_bonus_ranks` is captured as a public symbol by the API
# parity snapshot but has ZERO callers inside memory_core (the actually-used
# path is `_apply_recency_bonus`). Kept here for back-compat with any
# external introspection caller. Consider removal after Phase 5 if no
# external use surfaces.
def _recency_bonus_ranks(valid_froms, bias: float) -> list[float]:
    """Linear rank-based recency bonus aligned to ``valid_froms``.

    Same semantics as the legacy ``_apply_recency_bonus``: empty / missing
    ``valid_from`` -> 0.0; dated items get ``bias * rank / (n_dated - 1)`` after
    lex-sort. When fewer than two dated items exist, all zeros.

    Note: this function is captured as a public symbol by the API parity
    snapshot but has zero callers inside memory_core (the actually-used path
    is ``_apply_recency_bonus``). Preserved here for back-compat with any
    external introspection caller.
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


def _explain_reason(expl: dict) -> str:
    """Human-readable one-liner summarizing WHY a result matched, synthesized from
    the numeric _explanation components (pure — no new computation). Names the
    dominant contributing signals so callers can answer "why did you remember
    this?" without parsing the raw dict. Used only under search(explain=True)."""
    parts: list[str] = []
    vec = float(expl.get("vector") or 0.0)
    bm25 = float(expl.get("bm25") or 0.0)
    if vec >= 0.6:
        parts.append("strong semantic match")
    elif vec >= 0.35:
        parts.append("moderate semantic match")
    if bm25 >= 0.5:
        parts.append("keyword (BM25) match")
    if float(expl.get("title_overlap") or 0.0) > 0.0:
        parts.append("title overlaps the query")
    if float(expl.get("importance") or 0.0) >= 0.7:
        parts.append("high importance")
    if float(expl.get("role_boost") or 0.0) > 0.0:
        parts.append("speaker/role match")
    ih = expl.get("intent_hint")
    if ih and ih not in ("", "general"):
        parts.append(f"routed as {ih}")
    if not parts:
        parts.append("weak/hybrid match (no single dominant signal)")
    return "; ".join(parts)
