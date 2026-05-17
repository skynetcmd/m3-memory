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

import json
import logging
import re
from datetime import date, datetime

from . import config
from .db import _db
from .util import (
    _batch_cosine_py,
    _unpack_many,
    _HAS_NUMPY,
    _np,
)

logger = logging.getLogger("memory.search")


# ──────────────────────────────────────────────────────────────────────────────
# Query-router regexes (also used by event-extraction, which still lives in
# memory_core — it imports `_EVENT_PROPER_NOUN` back through the shim).
# ──────────────────────────────────────────────────────────────────────────────
# Used by event-extraction (in memory_core for now). Re-exported through the
# memory_core shim so legacy callers continue to find it under memory_core.
_EVENT_PROPER_NOUN = re.compile(r"\b([A-Z][a-z]{2,})\b")

# Query-type routing for retrieval. When QUERY_TYPE_ROUTING is on and a query
# looks like "When/what date ... <ProperNoun>", shift vector_weight toward
# BM25 so proper-noun signal doesn't get diluted by embedding similarity.
_TEMPORAL_QUERY_RE = re.compile(
    r"\b(when|what\s+date|which\s+day|on\s+what)\b", re.IGNORECASE,
)

# Hoisted out of _apply_temporal_boost so it isn't re-compiled per search call.
# These match ISO `YYYY-MM-DD` and `D Month YYYY` shapes inside the query.
_DATE_RE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_RE_LONG = re.compile(
    r"\b(\d+)\s+(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+(\d{4})\b",
)
_DATE_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


def _pull_predecessor_turns(scored: list) -> None:
    """Append turn N-1 to ``scored`` when turn N is already present.

    Used under M3_INTENT_ROUTING with intent_hint="user-fact" — bridges
    the gap where the assistant echo is the best FTS match but the
    user's original statement (one turn earlier) carries the actual
    fact. Mutates the list in-place with the predecessor scored at
    ~85% of the original turn's score so it competes but doesn't
    automatically displace.

    Caps at the top 10 current hits to bound extra DB work; most
    user-fact queries only need a few predecessors, not a bulk pull.
    Items without ``conversation_id`` or ``metadata_json.turn_index``
    are skipped.
    """
    candidates: list[tuple[str, int, float]] = []  # (cid, target_idx, parent_score)
    seen_ids = {item.get("id") for _, item in scored if item.get("id")}
    for score, item in scored[:10]:
        cid = item.get("conversation_id")
        meta_raw = item.get("metadata_json")
        if not cid or not meta_raw:
            continue
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
            t_idx = meta.get("turn_index")
            if isinstance(t_idx, int) and t_idx > 0:
                candidates.append((cid, t_idx - 1, score))
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    if not candidates:
        return
    # Single batched query: pull all turns from the affected conversations in
    # one round-trip, then filter to the exact (cid, turn_index) pairs and the
    # per-candidate parent_score in Python.
    cids: set[str] = {c for c, _, _ in candidates}
    wanted: dict[tuple[str, int], float] = {}
    for cid, t_idx, p_score in candidates:
        key = (cid, t_idx)
        # Multiple top-10 hits in the same conv may share a target_idx; keep
        # the parent_score of the higher-ranked hit (first occurrence wins).
        wanted.setdefault(key, p_score)
    try:
        with _db() as db:
            placeholders = ",".join("?" * len(cids))
            rows = db.execute(
                f"SELECT id, content, title, type, importance, metadata_json, "
                f"  conversation_id, "
                f"  CAST(json_extract(metadata_json, '$.turn_index') AS INTEGER) AS turn_index "
                f"FROM memory_items "
                f"WHERE conversation_id IN ({placeholders}) AND is_deleted = 0",
                tuple(cids),
            ).fetchall()
        for row in rows:
            tkey = (row["conversation_id"], row["turn_index"])
            if tkey not in wanted:
                continue
            if row["id"] in seen_ids:
                continue
            seen_ids.add(row["id"])
            pre_item = {
                "id": row["id"],
                "content": row["content"],
                "title": row["title"],
                "type": row["type"],
                "importance": row["importance"],
                "metadata_json": row["metadata_json"],
                "conversation_id": row["conversation_id"],
            }
            scored.append((wanted[tkey] * 0.85, pre_item))
    except Exception as e:  # defensive — predecessor pull is best-effort
        logger.debug(f"predecessor pull skipped: {type(e).__name__}: {e}")


def _maybe_route_query(query: str, vector_weight: float, intent_hint: str = "") -> float:
    """Decide whether to shift vector_weight toward BM25 based on query shape.

    Two triggers — an SLM-supplied intent hint takes precedence, then the
    heuristic fires as a fallback:
      - intent_hint in {"temporal-reasoning", "multi-session"} -> 0.3
      - QUERY_TYPE_ROUTING on AND query starts with "when/what date/..."
        AND contains a proper noun -> 0.3
    Both require the M3_QUERY_TYPE_ROUTING env gate. intent-hint path
    ALSO works standalone when M3_INTENT_ROUTING is on (so bench callers
    can opt in without touching both knobs).
    """
    # Intent-hint path: trusted signal from an upstream classifier.
    if intent_hint and (config.QUERY_TYPE_ROUTING or config.INTENT_ROUTING):
        if intent_hint in ("temporal-reasoning", "multi-session"):
            return 0.3
    # Heuristic path: unchanged from before.
    if not config.QUERY_TYPE_ROUTING:
        return vector_weight
    if not query:
        return vector_weight
    if not _TEMPORAL_QUERY_RE.search(query):
        return vector_weight
    if not _EVENT_PROPER_NOUN.search(query):
        return vector_weight
    return 0.3


# ──────────────────────────────────────────────────────────────────────────────
# Ranker post-processing: recency bonus, elbow trim, temporal boost
# ──────────────────────────────────────────────────────────────────────────────
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


# ──────────────────────────────────────────────────────────────────────────────
# Cross-encoder reranker (lazy-loaded)
# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton — pays the model-load cost only when rerank=True is
# first hit. Default model is the canonical ms-marco distilled cross-encoder
# (~120MB on disk, ~12MB resident weights), small + fast enough for per-query
# reranking at bench scale (~50ms / pair on GPU, ~200ms / pair on CPU).
#
# CONTRACT: importing this module does NOT import sentence_transformers —
# only the first call to _get_reranker(...) does. Keeps cold-start fast for
# callers that don't use rerank.
_RERANKER_MODEL = None  # CrossEncoder | None — lazy-init
_RERANKER_MODEL_NAME = ""


def _get_reranker(model_name: str):
    """Lazy-load + cache cross-encoder reranker.

    Reuses the cached instance if model_name matches the previously-loaded one;
    otherwise loads the new model (and discards the prior). GPU is used if
    available; falls back to CPU silently.

    Raises RuntimeError with a clear install hint if sentence-transformers is
    not importable (it is a hard dep in requirements.txt; missing import means
    the user has a broken install).
    """
    global _RERANKER_MODEL, _RERANKER_MODEL_NAME
    if _RERANKER_MODEL is not None and _RERANKER_MODEL_NAME == model_name:
        return _RERANKER_MODEL
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as e:
        raise RuntimeError(
            f"rerank=True requires sentence-transformers (declared in "
            f"requirements.txt). Install/repair via: "
            f"pip install -r requirements.txt. Original error: {e}"
        ) from e
    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        device = "cpu"
    _RERANKER_MODEL = CrossEncoder(model_name, device=device)
    _RERANKER_MODEL_NAME = model_name
    return _RERANKER_MODEL


def _enforce_expansion_displacement_guard(
    hits: list,
    *,
    protected_ranks: int = config.EXPANSION_PROTECTED_RANKS,
    margin: float = config.EXPANSION_DISPLACEMENT_MARGIN,
) -> list:
    """Enforce: at ranks 1..protected_ranks, expansion rows may only outrank a
    primary row if expansion_score >= margin * primary_score.

    Operates on a list[tuple[score, dict]] in current ranked order. Items are
    classified as "expansion" if dict["_expanded_via"] is set and != "primary";
    everything else (including missing tag, "primary") is treated as primary.

    The pass walks rank 1..protected_ranks. At each protected rank, if the row
    is an expansion that fails the margin test against the next primary row in
    the list, swap them. The same primary is then locked at that rank; we move
    on to the next protected rank. Beyond protected_ranks, the original order
    is preserved.

    Idempotent on already-conforming lists. No-op if protected_ranks <= 0 or
    margin <= 1.0 (treating margin <= 1.0 as "no displacement allowed at all"
    would be too strict; instead we treat it as "feature disabled, score-only").

    Defaults are snapshotted at import time (legacy behavior). For dynamic
    env-var overrides, callers should pass explicit values.
    """
    if not hits or protected_ranks <= 0 or margin <= 1.0:
        return hits

    def _is_expansion(item) -> bool:
        if not isinstance(item, dict):
            return False
        tag = item.get("_expanded_via")
        return bool(tag) and tag != "primary"

    # Rust path: classification stays here (it knows _expanded_via); the Rust
    # core computes the reordering permutation, which we apply to the original
    # (score, item) rows.
    if config.m3_core_rs is not None:
        typed = [(float(s), _is_expansion(it)) for s, it in hits]
        perm = config.m3_core_rs.enforce_displacement_guard(typed, protected_ranks, margin)
        return [hits[i] for i in perm]

    work = list(hits)
    n = len(work)
    limit = min(protected_ranks, n)
    for rank in range(limit):
        score, item = work[rank]
        if not _is_expansion(item):
            continue
        # Find the next primary candidate at rank+1..end
        next_primary_idx = None
        for j in range(rank + 1, n):
            if not _is_expansion(work[j][1]):
                next_primary_idx = j
                break
        if next_primary_idx is None:
            continue
        primary_score, _ = work[next_primary_idx]
        if score > 0 and primary_score > 0 and score >= margin * primary_score:
            continue  # expansion earned its rank
        work[rank], work[next_primary_idx] = work[next_primary_idx], work[rank]
    return work


def _apply_rerank(
    hits: list,
    query: str,
    *,
    pool_k: int,
    final_k: int,
    model_name: str,
    blend: float,
) -> list:
    """Re-score top-pool_k hits with cross-encoder; blend with hybrid score.

    Args:
        hits: list[tuple[float, dict]] — output shape of memory_search_*_impl
        query: user query string
        pool_k: how many top hits to rescore (rest are dropped if pool_k < len)
        final_k: how many top hits to return after rerank+blend
        model_name: cross-encoder model id (e.g. "cross-encoder/ms-marco-MiniLM-L-6-v2")
        blend: blend factor — final = blend * ce_score + (1 - blend) * hybrid_score
               1.0 = pure CE replacement (default), 0.5 = average, 0.0 = no-op

    Returns hits in same shape as input, sorted by blended score descending,
    truncated to final_k.

    CONTRACT: when blend=0.0, this is a no-op — returns input hits[:final_k]
    unmodified. Callers that pass rerank=True with blend=0.0 get the same
    behavior as rerank=False (no CE call made).
    """
    if not hits or final_k <= 0 or blend <= 0.0:
        return hits[:final_k]
    pool = hits[: max(pool_k, final_k)]  # never truncate below final_k
    if not pool:
        return []
    reranker = _get_reranker(model_name)
    # Build (query, content) pairs. Skip rows with empty content (rerank can't
    # score them; they fall back to hybrid score via blend).
    pairs = []
    pair_indices = []  # indices into pool that have content
    for i, (_, item) in enumerate(pool):
        content = (item.get("content") or "") if isinstance(item, dict) else ""
        if content:
            pairs.append([query, content])
            pair_indices.append(i)
    if not pairs:
        return pool[:final_k]
    ce_scores = reranker.predict(pairs, show_progress_bar=False)
    pool_ce: list = [0.0] * len(pool)
    for idx, ce in zip(pair_indices, ce_scores):
        pool_ce[idx] = float(ce)
    blended: list = []
    for (hybrid_score, item), ce in zip(pool, pool_ce):
        new_score = blend * ce + (1.0 - blend) * hybrid_score
        blended.append((new_score, item))
    blended.sort(key=lambda t: t[0], reverse=True)
    # Enforce expansion-displacement guard at top ranks so the CE step cannot
    # promote an expansion row past a primary at rank <= protected unless the
    # CE-blended score overwhelmingly outscores the next primary. Without this,
    # rerank with blend=1.0 freely undoes the same invariant applied at fusion.
    blended = _enforce_expansion_displacement_guard(blended)
    return blended[:final_k]


# ──────────────────────────────────────────────────────────────────────────────
# Query routing (auto-route layer + temporal classification)
# ──────────────────────────────────────────────────────────────────────────────
# Module-level temporal regex — same patterns memory `2d1d5812` documented;
# 100% recall on LongMemEval temporal-reasoning, low FPR on others.
_TEMPORAL_ROUTER_PATTERNS = (
    r"\bwhen\b", r"\bhow long\b", r"\bwhat\s+(?:date|day|month|year|time)\b",
    r"\bbefore\b", r"\bafter\b", r"\bsince\b", r"\buntil\b",
    r"\b(?:days?|weeks?|months?|years?)\s+ago\b",
    r"\bfirst\b", r"\blast\b", r"\brecent(?:ly)?\b",
    r"\bearliest\b", r"\blatest\b",
    r"\bwhich\s+\w+\s+first\b", r"\bin\s+what\s+order\b",
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)(?:day)?\b",
    r"\bvalentine'?s?\s+day\b", r"\bchristmas\b", r"\bthanksgiving\b", r"\bnew\s+year'?s?\b",
)
_TEMPORAL_ROUTER_RE = re.compile("|".join(_TEMPORAL_ROUTER_PATTERNS), re.IGNORECASE)

# Module-level entity mention patterns for question-time parsing (Phase 6).
# Regex-only, no SLM — same compilation style as _TEMPORAL_ROUTER_PATTERNS.
# memory_core's `_entity_graph_neighbor_ids` (graph code, stays in memory_core)
# reads `_ENTITY_MENTION_RE` through the shim re-export.
_ENTITY_MENTION_PATTERNS = (
    r'"[^"]+"',                            # double-quoted strings
    r"'[^']+'",                            # single-quoted strings
    r"\b(?:19|20)\d{2}\b",                # 4-digit years (1900–2099)
    r"\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2}\b",   # Month Day
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*",   # Capitalized noun phrases
)
_ENTITY_MENTION_RE = re.compile("|".join(_ENTITY_MENTION_PATTERNS))


# ---------------------------------------------------------------------------
# AUTO routing helpers — Phase 1 refactor.  auto_route=False (default) is a
# strict no-op; the helpers below are only invoked when auto_route=True.
# ---------------------------------------------------------------------------
_UNSET = object()  # module-level sentinel distinguishing "not passed" from "passed default"


def _extract_caller_overrides(local_args: dict, sig_defaults: dict) -> dict:
    """Return only params the caller actually changed from function-signature defaults.

    local_args: the dict of param names → values actually in use (e.g. a subset of locals())
    sig_defaults: dict of param_name -> default_value from the function's signature

    A value is considered an override when it differs from the signature default by
    identity or equality.  String/numeric/bool comparisons use ==; object sentinels
    use `is not`.
    """
    overrides = {}
    for k, v in local_args.items():
        if k not in sig_defaults:
            continue
        default = sig_defaults[k]
        # Use identity check first (catches sentinel objects), then equality.
        if v is not default and v != default:
            overrides[k] = v
    return overrides


def _apply_auto_layer(
    query: str,
    primary_candidates: list,
    current_params: dict,
    sig_defaults: dict,
) -> tuple:
    """Apply AUTO branch values to params. Caller overrides are always preserved.

    current_params: the kwargs dict reflecting what the caller actually passed
    sig_defaults: function-signature defaults (for override detection)

    Resolution order (lowest -> highest priority):
      1. sig_defaults         — function-signature concrete defaults
      2. branch_vals          — AUTO branch values for the chosen branch
      3. caller_overrides     — what the caller explicitly changed from defaults

    Returns:
        (resolved_params: dict, auto_metadata: dict)

    auto_metadata contains: auto_branch, auto_branch_values, caller_overrides, auto_signals
    """
    import auto_route  # local import avoids circular; auto_route has no memory_core deps

    branch = auto_route.decide_branch(query, primary_candidates, current_params)
    branch_vals = auto_route.branch_values(branch, current_params)
    caller_overrides = _extract_caller_overrides(current_params, sig_defaults)

    # Merge layers: defaults -> AUTO branch values -> caller overrides
    resolved = {**sig_defaults, **branch_vals, **caller_overrides}

    return resolved, {
        "auto_branch": branch,
        "auto_branch_values": branch_vals,
        "caller_overrides": caller_overrides,
        "auto_signals": auto_route.signals_summary(query, primary_candidates),
    }


def _apply_sharp_trim(hits, *, threshold_ratio, k_min, k_max):
    """Sharp-branch post-process: keep hits within threshold_ratio of top score, bounded [k_min, k_max].

    hits: list of (score, item_dict) tuples (the canonical routed_impl output shape)
    """
    if not hits:
        return hits
    if k_max and len(hits) > k_max:
        hits = hits[:k_max]
    top_score = hits[0][0] if hits else 0.0
    if top_score <= 0:
        return hits[: max(k_min, 1)]
    threshold = top_score * threshold_ratio
    kept = [h for h in hits if h[0] >= threshold]
    if k_min and len(kept) < k_min:
        kept = hits[:k_min]
    return kept


def is_temporal_query(query: str) -> bool:
    """Returns True if the query uses temporal vocabulary (regex-based, no LLM)."""
    if not query:
        return False
    return bool(_TEMPORAL_ROUTER_RE.search(query))


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
