"""Pure routing helpers extracted from `memory.search` (Phase 4.B follow-up).

Holds the query-routing / result-shaping helpers that do not touch any
memory_core callback, the reranker singleton, or a `_resolve_*` shim:
temporal query classification, AUTO-branch param resolution, sharp-trim,
the expansion-displacement guard, sqlite-vec feature detection, and the
predecessor-turn pull (which talks to the DB directly via `.db._db`, not
through the floor-bound/test-shim pattern the big impls use).

CONTRACT: this module must NOT import `memory.search` (that would create a
cycle, since search.py re-imports these names at its top to keep the
memory_core lazy-registry shim resolving to the same function objects).
"""
from __future__ import annotations

import json
import logging
import sqlite3

from . import config
from .db import _db
from .fts import _EVENT_PROPER_NOUN, _TEMPORAL_QUERY_RE, _TEMPORAL_ROUTER_RE

logger = logging.getLogger("memory.search_routing")


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


def _detect_sqlite_vec(db) -> bool:
    """Return True if the sqlite-vec extension functions are available on the connection."""
    if not isinstance(db, sqlite3.Connection):
        return False
    try:
        db.execute("SELECT vec_version()")
        return True
    except Exception:
        return False
