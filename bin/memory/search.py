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

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timezone

from m3_sdk import resolve_db_path

from . import config
from .config import (
    DEFAULT_RERANK_MODEL,
    EMBED_DIM,
    ENTITY_SEED_STOPLIST,
    FEDERATION_LOW_SCORE_THRESHOLD,
    IMPORTANCE_WEIGHT,
    INTENT_ROUTING,
    INTENT_USER_FACT_BOOST,
    SEARCH_ROW_CAP,
    SHORT_TURN_THRESHOLD,
    SUPERSEDES_PENALTY,
    TITLE_MATCH_BOOST,
    m3_core_rs,
)
from .chroma import _query_chroma
from .db import _db, _enqueue_access_stamps
from . import graph as _graph_mod
from .graph import _graph_neighbor_ids, _session_neighbor_ids, _entity_graph_neighbor_ids, _score_extra_rows, memory_graph_impl


def _resolve_graph_helper(name: str):
    """Resolve a graph helper at call time so tests that monkeypatch
    `memory_core.<name>` (pre-refactor pattern, e.g. test_entity_graph
    and test_memory_search_routed) take effect.

    Checks memory_core first (where tests patch), falls back to the
    canonical `memory.graph` module.
    """
    try:
        import memory_core as _mc  # type: ignore
        fn = getattr(_mc, name, None)
        if fn is not None:
            return fn
    except ImportError:
        pass
    return getattr(_graph_mod, name)


def _resolve_search_callable(name: str):
    """Same pattern as _resolve_graph_helper but for callables within
    this module that tests monkeypatch through the memory_core shim
    (e.g. memory_search_scored_impl).

    Necessary because Python binds module-local function names at the
    `def` site — once a function `f` is defined inside this module,
    referring to `f` inside another function `g` resolves to *this*
    module's `f`, even if `memory_core.f` has been patched by a test.
    Going through this helper picks up the patched version at call time.
    """
    try:
        import memory_core as _mc  # type: ignore
        fn = getattr(_mc, name, None)
        if fn is not None:
            return fn
    except ImportError:
        pass
    # Resolve via the current module's globals — the canonical local copy.
    return globals()[name]
from .embed import _embed
from .fts import (
    _DATE_MONTHS,
    _DATE_RE_ISO,
    _DATE_RE_LONG,
    _ENTITY_MENTION_RE,
    _EVENT_PROPER_NOUN,
    _TEMPORAL_QUERY_RE,
    _TEMPORAL_ROUTER_RE,
    _compile_fts_query,
    _query_title_token_set,
    _title_overlap_from_qset,
)
from .util import (
    _HAS_NUMPY,
    _batch_cosine,
    _batch_cosine_py,
    _cosine,
    _cosine_batch_packed,
    _np,
    _unpack_many,
)

logger = logging.getLogger("memory.search")


# Cross-module callback names that stay in memory_core. The retrieval impls
# below reference them as bare names; we bind them into this module's globals
# on first call via `_resolve_mc_callbacks()`. The list covers:
#
#   - DB-state gate predicates: `_prefer_observations_gate`,
#     `_two_stage_observations_gate`
#   - telemetry hook: `_track_cost`
#
# Why deferred binding rather than `from memory_core import ...` at top:
# memory_core's import path runs `from memory import search` near its top, so
# this module's body executes BEFORE memory_core finishes defining those
# symbols. A top-level `from memory_core import _track_cost` here would
# raise ImportError (partial module). By the time any impl function is
# actually called, memory_core is fully loaded — so resolving at first call
# always succeeds. See docs/MEMORY_CORE_MODULARIZATION_LESSONS.md §5.
_MC_CALLBACK_NAMES = (
    "_prefer_observations_gate",
    "_two_stage_observations_gate",
    "_track_cost",
)
_MC_CALLBACKS_BOUND = False

# Forward declarations for the deferred-bound callbacks above. These None
# placeholders are overwritten by `_resolve_mc_callbacks()` before any impl
# runs; they exist so static analysis sees the names defined at module scope.
_prefer_observations_gate = None
_two_stage_observations_gate = None
_track_cost = None


def _resolve_mc_callbacks() -> None:
    """Bind memory_core callback symbols into this module's globals.

    Idempotent — subsequent calls short-circuit on `_MC_CALLBACKS_BOUND`.
    The bound names are the exact callables/predicates memory_core defines,
    so call-time behavior is bit-for-bit identical to the legacy code.
    """
    global _MC_CALLBACKS_BOUND
    if _MC_CALLBACKS_BOUND:
        return
    import memory_core as _mc  # type: ignore
    g = globals()
    for n in _MC_CALLBACK_NAMES:
        g[n] = getattr(_mc, n)
    _MC_CALLBACKS_BOUND = True


# ──────────────────────────────────────────────────────────────────────────────
# Query-router regexes (also used by event-extraction, which still lives in
# memory_core — it imports `_EVENT_PROPER_NOUN` back through the shim).
# ──────────────────────────────────────────────────────────────────────────────
# Used by event-extraction (in memory_core for now). Re-exported through the
# memory_core shim so legacy callers continue to find it under memory_core.

# Query-type routing for retrieval. When QUERY_TYPE_ROUTING is on and a query
# looks like "When/what date ... <ProperNoun>", shift vector_weight toward
# BM25 so proper-noun signal doesn't get diluted by embedding similarity.

# Hoisted out of _apply_temporal_boost so it isn't re-compiled per search call.
# These match ISO `YYYY-MM-DD` and `D Month YYYY` shapes inside the query.
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



# ----------------------------------------------------------------------------
# Phase 4.B sub-6+7: the four retrieval impls.
# memory_search_scored_impl, memory_search_routed_impl, _maybe_expand_routed,
# memory_search_multi_db_impl, memory_search_impl.
# Graph helpers (_graph_neighbor_ids, _session_neighbor_ids,
# _entity_graph_neighbor_ids, _score_extra_rows) stay in memory_core and are
# resolved at call time via _resolve_mc_callbacks() to avoid a
# memory_core -> memory.search -> memory_core import cycle. See
# docs/MEMORY_CORE_MODULARIZATION_LESSONS.md section 5.
# ----------------------------------------------------------------------------


async def memory_search_scored_impl(
    query,
    mmr=True,
    k=8,
    type_filter="",
    agent_filter="",
    search_mode="hybrid",
    user_id="",
    scope="",
    as_of="",
    conversation_id="",
    explain=False,
    extra_columns=None,
    recency_bias=0.0,
    vector_weight=0.7,
    adaptive_k=False,
    elbow_sensitivity=1.5,
    adaptive_k_min=0,
    adaptive_k_max=0,
    smart_time_boost=0.0,
    smart_neighbor_sessions=0,
    variant="",
    intent_hint="",
    vector_kind_strategy="default",
    _depth=0,
    _capture_dict: dict = None,
):
    """Hybrid FTS5+vector+MMR search returning a list of (score, item_dict).

    Structured sibling of `memory_search_impl`. Used by benchmarks and other
    callers that need raw result rows (with metadata_json, conversation_id,
    valid_from, etc.) rather than the formatted text output.

    `extra_columns` is an optional list of extra `mi.<column>` names to include
    in each item dict (e.g. ["metadata_json", "conversation_id", "valid_from",
    "valid_to", "user_id"]). Federated Chroma fallback results will NOT have
    these extra fields.

    `intent_hint` is consumed only when M3_INTENT_ROUTING is on (or the
    narrower M3_QUERY_TYPE_ROUTING handles the weight shift). Supported
    values — "user-fact", "temporal-reasoning", "multi-session", "general"
    — match the labels emitted by bin/slm_intent.classify_intent(). Off by
    default; callers can pass the hint without enabling the gate and it'll
    be silently ignored, which is what makes this safe to thread through
    existing call sites.

    `vector_kind_strategy` picks which rows from memory_embeddings to score
    against when v022 dual-embedding is in play:
      - "default" (back-compat): only vector_kind='default' rows.
      - "max": score against every vector_kind; dedupe by memory_id keeping
        the highest vector similarity. Used with dual_embed ingests where
        both a raw ('default') and SLM-enriched ('enriched') vector exist
        per turn, so a turn wins its bucket on whichever representation
        the query favors.
    """
    # Floor: bind every callable to its module-global default FIRST, so that
    # any subsequent step (callback resolution, test-shim lookup) can fail
    # without leaving these names unbound. Python's compile-time scope
    # analysis sees these as locals (because of the assignment), so a
    # missing assignment would make line ~1350 raise NameError on the
    # bare `_batch_cosine(...)` call. The 2026-05-17 chatlog curator hit
    # exactly this when `_resolve_mc_callbacks()` raised before the
    # original try-block ran.
    _embed = globals()["_embed"]
    _db = globals()["_db"]
    _batch_cosine = globals()["_batch_cosine"]
    _query_chroma = globals()["_query_chroma"]

    # Best-effort: callback resolution + test-shim rebinding. Any failure
    # here just leaves the floor bindings above in place.
    try:
        _resolve_mc_callbacks()  # bind memory_core callbacks on first call
    except Exception as e:  # noqa: BLE001 — fail-open is the point
        logger.debug("memory_search_scored_impl: callback resolution failed (%s); using module defaults", e)

    # Test-shim resolution: legacy regression tests monkeypatch these on
    # `memory_core`. Without local rebinding the module-global imports
    # win, defeating the patches. Resolve once per call so the production
    # path pays effectively zero overhead.
    try:
        import memory_core as _mc  # type: ignore
        _embed = getattr(_mc, "_embed", _embed)
        _db = getattr(_mc, "_db", _db)
        _batch_cosine = getattr(_mc, "_batch_cosine", _batch_cosine)
        _query_chroma = getattr(_mc, "_query_chroma", _query_chroma)
    except ImportError:
        pass  # already have floor bindings

    try:
        k = int(k)
    except (TypeError, ValueError):
        k = 8
    _track_cost("search_calls")
    if _depth > 1:
        return []

    vector_weight = _maybe_route_query(query, vector_weight, intent_hint=intent_hint)

    q_vec, _ = await _embed(query)
    if not q_vec:
        return []

    extra_columns = list(extra_columns or [])
    _BASE_COLS = ["id", "content", "title", "type", "importance"]
    _allowed_extra = {
        "metadata_json", "conversation_id", "valid_from", "valid_to",
        "user_id", "scope", "agent_id", "created_at", "source",
    }
    if recency_bias and "valid_from" not in extra_columns:
        extra_columns = extra_columns + ["valid_from"]
    safe_extra = [c for c in extra_columns if c in _allowed_extra and c not in _BASE_COLS]
    extra_sql = (", " + ", ".join(f"mi.{c}" for c in safe_extra)) if safe_extra else ""

    where_clauses = ["mi.is_deleted = 0"]
    params = []

    if type_filter:
        is_exact = (type_filter.startswith('"') and type_filter.endswith('"')) or (type_filter.startswith("'") and type_filter.endswith("'"))
        actual_type = type_filter[1:-1] if is_exact else type_filter
        if is_exact:
            where_clauses.append("mi.type = ?")
        else:
            where_clauses.append("mi.type LIKE ?")
        params.append(actual_type)

    if agent_filter:
        is_exact = (agent_filter.startswith('"') and agent_filter.endswith('"')) or (agent_filter.startswith("'") and agent_filter.endswith("'"))
        actual_agent = agent_filter[1:-1] if is_exact else agent_filter
        if is_exact:
            where_clauses.append("mi.agent_id = ?")
        else:
            where_clauses.append("LOWER(mi.agent_id) = LOWER(?)")
        params.append(actual_agent)

    if user_id:
        where_clauses.append("mi.user_id = ?")
        params.append(user_id)
    if scope:
        where_clauses.append("mi.scope = ?")
        params.append(scope)
    if conversation_id:
        where_clauses.append("mi.conversation_id = ?")
        params.append(conversation_id)
    if variant:
        # Accept "<name>" for exact-variant, "" for unfiltered (default),
        # the sentinel "__none__" for rows where variant IS NULL, or a list /
        # tuple of names for multi-variant retrieval (e.g. paired source +
        # observation variants in the same scoring pass).
        # The "__none__" sentinel inside a list is also honored — it expands
        # to a separate `OR mi.variant IS NULL` clause.
        if isinstance(variant, (list, tuple, set)):
            names = [v for v in variant if v]
            include_null = "__none__" in names
            names = [v for v in names if v != "__none__"]
            sub: list[str] = []
            if names:
                placeholders = ",".join(["?"] * len(names))
                sub.append(f"mi.variant IN ({placeholders})")
                params.extend(names)
            if include_null:
                sub.append("mi.variant IS NULL")
            if sub:
                where_clauses.append("(" + " OR ".join(sub) + ")")
        elif variant == "__none__":
            where_clauses.append("mi.variant IS NULL")
        else:
            where_clauses.append("mi.variant = ?")
            params.append(variant)

    if as_of:
        # Open-ended validity is represented as NULL by new writes; legacy
        # rows may still carry "". Match both so a future write-path change
        # to use NULL exclusively doesn't break historical data.
        where_clauses.append("(mi.valid_from IS NULL OR mi.valid_from = '' OR mi.valid_from <= ?)")
        where_clauses.append("(mi.valid_to   IS NULL OR mi.valid_to   = '' OR mi.valid_to   > ?)")
        params.extend([as_of, as_of])

    # v022: filter the embeddings join by vector_kind unless caller opted
    # into cross-kind fusion. Legacy rows (pre-v022 / single-embed ingests)
    # carry vector_kind='default' via the migration's NOT NULL DEFAULT, so
    # "default" strategy is a strict superset of pre-v022 behavior.
    if vector_kind_strategy == "default":
        where_clauses.append("me.vector_kind = 'default'")
    elif vector_kind_strategy != "max":
        raise ValueError(
            f"vector_kind_strategy must be 'default' or 'max', got {vector_kind_strategy!r}"
        )

    where_sql = " AND ".join(where_clauses)

    def _recurse_semantic():
        return memory_search_scored_impl(
            query, k=k, type_filter=type_filter, agent_filter=agent_filter,
            search_mode="semantic", user_id=user_id, scope=scope, as_of=as_of,
            conversation_id=conversation_id, explain=explain,
            extra_columns=extra_columns, recency_bias=recency_bias,
            vector_weight=vector_weight, adaptive_k=adaptive_k,
            smart_time_boost=smart_time_boost,
            smart_neighbor_sessions=smart_neighbor_sessions,
            variant=variant,
            intent_hint=intent_hint,
            vector_kind_strategy=vector_kind_strategy,
            _depth=_depth + 1,
            _capture_dict=_capture_dict,
        )

    # When strategy="max" the memory_embeddings join returns one row per
    # (memory_id, vector_kind) pair, so a straight LIMIT 1000 would see
    # each item N times (N = distinct kinds stored) and the effective
    # unique-item pool would shrink to 1000/N. Double the SQL-level cap
    # for max-kind so the unique pool stays near 1000. Strategy="default"
    # pins to a single kind, so the base cap already counts unique items.
    sql_row_limit = 5000 if vector_kind_strategy == "max" else 2000

    try:
        with _db() as db:
            if search_mode == "semantic":
                sql = f"""
                    SELECT mi.id, mi.content, mi.title, mi.type, mi.importance, me.embedding, 0.0 as bm25_score{extra_sql}
                    FROM memory_items mi
                    JOIN memory_embeddings me ON mi.id = me.memory_id
                    WHERE {where_sql}
                    ORDER BY mi.created_at DESC
                """
                if os.environ.get("M3_DEBUG"):
                    print(f"DEBUG SQL (semantic):\n{sql}")
                    print(f"DEBUG PARAMS: {params}")
                rows = db.execute(sql, params).fetchall()
                if os.environ.get("M3_DEBUG"):
                    print(f"DEBUG SQL HITS (semantic): {len(rows)}")
            else:
                # ...
                # (omitted for brevity, will do hybrid next)
                sql = f"""
                    SELECT mi.id, mi.content, mi.title, mi.type, mi.importance, me.embedding,
                           bm25(memory_items_fts) as bm25_score{extra_sql}
                    FROM memory_items mi
                    JOIN memory_embeddings me ON mi.id = me.memory_id
                    JOIN memory_items_fts fts ON mi.rowid = fts.rowid
                    WHERE {where_sql} AND memory_items_fts MATCH ?
                    ORDER BY bm25_score ASC
                    LIMIT {sql_row_limit}
                """
                fts_query, ok = _compile_fts_query(query, search_mode)
                if not ok:
                    if search_mode != "fts5":
                        return await _recurse_semantic()
                    return []

                if os.environ.get("M3_DEBUG"):
                    print(f"DEBUG SQL (hybrid):\n{sql}")
                    print(f"DEBUG PARAMS: {(*params, fts_query)}")
                rows = db.execute(sql, (*params, fts_query)).fetchall()
                if os.environ.get("M3_DEBUG"):
                    print(f"DEBUG SQL HITS (hybrid): {len(rows)}")
                if not rows and search_mode != "fts5":
                    return await _recurse_semantic()
    except sqlite3.OperationalError as e:
        if os.environ.get("M3_DEBUG"):
            print(f"DEBUG SQL ERROR: {e}")
        if search_mode != "fts5":
            return await _recurse_semantic()
        return []

    scored = []
    # Under max-kind, trim AFTER dedup so SEARCH_ROW_CAP counts unique items,
    # not kind-duplicated rows. Under default (pins to one kind) the dupes
    # don't exist, so the cap already counts unique items and we trim up-front
    # to avoid an unnecessary cosine batch.
    if vector_kind_strategy != "max" and len(rows) > SEARCH_ROW_CAP:
        rows = rows[:SEARCH_ROW_CAP]

    # Batched vector scoring: pass raw blobs straight to the Rust packed-cosine
    # primitive (single FFI hop, rayon-parallel) or the numpy fallback. This
    # replaces the per-row `struct.unpack` + per-row `cosine` from the legacy
    # code path. Embeddings are only materialized as a list when MMR needs them
    # (lazy `_get_page_matrix` below).
    page_blobs = [r["embedding"] for r in rows]
    page_scores = _cosine_batch_packed(q_vec, page_blobs, EMBED_DIM)

    # Max-kind fusion: when the SQL let through multiple vector_kind rows
    # per memory_id, keep the row with the highest vector similarity so
    # each item scores exactly once downstream. The FTS bm25 value is the
    # same across a memory_id's rows (bm25 is per-item), so dropping the
    # losing vector only discards vector-similarity information.
    if vector_kind_strategy == "max" and rows:
        best: dict[str, int] = {}
        for i, row in enumerate(rows):
            mid = row["id"]
            if mid not in best or page_scores[i] > page_scores[best[mid]]:
                best[mid] = i
        keep_idx = sorted(best.values())
        rows = [rows[i] for i in keep_idx]
        page_scores = [page_scores[i] for i in keep_idx]
        page_blobs = [page_blobs[i] for i in keep_idx]
        # Now trim to the cap — count unique items, not kind-duplicated rows.
        if len(rows) > SEARCH_ROW_CAP:
            rows = rows[:SEARCH_ROW_CAP]
            page_scores = page_scores[:SEARCH_ROW_CAP]
            page_blobs = page_blobs[:SEARCH_ROW_CAP]

    if _capture_dict is not None:
        _capture_dict["pre_seen_content_filter_rows"] = len(rows)

    # ── Vectorized per-row scoring ──────────────────────────────────────────
    # Pull bm25 / content_len / importance / title-overlap as parallel arrays,
    # then hand the whole batch to `_hybrid_score_batch` (Rust rayon / numpy
    # vectorized / pure-Python loop, in that order of preference).
    bm25_arr: list = []
    content_lens: list = []
    importances: list = []
    title_overlaps: list = []
    q_title_set = _query_title_token_set(query)
    title_boost_const = TITLE_MATCH_BOOST
    importance_w = IMPORTANCE_WEIGHT
    short_turn_t = SHORT_TURN_THRESHOLD
    for row in rows:
        bm25_arr.append(row["bm25_score"])
        content_lens.append(len(row["content"] or ""))
        importances.append(float(row["importance"] or 0.0))
        title_overlaps.append(_title_overlap_from_qset(q_title_set, row["title"] or ""))

    final_scores = _hybrid_score_batch(
        page_scores,
        bm25_arr,
        content_lens,
        importances,
        title_overlaps,
        vector_weight=vector_weight,
        importance_weight=importance_w,
        title_match_boost=title_boost_const,
        short_turn_threshold=short_turn_t,
    )

    # Role-biased boost (Piece 2 of intent routing). Sparse — most queries
    # don't have intent_hint set, so the loop body is fully skipped. When
    # active, do a cheap substring pre-check on metadata_json before parsing
    # JSON, since `'"role":"user"'` is what the boost looks for.
    intent_user_fact_active = INTENT_ROUTING and intent_hint == "user-fact"
    role_boosts: list = [0.0] * len(rows)
    if intent_user_fact_active:
        for i, row in enumerate(rows):
            try:
                meta_raw = row["metadata_json"] if "metadata_json" in row.keys() else None
            except (IndexError, KeyError):
                meta_raw = None
            if not meta_raw:
                continue
            # Cheap pre-check: skip JSON parsing when "user" role isn't even
            # mentioned. Avoids `json.loads` on every row in the pool.
            if '"role"' not in meta_raw or '"user"' not in meta_raw:
                continue
            try:
                meta = json.loads(meta_raw)
                if isinstance(meta, dict) and meta.get("role") == "user":
                    role_boosts[i] = INTENT_USER_FACT_BOOST
            except (json.JSONDecodeError, TypeError):
                pass

    # Build the final (score, item) pairs. `item` is constructed by enumerating
    # the row mapping rather than `dict(row); del item["embedding"]` so the
    # 4-8KB embedding blob is never reassigned into a Python object.
    bm25_w_complement = 1.0 - vector_weight
    for i, row in enumerate(rows):
        item: dict = {}
        for key in row.keys():
            if key == "embedding":
                continue
            item[key] = row[key]
        final_score = final_scores[i] + role_boosts[i]
        if explain:
            vector_score = page_scores[i]
            bm25_norm = 1.0 / (1.0 + abs(row["bm25_score"]))
            length_penalty = (
                max(0.3, content_lens[i] / short_turn_t)
                if content_lens[i] < short_turn_t
                else 1.0
            )
            item["_explanation"] = {
                "vector": vector_score,
                "bm25": bm25_norm,
                "importance": row["importance"],
                "raw_hybrid": vector_score * vector_weight + bm25_norm * bm25_w_complement,
                "length_penalty": length_penalty,
                "title_overlap": title_overlaps[i],
                "title_boost": title_boost_const * title_overlaps[i],
                "importance_boost": importance_w * importances[i],
                "vector_weight": vector_weight,
                "intent_hint": intent_hint,
                "role_boost": role_boosts[i],
            }
        scored.append((final_score, item))

    # Apply temporal boost if dates detected in query
    if scored:
        scored = _apply_temporal_boost(scored, query, explain=explain)

    if recency_bias > 0 and scored:
        scored = _apply_recency_bonus(scored, recency_bias, explain=explain)

    # Predecessor pull (Piece 3 of intent routing). For user-fact intent
    # the top-ranked turn is often the assistant echo at index N+1; fetch
    # turn N from the same conversation so the user's original statement
    # enters the candidate set. Only runs at _depth==0 to avoid unbounded
    # recursion, and is capped to the current top 10 hits so the extra
    # DB work stays bounded.
    if INTENT_ROUTING and intent_hint == "user-fact" and _depth == 0 and scored:
        _pull_predecessor_turns(scored)

    _MMR_LAMBDA = 0.7
    pre_ranked_all = sorted(scored, key=lambda x: x[0], reverse=True)

    # Adaptive K: Trim by elbow if requested
    if adaptive_k:
        if _capture_dict is not None:
            _capture_dict["pre_adaptive_k_rows"] = len(pre_ranked_all)
        pre_ranked_all = _trim_by_elbow(pre_ranked_all, sensitivity=elbow_sensitivity)
        if _capture_dict is not None:
            _capture_dict["post_elbow_trim_rows"] = len(pre_ranked_all)
        if adaptive_k_min and len(pre_ranked_all) < adaptive_k_min:
            # Floor: undo the trim when it leaves fewer than the requested minimum.
            pre_ranked_all = sorted(scored, key=lambda x: x[0], reverse=True)[:adaptive_k_min]
        if adaptive_k_max and len(pre_ranked_all) > adaptive_k_max:
            pre_ranked_all = pre_ranked_all[:adaptive_k_max]
        if _capture_dict is not None:
            _capture_dict["post_adaptive_k_rows"] = len(pre_ranked_all)
        if len(pre_ranked_all) < k:
            k = len(pre_ranked_all)

    if _capture_dict is not None:
        _capture_dict["pre_seen_content_dedup_rows"] = len(pre_ranked_all)
    seen_content: set[str] = set()
    pre_ranked: list = []
    for entry in pre_ranked_all:
        c = (entry[1].get("content") or "").strip()
        if c and c in seen_content:
            continue
        if c:
            seen_content.add(c)
        pre_ranked.append(entry)
        if len(pre_ranked) >= k * 3:
            break
    if _capture_dict is not None:
        _capture_dict["post_seen_content_dedup_rows"] = len(pre_ranked)
    if mmr and len(pre_ranked) > k and page_blobs:
        # Tier-A perf: try the zero-unpack Rust path first. `page_blobs` is
        # already the raw bytes from SQL — `mmr_rerank_scored_packed` takes
        # one contiguous bytes buffer and slices internally via PyO3
        # zero-copy borrow, releasing the GIL while MMR runs. Skips the
        # `_unpack_many` reshape entirely when:
        #   - every candidate has a blob (no missing vectors), AND
        #   - the blobs are in pre_ranked order (we can assume so because
        #     pre_ranked rows were derived from `rows`, which is the same
        #     order as `page_blobs`), AND
        #   - explanations aren't requested.
        # Fall through to the unpacked path when any of those don't hold.
        _bytes_per_row = EMBED_DIM * 4
        _id_to_blob_idx = {rows[i]["id"]: i for i in range(len(rows))}
        _packed_blob_indices = [_id_to_blob_idx.get(it["id"]) for _, it in pre_ranked]
        _packed_ok = (
            m3_core_rs is not None
            and not explain
            and all(idx is not None for idx in _packed_blob_indices)
            and all(
                isinstance(page_blobs[idx], (bytes, bytearray))
                and len(page_blobs[idx]) == _bytes_per_row
                for idx in _packed_blob_indices
            )
        )

        if _packed_ok:
            # Reassemble blobs in pre_ranked order (rows order may differ
            # from pre_ranked order after content-dedup). One bytes.join().
            relevance = [float(s) for s, _ in pre_ranked]
            ordered_flat = b"".join(
                bytes(page_blobs[idx]) if isinstance(page_blobs[idx], bytearray)
                else page_blobs[idx]
                for idx in _packed_blob_indices
            )
            sel_idx = m3_core_rs.mmr_rerank_scored_packed(
                relevance, ordered_flat, EMBED_DIM, _MMR_LAMBDA, k, True
            )
            ranked = [pre_ranked[i] for i in sel_idx]
            _emb_lookup = None  # not built; downstream uses ranked only
        else:
            # Unpacked path. _unpack_many is one batched numpy.frombuffer
            # reshape (or list-of-lists fallback when numpy is absent).
            page_matrix = _unpack_many(page_blobs, dim=EMBED_DIM)
            # When _unpack_many returns ndarray, indexing yields a 1-D ndarray row;
            # when it falls back to list-of-lists, indexing yields list[float].
            # Both are valid inputs to m3_core_rs and to numpy cosine.
            _emb_lookup = {rows[i]["id"]: page_matrix[i] for i in range(len(rows))}
            # Rust path (unpacked variant): authoritative when every candidate
            # has an embedding and explanations aren't requested. Same gate as
            # the packed path; only used as a fallback when one of the packed
            # preconditions failed (typically: a row's blob has the wrong
            # length due to a partially-migrated corpus).
            _mmr_vecs = [_emb_lookup.get(it["id"]) for _, it in pre_ranked]
            _unpacked_rust_ok = (
                m3_core_rs is not None
                and not explain
                and all(v is not None for v in _mmr_vecs)
            )
            if _unpacked_rust_ok:
                relevance = [float(s) for s, _ in pre_ranked]
                # Rust wants list[list[float]] — convert ndarray rows on the way out.
                _mmr_vecs_lists = [
                    (v.tolist() if hasattr(v, "tolist") else list(v)) for v in _mmr_vecs
                ]
                sel_idx = m3_core_rs.mmr_rerank_scored(relevance, _mmr_vecs_lists, _MMR_LAMBDA, k, True)
                ranked = [pre_ranked[i] for i in sel_idx]
            else:
                # Python fallback. Pre-stash selected-vector stack so we can compute
                # `max_sim` against all selected at once (one numpy gemv per round)
                # instead of one FFI hop per (candidate, selected) pair.
                selected = [pre_ranked[0]]
                candidates = list(pre_ranked[1:])
                sel_vecs: list = []
                first_vec = _emb_lookup.get(pre_ranked[0][1]["id"])
                if first_vec is not None:
                    sel_vecs.append(first_vec)
                while candidates and len(selected) < k:
                    best_idx, best_mmr = 0, -float('inf')
                    # Build the selected-vector matrix once per outer iteration.
                    if _HAS_NUMPY and sel_vecs:
                        try:
                            sel_mat = _np.asarray(sel_vecs, dtype=_np.float32)
                        except Exception:
                            sel_mat = None
                    else:
                        sel_mat = None
                    for ci, (c_score, c_item) in enumerate(candidates):
                        c_vec = _emb_lookup.get(c_item["id"])
                        if c_vec is None or not sel_vecs:
                            # Candidate has no embedding (vector-side hit absent
                            # from `rows`) OR nothing selected yet. Treat as
                            # max_sim=0 -> MMR reduces to lambda*c_score.
                            max_sim = 0.0
                        elif sel_mat is not None:
                            # One batched cosine across all already-selected vectors.
                            sims = _batch_cosine(c_vec, sel_mat)
                            max_sim = max(sims, default=0.0)
                        else:
                            # Pure-Python last-resort: per-pair cosine. Slow but
                            # only hit when numpy is absent AND Rust is absent.
                            max_sim = max(
                                (_cosine(c_vec, sv) for sv in sel_vecs),
                                default=0.0,
                            )
                        mmr_score = _MMR_LAMBDA * c_score - (1 - _MMR_LAMBDA) * max_sim
                        if mmr_score > best_mmr:
                            best_mmr = mmr_score
                            best_idx = ci
                        if explain:
                            if "_explanation" not in c_item:
                                c_item["_explanation"] = {}
                            c_item["_explanation"]["max_sim_to_selected"] = max_sim
                            c_item["_explanation"]["mmr_penalty"] = (1 - _MMR_LAMBDA) * max_sim
                    chosen = candidates.pop(best_idx)
                    selected.append(chosen)
                    chosen_vec = _emb_lookup.get(chosen[1]["id"])
                    if chosen_vec is not None:
                        sel_vecs.append(chosen_vec)
                ranked = selected
    else:
        ranked = pre_ranked

    # Hard skip: conversation_id is a strict scope boundary we never cross-peer;
    # type_filter should stay local to avoid type pollution from remote stores.
    _skip_federated_hard = bool(conversation_id or type_filter)

    # Soft condition: fire federation when local results are weak (too few or low confidence).
    local_top_score = ranked[0][0] if ranked else 0.0
    _local_weak = (
        len(ranked) < 3
        or local_top_score < FEDERATION_LOW_SCORE_THRESHOLD
    )

    if _local_weak and not _skip_federated_hard:
        fed_results = await _query_chroma(
            q_vec, k=3,
            scope_filter={"user_id": user_id, "scope": scope, "agent_id": agent_filter},
        )
        for fr in fed_results:
            if not any(r[1]["id"] == fr["id"] for r in ranked):
                if not explain:
                    # Still tag so audit tooling can identify federation hits
                    fr.setdefault("_explanation", {"source": fr.get("_explanation", {}).get("source", "federated_chroma_scoped")})
                ranked.append((fr["score"], fr))

    if ranked:
        # Fire-and-forget access stamps: buffered for ~250ms then flushed in a
        # single batched UPDATE off the read path. See `_access_stamp_flusher`.
        _enqueue_access_stamps(
            [item[1]["id"] for item in ranked if "bm25_score" in item[1]]
        )

    # Time-aware boost + neighbor-session expansion. Both are off unless the
    # caller opts in with smart_time_boost > 0 or smart_neighbor_sessions > 0.
    # Caller must include "metadata_json" in extra_columns so referenced_dates
    # / session_index metadata is available on rows.
    if ranked and (smart_time_boost > 0.0 or smart_neighbor_sessions > 0):
        from temporal_utils import extract_referenced_dates, has_temporal_cues

        def _meta_for(item: dict) -> dict:
            m = item.get("metadata")
            if isinstance(m, dict):
                return m
            raw = item.get("metadata_json") or "{}"
            try:
                m = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (json.JSONDecodeError, TypeError):
                m = {}
            item["metadata"] = m
            return m

        query_dates = extract_referenced_dates(query) if smart_time_boost > 0.0 else []
        query_has_temporal = has_temporal_cues(query)

        if smart_time_boost > 0.0 and query_dates:
            query_dt_set: list[datetime] = []
            for ds in query_dates:
                try:
                    query_dt_set.append(datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=timezone.utc))
                except ValueError:
                    pass
            if query_dt_set:
                boosted: list[tuple[float, dict]] = []
                for score, item in ranked:
                    new_score = score
                    vf = item.get("valid_from") or ""
                    if vf:
                        try:
                            h_dt = datetime.fromisoformat(vf)
                            for qdt in query_dt_set:
                                if abs((h_dt - qdt).days) <= 30:
                                    new_score += smart_time_boost
                                    break
                        except (ValueError, TypeError):
                            pass
                    meta = _meta_for(item)
                    for rd in meta.get("referenced_dates") or []:
                        try:
                            rd_dt = datetime.strptime(rd, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            for qdt in query_dt_set:
                                if abs((rd_dt - qdt).days) <= 14:
                                    new_score += smart_time_boost
                                    break
                            if new_score != score:
                                break
                        except (ValueError, TypeError):
                            continue
                    boosted.append((new_score, item))
                boosted.sort(key=lambda t: t[0], reverse=True)
                ranked = boosted

        if smart_neighbor_sessions > 0 and ranked:
            hit_session_indices: set[int] = set()
            hit_user_ids: set[str] = set()
            for _s, item in ranked:
                meta = _meta_for(item)
                si = meta.get("session_index")
                if si is not None:
                    try:
                        hit_session_indices.add(int(si))
                    except (TypeError, ValueError):
                        pass
                uid = item.get("user_id")
                if uid:
                    hit_user_ids.add(uid)
            multi_session_signal = len(hit_session_indices) >= 2
            if (query_has_temporal or multi_session_signal) and hit_session_indices and hit_user_ids:
                neighbor_indices: set[int] = set()
                for si in hit_session_indices:
                    for offset in range(-smart_neighbor_sessions, smart_neighbor_sessions + 1):
                        if offset != 0 and (si + offset) >= 0:
                            neighbor_indices.add(si + offset)
                neighbor_indices -= hit_session_indices
                if neighbor_indices:
                    already = {item["id"] for _s, item in ranked}
                    try:
                        with _db() as db:
                            for uid in hit_user_ids:
                                for si in neighbor_indices:
                                    rows = db.execute(
                                        "SELECT id, content, title, type, metadata_json, conversation_id "
                                        "FROM memory_items "
                                        "WHERE user_id = ? AND is_deleted = 0 AND type = 'message' "
                                        "  AND metadata_json LIKE ? ",
                                        (uid, f'%"session_index": {si}%'),
                                    ).fetchall()
                                    for r in rows:
                                        if r["id"] in already:
                                            continue
                                        already.add(r["id"])
                                        meta_raw = r["metadata_json"] or "{}"
                                        try:
                                            meta = json.loads(meta_raw)
                                        except (json.JSONDecodeError, TypeError):
                                            meta = {}
                                        neighbor_item = {
                                            "id": r["id"], "content": r["content"],
                                            "title": r["title"], "type": r["type"],
                                            "metadata_json": meta_raw, "metadata": meta,
                                            "conversation_id": r["conversation_id"],
                                            "_smart_neighbor": True,
                                        }
                                        ranked.append((0.0, neighbor_item))
                    except Exception as e:
                        logger.debug(f"smart_neighbor_sessions expansion failed: {e}")

    # Phase 11 supersedence-aware demotion moved to memory_search_scored_impl
    # (after _apply_rerank) so the MiniLM cross-encoder cannot undo it.

    # Phase D Mastra: post-rank preference for type='observation' rows.
    # When M3_PREFER_OBSERVATIONS=1, partition the ranked list into
    # obs_hits (type='observation') and raw_hits (everything else). If the
    # observations alone supply enough context (sum of token estimates above
    # M3_OBSERVATION_BUDGET_TOKENS, default 4000), return only obs_hits[:k].
    # Otherwise interleave: obs first, then raw to fill k slots. The point
    # is to favor synthesized atomic facts over raw turns when both are
    # retrieved for the same query.
    #
    # Off by default; bench harness opts in via --observer-variant flag
    # (Phase D Task 8) or callers set M3_PREFER_OBSERVATIONS=1 directly.
    # `_prefer_observations_gate` / `_two_stage_observations_gate` are bound
    # into module globals by the `_resolve_mc_callbacks()` call at the top of
    # this function — see the callback registry near the module header.
    if ranked and _prefer_observations_gate():
        try:
            obs_budget = int(os.environ.get("M3_OBSERVATION_BUDGET_TOKENS", "4000"))
        except ValueError:
            obs_budget = 4000
        obs_hits = [(s, it) for s, it in ranked
                    if isinstance(it, dict) and it.get("type") == "observation"]
        raw_hits = [(s, it) for s, it in ranked
                    if not (isinstance(it, dict) and it.get("type") == "observation")]
        if obs_hits:
            # Cheap token estimate: 1 token per 4 chars. The Mastra paper's
            # rationale is that an observation log displaces equivalent raw
            # turns when its summary is dense enough; we don't need precise
            # tokenization for the gate, just an order-of-magnitude check.
            obs_tokens = sum(len((it.get("content") or "")) // 4 for _, it in obs_hits)
            if obs_tokens >= obs_budget:
                # Observation-only return — observations supply enough.
                ranked = obs_hits[:k]
            else:
                # Interleave: observations first, then raw to fill remaining slots.
                slots = max(0, k - len(obs_hits))
                ranked = obs_hits + raw_hits[:slots]

    # Phase B3 (chatlog-recall plan, 2026-04-26): two-stage retrieval —
    # expand top-k observations to include their source turns. The
    # Observer's write_observation stores source_turn_ids in metadata_json;
    # when M3_TWO_STAGE_OBSERVATIONS=1 fires, we look up those rows and
    # append them to the ranked list at a small score discount so the
    # observation still ranks highest but the answerer sees the underlying
    # turns when it needs verbatim quotes.
    #
    # Off by default. The discount factor is M3_TWO_STAGE_TURN_PENALTY
    # (default 0.7 — turns rank just below their observation but ahead of
    # other raw hits).
    if ranked and _two_stage_observations_gate():
        try:
            turn_penalty = float(os.environ.get("M3_TWO_STAGE_TURN_PENALTY", "0.7"))
        except ValueError:
            turn_penalty = 0.7
        try:
            max_turns_per_obs = int(os.environ.get("M3_TWO_STAGE_MAX_TURNS_PER_OBS", "3"))
        except ValueError:
            max_turns_per_obs = 3
        # Collect source_turn_ids from observation hits (top-N only — no
        # point expanding tail-rank observations the user won't see).
        # Scope to top-k since obs_hits / raw_hits may have already been
        # collapsed back into `ranked` above.
        topk = ranked[: k]
        source_turn_ids: list[str] = []
        existing_ids = {it.get("id") for _, it in topk if isinstance(it, dict) and it.get("id")}
        for s, it in topk:
            if not isinstance(it, dict) or it.get("type") != "observation":
                continue
            # Inline meta lookup — _meta_for is scoped to a different block
            # earlier in this function. Same logic: prefer the parsed
            # metadata dict if attached, else parse metadata_json on demand.
            md = it.get("metadata") if isinstance(it.get("metadata"), dict) else None
            if md is None:
                raw = it.get("metadata_json") or "{}"
                try:
                    md = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
                except (json.JSONDecodeError, TypeError):
                    md = {}
            stids = md.get("source_turn_ids") or []
            if isinstance(stids, list):
                # Cap how many turns we pull per observation.
                for tid in stids[:max_turns_per_obs]:
                    if isinstance(tid, str) and tid not in existing_ids:
                        source_turn_ids.append(tid)
                        existing_ids.add(tid)
        if source_turn_ids:
            try:
                with _db() as db:
                    placeholders = ",".join("?" * len(source_turn_ids))
                    turn_rows = db.execute(
                        f"SELECT id, content, title, type, importance "
                        f"FROM memory_items "
                        f"WHERE id IN ({placeholders}) AND COALESCE(is_deleted,0)=0",
                        source_turn_ids,
                    ).fetchall()
                # Find the lowest score among existing top-k as the floor,
                # then place expanded turns at floor * turn_penalty so they
                # rank below existing hits but get included in formatted output.
                base_score = min((s for s, _ in topk), default=0.5)
                floor = max(0.01, base_score * turn_penalty)
                for r in turn_rows:
                    expanded_item = dict(r) if hasattr(r, "keys") else {
                        "id": r[0], "content": r[1], "title": r[2],
                        "type": r[3], "importance": r[4] or 0.0,
                    }
                    expanded_item["_two_stage_expanded"] = True
                    ranked.append((floor, expanded_item))
                # Re-sort once so the expanded turns settle in correctly.
                ranked.sort(key=lambda t: t[0], reverse=True)
            except Exception as e:
                logger.debug(f"two-stage observation expansion failed: {e}")

    return ranked


async def memory_search_routed_impl(
    query: str,
    mmr: bool = True,
    k: int = 10,
    fact_variant: str = "",
    temporal_k_bump: int = 5,
    graph_depth: int = 0,
    expand_sessions: bool = False,
    session_cap: int = 12,
    entity_graph: bool = False,
    entity_graph_depth: int = 1,
    entity_graph_max_neighbors: int = 20,
    entity_graph_valid_types: list = None,          # None = use VALID_ENTITY_TYPES; [] from MCP treated as None
    entity_graph_valid_predicates: list = None,     # None = use VALID_ENTITY_PREDICATES; [] from MCP treated as None
    entity_stoplist: list = None,                   # None = use M3_ENTITY_SEED_STOPLIST env; [] disables filtering
    # Cross-encoder rerank — default off; production behavior unchanged when False.
    # When True: rescores top (rerank_pool_k or 3*k) hits with sentence-transformers
    # CrossEncoder, blends with hybrid score, re-sorts. See _apply_rerank() docstring
    # and decision memory for the resolution chain.
    rerank: bool = False,
    rerank_model: str = "",                         # empty = DEFAULT_RERANK_MODEL
    rerank_pool_k: int = 0,                         # 0 = 3*k (sensible default; never below k)
    rerank_blend: float = 1.0,                      # 1.0 = pure CE replacement, 0.5 = avg, 0.0 = no-op
    user_id: str = "",
    scope: str = "",
    type_filter: str = "",
    agent_filter: str = "",
    search_mode: str = "hybrid",
    variant: str = "",
    as_of: str = "",
    conversation_id: str = "",
    explain: bool = False,
    extra_columns=None,
    recency_bias: float = 0.0,
    vector_weight: float = 0.7,
    adaptive_k: bool = False,
    elbow_sensitivity: float = 1.5,
    adaptive_k_min: int = 0,
    adaptive_k_max: int = 0,
    smart_time_boost: float = 0.0,
    smart_neighbor_sessions: int = 0,
    intent_hint: str = "",
    vector_kind_strategy: str = "default",
    # --- AUTO routing layer (opt-in, default off) ---
    # Invariant: auto_route=False produces byte-identical output to pre-refactor.
    auto_route: bool = False,
    # Signal-detection thresholds (overridable)
    auto_top1_sharp_min: float = 0.89,                     # top-1 score above which query is "sharp"
    auto_slope_at_3_sharp_min: float = 0.08,               # slope-at-3 above which query is "sharp"
    auto_conv_id_diversity_threshold: int = 5,             # conv_id diversity above which → multi_session
    auto_top1_low_threshold: float = 0.50,                 # OOD guard — below this, not sharp
    # Branch values: temporal
    auto_temporal_k: int = 15,                             # k for temporal branch
    auto_temporal_recency_bias: float = 0.05,              # recency_bias for temporal branch
    auto_temporal_expand_sessions: bool = True,            # expand_sessions for temporal branch
    auto_temporal_graph_depth: int = 1,                    # graph_depth for temporal branch (AUTO_v2 fix)
    # Branch values: multi_session
    auto_multi_k: int = 20,                                # k for multi_session branch
    auto_multi_expand_sessions: bool = True,               # expand_sessions for multi_session branch
    # Branch values: sharp (post-process trim)
    auto_sharp_threshold_ratio: float = 0.85,              # trim hits below top_score * ratio
    auto_sharp_k_min: int = 3,                             # floor after threshold trim
    auto_sharp_k_max: int = 10,                            # ceiling after threshold trim
    # Branch values: entity_anchored (AUTO entity-graph expansion)
    auto_entity_graph_enabled: bool = True,                # AUTO fires entity branch when query has named entities
    auto_entity_graph_depth: int = 1,                      # entity_graph_depth for entity_anchored branch
    auto_entity_graph_max_neighbors: int = 20,             # entity_graph_max_neighbors for entity_anchored branch
    auto_entity_graph_named_entity_threshold: int = 1,     # min named entities to fire entity_anchored branch
    # Capture mechanism (option b): caller passes a dict, function populates it
    _capture_dict: dict = None,
) -> list:
    """Temporal-aware routed retrieval, with optional graph + session expansion.

    Rule:
      - if is_temporal_query(query): retrieve verbatim only at (k + temporal_k_bump)
        with vector_kind_strategy='default'
      - else: retrieve at k. If fact_variant is non-empty, fuse base-variant hits
        with fact-variant hits client-side (max-fusion by score per memory_id).
        If fact_variant is empty, this collapses to a standard memory_search_scored_impl
        call at vector_kind_strategy='max' (so any pre-existing dual-embed rows
        on the base variant get used).

    Optional post-retrieval expansions (both opt-in, default off):
      - graph_depth > 0: traverse memory_relationships from each top-k hit up
        to N hops (clamped to 3), score the new rows against the query, and
        max-fuse them into the result before re-trimming to k.
      - expand_sessions=True: pull all turns sharing each top-k hit's
        conversation_id (capped at session_cap per conversation), score them
        against the query, and max-fuse. Useful for supersession / context-
        recovery questions.

    AUTO routing layer (opt-in via auto_route=True):
      When auto_route=True, a two-pass strategy is used. First an overshoot
      retrieval at k=20 is run to obtain post-retrieval signals (score curve,
      conv_id diversity). The branch decision then sets unset retrieval
      parameters before the main retrieval proceeds. Caller-explicit values
      always win over AUTO branch values. When auto_route=False (the default),
      none of this runs and behaviour is byte-identical to pre-refactor.

      If _capture_dict is passed (a mutable dict), it is populated with:
        auto_branch, auto_branch_values, caller_overrides, auto_signals.

    Retrieval-pool telemetry (always populated when _capture_dict is passed,
    regardless of auto_route — written by the primary memory_search_scored_impl
    call; the overshoot and fact-fuse calls do not write):
        pre_seen_content_filter_rows  -- pool size after row-cap, before content-dedup
        pre_seen_content_dedup_rows   -- pool size entering dedup loop (post-rank)
        post_seen_content_dedup_rows  -- pool size after content-dedup, before MMR/rerank
      Adaptive-K elbow telemetry (only present when adaptive_k=True):
        pre_adaptive_k_rows           -- pool size before _trim_by_elbow
        post_elbow_trim_rows          -- pool size immediately after the elbow trim
        post_adaptive_k_rows          -- pool size after min/max floors applied

    Returns the same shape as memory_search_scored_impl: list[tuple[score, dict]].
    """
    _resolve_mc_callbacks()  # bind memory_core callbacks on first call
    # AUTO routing layer (opt-in, default off — invariant: off = byte-identical to today)
    auto_metadata = None
    resolved = None
    overshoot_candidates: list = []  # captured for possible reuse as base_hits
    _overshoot_k = 20                 # the fixed overshoot pool size
    # The overshoot's job is signal-extraction (top_1, slope_at_3, conv_id
    # diversity). We deliberately align its `vector_kind_strategy` with the
    # branch the primary call would use — temporal -> "default", non-temporal
    # -> "max" — so the overshoot pool can also serve as the primary candidate
    # pool when eligibility (see _try_reuse_overshoot below) is met. Branch
    # decision is unaffected; the signals dominate over the vector_kind choice.
    _overshoot_strategy = "default" if is_temporal_query(query) else "max"
    if auto_route:
        # `_capture_dict` is forwarded so the overshoot's retrieval-pool
        # telemetry (pre_seen_content_filter_rows, etc.) is written even when
        # the overshoot doubles as the primary pool. When reuse doesn't fire,
        # the primary call below overwrites these keys — both writes describe
        # the same family of values (pool sizes for the candidate retrieval).
        overshoot_candidates = await _resolve_search_callable("memory_search_scored_impl")(query, mmr=mmr, k=_overshoot_k, user_id=user_id, scope=scope,
            type_filter=type_filter, agent_filter=agent_filter,
            search_mode=search_mode, variant=variant, as_of=as_of,
            conversation_id=conversation_id, extra_columns=extra_columns,
            vector_kind_strategy=_overshoot_strategy,
            _capture_dict=_capture_dict,
        )

        # Signature defaults for all overridable retrieval knobs.
        # These must match the function signature defaults above exactly.
        _sig_defaults = {
            "k": 10,
            "temporal_k_bump": 5,
            "graph_depth": 0,
            "expand_sessions": False,
            "session_cap": 12,
            "recency_bias": 0.0,
            "vector_weight": 0.7,
            # Entity-graph knobs (for override detection)
            "entity_graph": False,
            "entity_graph_depth": 1,
            "entity_graph_max_neighbors": 20,
            # AUTO threshold defaults (for override detection only)
            "auto_top1_sharp_min": 0.89,
            "auto_slope_at_3_sharp_min": 0.08,
            "auto_conv_id_diversity_threshold": 5,
            "auto_top1_low_threshold": 0.50,
            # AUTO branch value defaults
            "auto_temporal_k": 15,
            "auto_temporal_recency_bias": 0.05,
            "auto_temporal_expand_sessions": True,
            "auto_temporal_graph_depth": 1,
            "auto_multi_k": 20,
            "auto_multi_expand_sessions": True,
            "auto_sharp_threshold_ratio": 0.85,
            "auto_sharp_k_min": 3,
            "auto_sharp_k_max": 10,
            # AUTO entity_anchored branch defaults
            "auto_entity_graph_enabled": True,
            "auto_entity_graph_depth": 1,
            "auto_entity_graph_max_neighbors": 20,
            "auto_entity_graph_named_entity_threshold": 1,
        }

        # Current param values (what the caller actually passed or defaulted to).
        _current_params = {
            "k": k,
            "temporal_k_bump": temporal_k_bump,
            "graph_depth": graph_depth,
            "expand_sessions": expand_sessions,
            "session_cap": session_cap,
            "recency_bias": recency_bias,
            "vector_weight": vector_weight,
            # Entity-graph knobs (pass-through so AUTO layer can detect caller overrides)
            "entity_graph": entity_graph,
            "entity_graph_depth": entity_graph_depth,
            "entity_graph_max_neighbors": entity_graph_max_neighbors,
            # Threshold overrides (pass-through so decide_branch can read them)
            "auto_top1_sharp_min": auto_top1_sharp_min,
            "auto_slope_at_3_sharp_min": auto_slope_at_3_sharp_min,
            "auto_conv_id_diversity_threshold": auto_conv_id_diversity_threshold,
            "auto_top1_low_threshold": auto_top1_low_threshold,
            # Branch value overrides (pass-through so branch_values can read them)
            "auto_temporal_k": auto_temporal_k,
            "auto_temporal_recency_bias": auto_temporal_recency_bias,
            "auto_temporal_expand_sessions": auto_temporal_expand_sessions,
            "auto_temporal_graph_depth": auto_temporal_graph_depth,
            "auto_multi_k": auto_multi_k,
            "auto_multi_expand_sessions": auto_multi_expand_sessions,
            "auto_sharp_threshold_ratio": auto_sharp_threshold_ratio,
            "auto_sharp_k_min": auto_sharp_k_min,
            "auto_sharp_k_max": auto_sharp_k_max,
            # AUTO entity_anchored branch values (pass-through for decide_branch)
            "auto_entity_graph_enabled": auto_entity_graph_enabled,
            "auto_entity_graph_depth": auto_entity_graph_depth,
            "auto_entity_graph_max_neighbors": auto_entity_graph_max_neighbors,
            "auto_entity_graph_named_entity_threshold": auto_entity_graph_named_entity_threshold,
        }

        resolved, auto_metadata = _apply_auto_layer(
            query, overshoot_candidates, _current_params, _sig_defaults
        )

        # Apply resolved values back to local variables so the rest of the
        # function (which is unchanged) uses the AUTO-adjusted parameters.
        k = resolved["k"]
        temporal_k_bump = resolved["temporal_k_bump"]
        graph_depth = resolved["graph_depth"]
        expand_sessions = resolved["expand_sessions"]
        session_cap = resolved["session_cap"]
        recency_bias = resolved["recency_bias"]
        vector_weight = resolved["vector_weight"]
        # Apply AUTO entity-graph values.
        # Precedence rule: if auto_entity_graph_enabled=False, AUTO must NOT enable entity_graph.
        # Also: if caller explicitly passed entity_graph=False (recorded in caller_overrides),
        # that beats the entity_anchored branch value.
        _eg_caller_overrides = auto_metadata.get("caller_overrides", {})
        _eg_auto_blocked = (
            not auto_entity_graph_enabled
            or ("entity_graph" in _eg_caller_overrides and not _eg_caller_overrides["entity_graph"])
        )
        if _eg_auto_blocked and auto_metadata.get("auto_branch") == "entity_anchored":
            # Suppress AUTO's entity_graph=True — use the original entity_graph value
            resolved["entity_graph"] = entity_graph
        entity_graph = resolved.get("entity_graph", entity_graph)
        entity_graph_depth = resolved.get("entity_graph_depth", entity_graph_depth)
        entity_graph_max_neighbors = resolved.get("entity_graph_max_neighbors", entity_graph_max_neighbors)

        # Populate caller-supplied capture dict if present.
        if _capture_dict is not None:
            _capture_dict.update(auto_metadata)

    # Normalize MCP empty-list sentinel → None for entity vocab overrides.
    # This happens unconditionally (covers both auto_route=True and False paths).
    _egt = entity_graph_valid_types if entity_graph_valid_types else None
    _egp = entity_graph_valid_predicates if entity_graph_valid_predicates else None

    # Read env-var override for the bump
    bump = int(os.environ.get("M3_ROUTER_TEMPORAL_K_BUMP", str(temporal_k_bump)))

    # ── AUTO overshoot reuse ─────────────────────────────────────────────────
    # When AUTO already ran the overshoot retrieval, the overshoot result can
    # double as the primary candidate pool — skipping a second full retrieval —
    # IFF every divergence axis between the overshoot and primary calls is
    # neutralized. The overshoot uses retrieval-time defaults; eligibility
    # therefore requires the primary call to also be on those defaults.
    #
    # Divergence axes (must all be aligned):
    #  - `vector_kind_strategy`: overshoot ran with the same strategy the
    #    primary would (temporal -> "default", non-temporal -> "max"); set
    #    above just before the overshoot call.
    #  - `k`: overshoot pool is 20 rows; the effective primary `k` (k+bump for
    #    temporal, k*2 for fact_variant, else k) must fit.
    #  - `explain`: overshoot doesn't compute _explanation.
    #  - `recency_bias`: overshoot uses 0; if caller / AUTO branch set non-zero,
    #    the primary scores differ.
    #  - `vector_weight`: overshoot uses 0.7; AUTO temporal/multi don't touch
    #    it, so anything other than 0.7 must be a caller override.
    #  - `adaptive_k` / `smart_time_boost` / `smart_neighbor_sessions`: all
    #    off in the overshoot.
    #  - `intent_hint`: overshoot passes "" — caller must too.
    #  - `_capture_dict`: the primary call writes retrieval-pool telemetry; if
    #    the caller is reading it, we can't shortcut.
    #  - `fact_variant`: handled by computing effective_primary_k including
    #    the *2 factor.
    def _can_reuse_overshoot() -> bool:
        if not auto_route or not overshoot_candidates:
            return False
        if explain or intent_hint:
            return False
        if recency_bias or adaptive_k or adaptive_k_min or adaptive_k_max:
            return False
        if smart_time_boost or smart_neighbor_sessions:
            return False
        if abs(float(vector_weight) - 0.7) > 1e-9:
            return False
        effective_primary_k = (
            (k + bump) if is_temporal_query(query)
            else (k * 2 if fact_variant else k)
        )
        if effective_primary_k > _overshoot_k:
            return False
        return True

    _reuse_overshoot = _can_reuse_overshoot()
    if _reuse_overshoot:
        if auto_metadata is not None:
            auto_metadata["overshoot_reused"] = True
        if _capture_dict is not None:
            _capture_dict["overshoot_reused"] = True

    if is_temporal_query(query):
        if _reuse_overshoot:
            primary = overshoot_candidates[: (k + bump)]
        else:
            primary = await _resolve_search_callable("memory_search_scored_impl")(query, mmr=mmr, k=k + bump, user_id=user_id, scope=scope,
            type_filter=type_filter, agent_filter=agent_filter,
            search_mode=search_mode, variant=variant, as_of=as_of,
            conversation_id=conversation_id, explain=explain,
            extra_columns=extra_columns, recency_bias=recency_bias,
            vector_weight=vector_weight, adaptive_k=adaptive_k,
            elbow_sensitivity=elbow_sensitivity, adaptive_k_min=adaptive_k_min,
            adaptive_k_max=adaptive_k_max, smart_time_boost=smart_time_boost,
            smart_neighbor_sessions=smart_neighbor_sessions,
            intent_hint=intent_hint, vector_kind_strategy="default",
            _capture_dict=_capture_dict,
        )
        final_hits = await _resolve_search_callable("_maybe_expand_routed")(
            query, primary, k=k + bump,
            graph_depth=graph_depth,
            expand_sessions=expand_sessions, session_cap=session_cap,
            entity_graph=entity_graph,
            entity_graph_depth=entity_graph_depth,
            entity_graph_max_neighbors=entity_graph_max_neighbors,
            entity_graph_valid_types=_egt,
            entity_graph_valid_predicates=_egp,
            entity_stoplist=entity_stoplist,
            _capture_dict=_capture_dict,
        )
    else:
        # Non-temporal path
        if _reuse_overshoot:
            base_hits = overshoot_candidates[: (k * 2 if fact_variant else k)]
        else:
            base_hits = await _resolve_search_callable("memory_search_scored_impl")(query, mmr=mmr, k=k * 2 if fact_variant else k,
            user_id=user_id, scope=scope, type_filter=type_filter,
            agent_filter=agent_filter, search_mode=search_mode,
            variant=variant, as_of=as_of, conversation_id=conversation_id,
            explain=explain, extra_columns=extra_columns, recency_bias=recency_bias,
            vector_weight=vector_weight, adaptive_k=adaptive_k,
            elbow_sensitivity=elbow_sensitivity, adaptive_k_min=adaptive_k_min,
            adaptive_k_max=adaptive_k_max, smart_time_boost=smart_time_boost,
            smart_neighbor_sessions=smart_neighbor_sessions,
            intent_hint=intent_hint, vector_kind_strategy="max",
            _capture_dict=_capture_dict,
        )

        if not fact_variant:
            final_hits = await _resolve_search_callable("_maybe_expand_routed")(
                query, base_hits[:k], k=k,
                graph_depth=graph_depth,
                expand_sessions=expand_sessions, session_cap=session_cap,
                entity_graph=entity_graph,
                entity_graph_depth=entity_graph_depth,
                entity_graph_max_neighbors=entity_graph_max_neighbors,
                entity_graph_valid_types=_egt,
                entity_graph_valid_predicates=_egp,
                entity_stoplist=entity_stoplist,
                _capture_dict=_capture_dict,
            )
        else:
            # Fuse with fact_variant hits (client-side max-fusion by memory_id, top-k)
            fact_hits = await _resolve_search_callable("memory_search_scored_impl")(query, mmr=mmr, k=k * 2, user_id=user_id, scope=scope,
                type_filter=type_filter, agent_filter=agent_filter,
                search_mode=search_mode, variant=fact_variant, as_of=as_of,
                conversation_id=conversation_id, explain=explain,
                extra_columns=extra_columns, recency_bias=recency_bias,
                vector_weight=vector_weight, adaptive_k=adaptive_k,
                elbow_sensitivity=elbow_sensitivity, adaptive_k_min=adaptive_k_min,
                adaptive_k_max=adaptive_k_max, smart_time_boost=smart_time_boost,
                smart_neighbor_sessions=smart_neighbor_sessions,
                intent_hint=intent_hint, vector_kind_strategy="default",
            )

            # Both return list[tuple[score, dict]]. Dedupe by item id, keep highest score.
            best: dict = {}  # memory_id -> (score, item)
            for s, item in base_hits + fact_hits:
                mid = item.get("id") if isinstance(item, dict) else None
                if mid is None:
                    continue
                if mid not in best or s > best[mid][0]:
                    best[mid] = (s, item)
            fused = sorted(best.values(), key=lambda x: x[0], reverse=True)[:k]
            final_hits = await _resolve_search_callable("_maybe_expand_routed")(
                query, fused, k=k,
                graph_depth=graph_depth,
                expand_sessions=expand_sessions, session_cap=session_cap,
                entity_graph=entity_graph,
                entity_graph_depth=entity_graph_depth,
                entity_graph_max_neighbors=entity_graph_max_neighbors,
                entity_graph_valid_types=_egt,
                entity_graph_valid_predicates=_egp,
                entity_stoplist=entity_stoplist,
                _capture_dict=_capture_dict,
            )

    # Sharp-branch post-process trim (only when AUTO routing is active and sharp branch fired)
    if auto_route and auto_metadata and auto_metadata.get("auto_branch") == "sharp":
        final_hits = _apply_sharp_trim(
            final_hits,
            threshold_ratio=resolved["auto_sharp_threshold_ratio"],
            k_min=resolved["auto_sharp_k_min"],
            k_max=resolved["auto_sharp_k_max"],
        )
        if _capture_dict is not None:
            _capture_dict["sharp_post_trim_count"] = len(final_hits)

    # Entity-anchored capture: count entity-graph neighbors added to final hits.
    if auto_route and auto_metadata and auto_metadata.get("auto_branch") == "entity_anchored":
        if _capture_dict is not None:
            eg_count = sum(
                1 for _, item in final_hits
                if isinstance(item, dict) and item.get("_expanded_via") == "entity_graph"
            )
            _capture_dict["entity_graph_neighbors_added"] = eg_count

    # Cross-encoder rerank pass (default off). Runs LAST so it sees the fully
    # expanded + sharp-trimmed result set, including entity-graph neighbors.
    # CONTRACT: rerank=False → byte-identical to pre-feature behavior.
    if rerank:
        _model = rerank_model or DEFAULT_RERANK_MODEL
        _pool = rerank_pool_k if rerank_pool_k > 0 else (3 * k)
        _final_n = len(final_hits)
        final_hits = _apply_rerank(
            final_hits,
            query,
            pool_k=_pool,
            final_k=k,
            model_name=_model,
            blend=rerank_blend,
        )
        if _capture_dict is not None:
            _capture_dict["rerank_applied"] = True
            _capture_dict["rerank_model"] = _model
            _capture_dict["rerank_pool_k"] = _pool
            _capture_dict["rerank_blend"] = rerank_blend
            _capture_dict["rerank_pre_count"] = _final_n
            _capture_dict["rerank_post_count"] = len(final_hits)

    # Phase 11: supersedence-aware demotion — runs AFTER reranker so the
    # cross-encoder cannot undo it. Items that are the to_id of a 'supersedes'
    # edge (i.e. an older version exists) get score * SUPERSEDES_PENALTY.
    # Default 0.5x: demote but keep retrievable for "what did I previously
    # say?" queries. Set SUPERSEDES_PENALTY=0 to exclude entirely.
    if final_hits and SUPERSEDES_PENALTY < 1.0:
        hit_ids = [item.get("id") for _, item in final_hits if isinstance(item, dict) and item.get("id")]
        if hit_ids:
            try:
                with _db() as db:
                    placeholders = ",".join("?" * len(hit_ids))
                    sup_rows = db.execute(
                        f"SELECT to_id FROM memory_relationships "
                        f"WHERE relationship_type = 'supersedes' "
                        f"AND to_id IN ({placeholders})",
                        hit_ids,
                    ).fetchall()
                    superseded_ids: set = {r["to_id"] for r in sup_rows}
                if superseded_ids:
                    final_hits = [
                        (
                            (s * SUPERSEDES_PENALTY) if isinstance(item, dict) and item.get("id") in superseded_ids else s,
                            item,
                        )
                        for s, item in final_hits
                    ]
                    final_hits.sort(key=lambda t: t[0], reverse=True)
                    if _capture_dict is not None:
                        _capture_dict["superseded_demoted"] = len(superseded_ids)
            except Exception as e:
                logger.debug(f"supersedence-aware demotion failed: {e}")

    return final_hits


async def _maybe_expand_routed(
    query: str, primary: list, k: int,
    graph_depth: int = 0,
    expand_sessions: bool = False,
    session_cap: int = 12,
    entity_graph: bool = False,
    entity_graph_depth: int = 1,
    entity_graph_max_neighbors: int = 20,
    entity_graph_valid_types: list = None,
    entity_graph_valid_predicates: list = None,
    entity_stoplist: list = None,
    _capture_dict: dict = None,
) -> list:
    """Apply optional graph, session, and entity-graph expansion to a routed retrieval result.

    All three expansions take the primary top-k hits' ids (or the query, for entity_graph)
    as seeds, fetch new rows, score them against the query, and max-fuse with the primary
    list. If all are off (the default), returns primary unchanged.
    """
    _resolve_mc_callbacks()  # bind memory_core callbacks on first call

    # Test-shim resolution: tests patch `memory_core._db` to feed a fake
    # connection into this expansion path. Resolve at call entry so the
    # patches take effect; production reads memory.db._db.
    try:
        import memory_core as _mc  # type: ignore
        _db = getattr(_mc, "_db", globals()["_db"])
    except ImportError:
        _db = globals()["_db"]
    if graph_depth <= 0 and not expand_sessions and not entity_graph:
        return primary
    seed_ids = [item.get("id") for _, item in primary if isinstance(item, dict) and item.get("id")]
    if not seed_ids and not entity_graph:
        return primary

    # Build dict of new rows (memory_id -> row dict), avoiding duplicates of primary seeds.
    primary_ids: set = {item.get("id") for _, item in primary if isinstance(item, dict) and item.get("id")}
    extra_rows: dict = {}
    # Track which expansion source each extra row came from for _expanded_via tagging.
    extra_row_source: dict = {}  # memory_id -> "graph" | "session" | "entity_graph"

    if graph_depth > 0 and seed_ids:
        neighbor_ids = _resolve_graph_helper("_graph_neighbor_ids")(seed_ids, depth=int(graph_depth))
        if neighbor_ids:
            with _db() as db:
                placeholders = ",".join(["?"] * len(neighbor_ids))
                rows = db.execute(
                    f"SELECT id, type, title, content, metadata_json, conversation_id, "
                    f"valid_from, user_id FROM memory_items "
                    f"WHERE id IN ({placeholders}) AND COALESCE(is_deleted, 0) = 0",
                    list(neighbor_ids),
                ).fetchall()
                for r in rows:
                    extra_rows[r["id"]] = dict(r)
                    extra_row_source[r["id"]] = "graph"

    if expand_sessions and seed_ids:
        session_rows = _resolve_graph_helper("_session_neighbor_ids")(seed_ids, session_cap=int(session_cap))
        for rid, item in session_rows.items():
            if rid not in extra_rows:
                extra_rows[rid] = item
                extra_row_source[rid] = "session"

    if entity_graph:
        try:
            with _db() as db:
                eg_memory_ids = await _resolve_graph_helper("_entity_graph_neighbor_ids")(
                    query,
                    depth=int(entity_graph_depth),
                    max_neighbors=int(entity_graph_max_neighbors),
                    db=db,
                    valid_types=entity_graph_valid_types,
                    valid_predicates=entity_graph_valid_predicates,
                    entity_stoplist=entity_stoplist,
                    _capture_dict=_capture_dict,
                )
            new_ids = eg_memory_ids - primary_ids - set(extra_rows.keys())
            if new_ids:
                with _db() as db:
                    placeholders = ",".join(["?"] * len(new_ids))
                    eg_rows = db.execute(
                        f"SELECT id, type, title, content, metadata_json, conversation_id, "
                        f"valid_from, user_id FROM memory_items "
                        f"WHERE id IN ({placeholders}) AND COALESCE(is_deleted, 0) = 0",
                        list(new_ids),
                    ).fetchall()
                    for r in eg_rows:
                        if r["id"] not in extra_rows:
                            extra_rows[r["id"]] = dict(r)
                            extra_row_source[r["id"]] = "entity_graph"
        except Exception:  # noqa: BLE001
            pass  # entity_graph expansion is best-effort; never crash the primary path

    if not extra_rows:
        # Tag primary hits as "primary" and return unchanged.
        for _, item in primary:
            if isinstance(item, dict) and "_expanded_via" not in item:
                item["_expanded_via"] = "primary"
        return primary

    # Tag each extra row with its expansion source before scoring.
    for mid, item in extra_rows.items():
        item["_expanded_via"] = extra_row_source.get(mid, "graph")

    # Tag primary items as "primary" before fusion.
    for _, item in primary:
        if isinstance(item, dict) and "_expanded_via" not in item:
            item["_expanded_via"] = "primary"

    scored_extras = await _resolve_graph_helper("_score_extra_rows")(query, extra_rows, base_score=0.0)

    best: dict = {}
    for s, item in primary + scored_extras:
        mid = item.get("id") if isinstance(item, dict) else None
        if mid is None:
            continue
        if mid not in best or s > best[mid][0]:
            best[mid] = (s, item)
        elif s == best[mid][0] and item.get("_expanded_via", "primary") != "primary":
            # On exact score tie, prefer the non-primary tag to preserve cross-peer evidence.
            best[mid] = (s, item)
    fused = sorted(best.values(), key=lambda x: x[0], reverse=True)
    # Enforce expansion-displacement guard at top ranks before truncation to k.
    # The fusion sort treats expansion and primary rows at parity on score, but
    # the two score scales are not calibrated against each other at small k.
    # See EXPANSION_DISPLACEMENT_MARGIN docstring for the rule and env-var
    # overrides.
    fused = _enforce_expansion_displacement_guard(fused)
    fused = fused[:k]
    return fused


async def memory_search_multi_db_impl(
    query: str,
    databases: "list[str] | str",
    k: int = 8,
    mmr: bool = True,
    type_filter: str = "",
    agent_filter: str = "",
    search_mode: str = "hybrid",
    user_id: str = "",
    scope: str = "",
    as_of: str = "",
    conversation_id: str = "",
    extra_columns: "list[str] | None" = None,
    recency_bias: float = 0.0,
    adaptive_k: bool = False,
    variant: "str | list" = "",
    fan_out_limit: "int | None" = None,
):
    """Fan out a search across multiple SQLite databases and merge by score.

    `databases` accepts either a list of paths or a comma-separated string
    (MCP-friendly). Each path is searched independently via the existing
    `memory_search_scored_impl` under its own `active_database` context, so
    pool-cache keys stay correct and no global env mutation occurs.

    Score-comparability assumption: all DBs use the same `embed_model`. FTS5
    BM25 scores depend on per-DB corpus stats and may not be perfectly
    comparable across DBs; for typical small-N fan-out (chatlog + main) the
    rank-merge is good enough. Document this limitation in the MCP tool
    description so callers don't expect cross-DB statistical normalization.

    Each returned item is tagged with `_database` (the source path) so callers
    can preserve provenance after the merge. Returns a list of (score, item)
    sorted descending and truncated to `k`.
    """
    _resolve_mc_callbacks()  # bind memory_core callbacks on first call
    from m3_sdk import active_database, resolve_db_path

    if isinstance(databases, str):
        paths = [p.strip() for p in databases.split(",") if p.strip()]
    else:
        paths = [p for p in (databases or []) if p]
    if not paths:
        return []

    resolved = [resolve_db_path(p) for p in paths]

    # CSV-on-the-wire convenience for MCP callers: a comma-separated `variant`
    # string upgrades to a list so memory_search_scored_impl produces an
    # IN (...) clause. Single names + `__none__` keep their string fast path.
    if isinstance(variant, str) and "," in variant:
        variant = [s.strip() for s in variant.split(",") if s.strip()]

    sem = asyncio.Semaphore(fan_out_limit) if fan_out_limit and fan_out_limit > 0 else None

    async def _one(path: str):
        async def _run():
            with active_database(path):
                return await memory_search_scored_impl(query, mmr=mmr, k=k, type_filter=type_filter,
                    agent_filter=agent_filter, search_mode=search_mode,
                    user_id=user_id, scope=scope, as_of=as_of,
                    conversation_id=conversation_id,
                    extra_columns=extra_columns,
                    recency_bias=recency_bias, adaptive_k=adaptive_k,
                    variant=variant,
                )
        if sem is None:
            ranked = await _run()
        else:
            async with sem:
                ranked = await _run()
        for _score, item in ranked:
            item["_database"] = path
        return ranked

    per_db = await asyncio.gather(*[_one(p) for p in resolved], return_exceptions=True)

    merged: list[tuple[float, dict]] = []
    for path, result in zip(resolved, per_db):
        if isinstance(result, BaseException):
            logger.warning(
                f"memory_search_multi_db_impl: search failed for {path}: "
                f"{type(result).__name__}: {result}"
            )
            continue
        merged.extend(result)

    merged.sort(key=lambda sx: sx[0], reverse=True)
    return merged[:k]


async def memory_search_impl(
    query,
    k=8,
    type_filter="",
    agent_filter="",
    search_mode="hybrid",
    include_scratchpad=False,
    user_id="",
    scope="",
    as_of="",
    explain=False,
    conversation_id="",
    recency_bias=0.0,
    adaptive_k=False,
    variant="",
    intent_hint="",
    mmr=True,
    _depth=0,
):
    _resolve_mc_callbacks()  # bind memory_core callbacks on first call
    ranked = await memory_search_scored_impl(
        query,
        mmr=mmr,
        k=k,
        type_filter=type_filter,
        agent_filter=agent_filter,
        search_mode=search_mode,
        user_id=user_id,
        scope=scope,
        as_of=as_of,
        conversation_id=conversation_id,
        explain=explain,
        recency_bias=float(recency_bias) if recency_bias else 0.0,
        adaptive_k=bool(adaptive_k),
        variant=variant,
        intent_hint=intent_hint,
        extra_columns=["metadata_json", "conversation_id"] if intent_hint else None,
    )
    if ranked is None:
        return "Search failed: FTS and semantic both unavailable."

    if not ranked:
        return "No results found."
    lines = [f"Top {len(ranked)} results:"]
    for rank, (score, item) in enumerate(ranked, 1):
        content = item.get("content") or ""
        lines.append("-" * 40)
        lines.append(f"{rank}. [{item['id']}] score={score:.4f}  type: {item.get('type', 'unknown')}  title: {item.get('title','')}")

        if explain and "_explanation" in item:
            exp = item["_explanation"]
            if "raw_hybrid" in exp:
                vw = exp.get("vector_weight", 0.7)
                lines.append(f"   Breakdown: vector={exp['vector']:.4f} (weight {vw:.2f}) + bm25={exp['bm25']:.4f} (weight {1.0-vw:.2f}) -> raw={exp['raw_hybrid']:.4f}")
                if "mmr_penalty" in exp:
                    lines.append(f"   MMR penalty: -{exp['mmr_penalty']:.4f} (max_sim_to_selected={exp['max_sim_to_selected']:.4f})")
                lines.append(f"   Importance: {exp['importance']:.4f}")
            else:
                lines.append(f"   Source: {exp.get('source', 'unknown')}")

        lines.append(f"Content:\n{content}\n")
    lines.append("-" * 40)
    return "\n".join(lines)
