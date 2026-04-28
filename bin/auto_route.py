"""auto_route — multi-signal retrieval branch decider.

Pre-retrieval signals (from query text):
- has_temporal_cues(query): regex on temporal keywords + date patterns
- has_comparison_cues(query): regex on count/aggregation keywords
- count_named_entities(query): count of capitalized multi-word proper-noun phrases

Post-retrieval signals (from candidate list):
- top_1_score(candidates)
- slope_at_3(candidates)
- conv_id_diversity(candidates)

Branch decision (first match wins):
1. temporal  — if has_temporal_cues(query)
2. multi_session — if has_comparison_cues(query) OR conv_id_diversity > threshold
3. sharp — if top_1 > sharp_min AND slope_at_3 > sharp_slope_min
4. entity_anchored — if count_named_entities(query) >= threshold AND auto_entity_graph_enabled
5. default — fallback (no values set; pure pass-through to caller defaults)

API:
- decide_branch(query, candidates, params) -> str (branch name)
- branch_values(branch, params) -> dict[str, Any]  (parameter values for this branch)
- signals_summary(query, candidates) -> dict  (all signals as a dict for capture)
- count_named_entities(query) -> int  (count of proper-noun phrases in query)
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Default thresholds — all overridable via passed params
# ---------------------------------------------------------------------------

AUTO_TOP1_SHARP_MIN = 0.89
AUTO_SLOPE_AT_3_SHARP_MIN = 0.08
AUTO_CONV_ID_DIVERSITY_THRESHOLD = 5
AUTO_TOP1_LOW_THRESHOLD = 0.50

# ---------------------------------------------------------------------------
# Compiled regex patterns
# ---------------------------------------------------------------------------

# Temporal cue pattern — matches temporal vocabulary (word-boundary, case-insensitive)
_TEMPORAL_PATTERNS = (
    r"\bwhen\b",
    r"\bwhat\s+date\b",
    r"\bhow\s+long\b",
    r"\bsince\b",
    r"\buntil\b",
    r"\bago\b",
    r"\bbefore\b",
    r"\bafter\b",
    r"\bfirst\b",
    r"\blast\b",
    r"\bearliest\b",
    r"\blatest\b",
    r"\brecently\b",
    r"\byesterday\b",
    r"\btoday\b",
    r"\btomorrow\b",
    # Weekday names
    r"\bmonday\b",
    r"\btuesday\b",
    r"\bwednesday\b",
    r"\bthursday\b",
    r"\bfriday\b",
    r"\bsaturday\b",
    r"\bsunday\b",
    # Month names
    r"\bjanuary\b",
    r"\bfebruary\b",
    r"\bmarch\b",
    r"\bapril\b",
    r"\bmay\b",
    r"\bjune\b",
    r"\bjuly\b",
    r"\baugust\b",
    r"\bseptember\b",
    r"\boctober\b",
    r"\bnovember\b",
    r"\bdecember\b",
    # "X days/weeks/months/years" pattern
    r"\b\d+\s+(?:days?|weeks?|months?|years?)\b",
)

TEMPORAL_RE = re.compile("|".join(_TEMPORAL_PATTERNS), re.IGNORECASE)

# Comparison/aggregation cue pattern
_COMPARISON_PATTERNS = (
    r"\bhow\s+many\b",
    r"\bhow\s+much\b",
    r"\bcount\b",
    r"\btotal\b",
    r"\bsum\b",
    r"\bcompare\b",
    r"\bwhich\s+is\b",
    r"\ball\b",
    r"\bevery\b",
    r"\beach\b",
)

COMPARISON_RE = re.compile("|".join(_COMPARISON_PATTERNS), re.IGNORECASE)

# Named-entity (proper-noun phrase) pattern — used by entity_anchored branch.
# Matches capitalized multi-word sequences like "Alice Smith", "New York", "Acme Corp".
# Single-word capitalized terms (e.g. start of sentence) intentionally excluded — require
# at least two capitalized words to reduce false positives.
_NAMED_ENTITY_RE = re.compile(r"[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+")


# ---------------------------------------------------------------------------
# Pre-retrieval signals (query text only)
# ---------------------------------------------------------------------------


def has_temporal_cues(query: str) -> bool:
    """Return True if the query contains temporal vocabulary."""
    if not query:
        return False
    return bool(TEMPORAL_RE.search(query))


def has_comparison_cues(query: str) -> bool:
    """Return True if the query contains comparison/aggregation vocabulary."""
    if not query:
        return False
    return bool(COMPARISON_RE.search(query))


def count_named_entities(query: str) -> int:
    """Return the count of capitalized multi-word proper-noun phrases in the query.

    Uses simple regex: sequences of two or more capitalized words (e.g. 'Alice Smith',
    'New York', 'Acme Corp'). Single-word caps (e.g. sentence-initial words) are excluded.
    Returns 0 if query is empty.
    """
    if not query:
        return 0
    return len(_NAMED_ENTITY_RE.findall(query))


# ---------------------------------------------------------------------------
# Post-retrieval signals (candidate list)
# ---------------------------------------------------------------------------


def top_1_score(candidates: list) -> float:
    """Return the score of the top-ranked candidate, or 0.0 if empty.

    Candidates is list[tuple[score, dict]] — same shape as memory_search_scored_impl.
    """
    if not candidates:
        return 0.0
    return float(candidates[0][0])


def slope_at_3(candidates: list) -> float:
    """Return the score drop per rank over the first 3 results (slope magnitude).

    Computed as (score[0] - score[min(2, len-1)]) / max(1, min(2, len-1)).
    Returns 0.0 if fewer than 2 candidates.
    """
    if len(candidates) < 2:
        return 0.0
    idx = min(2, len(candidates) - 1)
    top = float(candidates[0][0])
    bottom = float(candidates[idx][0])
    return (top - bottom) / max(1, idx)


def conv_id_diversity(candidates: list, top_n: int = 10) -> int:
    """Return the count of distinct non-empty conversation_ids in the top-N candidates.

    Candidates is list[tuple[score, dict]].
    """
    seen: set = set()
    for _, item in candidates[:top_n]:
        if isinstance(item, dict):
            cid = item.get("conversation_id") or ""
            if cid:
                seen.add(cid)
    return len(seen)


# ---------------------------------------------------------------------------
# Branch decision
# ---------------------------------------------------------------------------


def decide_branch(query: str, candidates: list, params: dict) -> str:
    """Decide which routing branch to use. First match wins.

    Branch order:
    1. temporal  — pre-retrieval: has_temporal_cues(query)
    2. multi_session — pre-retrieval: has_comparison_cues(query)
                     OR post-retrieval: conv_id_diversity > threshold
    3. sharp — post-retrieval: top_1 > sharp_min AND slope_at_3 > sharp_slope_min
               AND top_1 > low_threshold (OOD guard)
    4. entity_anchored — pre-retrieval: count_named_entities(query) >= threshold
                        AND auto_entity_graph_enabled=True
    5. default — fallback

    params may override any threshold via keys matching the AUTO_* constant names
    (lowercase, e.g. 'auto_top1_sharp_min').
    """
    # --- 1. temporal ---
    if has_temporal_cues(query):
        return "temporal"

    # Read thresholds from params (caller-overridable)
    diversity_threshold = params.get(
        "auto_conv_id_diversity_threshold", AUTO_CONV_ID_DIVERSITY_THRESHOLD
    )
    top1_sharp_min = params.get("auto_top1_sharp_min", AUTO_TOP1_SHARP_MIN)
    slope_sharp_min = params.get("auto_slope_at_3_sharp_min", AUTO_SLOPE_AT_3_SHARP_MIN)
    top1_low_threshold = params.get("auto_top1_low_threshold", AUTO_TOP1_LOW_THRESHOLD)

    # --- 2. multi_session ---
    if has_comparison_cues(query):
        return "multi_session"
    diversity = conv_id_diversity(candidates)
    if diversity > diversity_threshold:
        return "multi_session"

    # --- 3. sharp ---
    t1 = top_1_score(candidates)
    s3 = slope_at_3(candidates)
    if t1 > top1_sharp_min and s3 > slope_sharp_min and t1 > top1_low_threshold:
        return "sharp"

    # --- 4. entity_anchored ---
    entity_graph_enabled = params.get("auto_entity_graph_enabled", True)
    entity_threshold = params.get("auto_entity_graph_named_entity_threshold", 1)
    if entity_graph_enabled and count_named_entities(query) >= entity_threshold:
        return "entity_anchored"

    # --- 5. default ---
    return "default"


# ---------------------------------------------------------------------------
# Branch values
# ---------------------------------------------------------------------------


def branch_values(branch: str, params: dict) -> dict[str, Any]:
    """Return the parameter values AUTO sets for the given branch.

    Caller-overridable defaults are read from params (with fallback to module constants).
    Returns an empty dict for the 'default' branch — pure pass-through to function defaults.
    """
    if branch == "temporal":
        return {
            "k": params.get("auto_temporal_k", 15),
            "recency_bias": params.get("auto_temporal_recency_bias", 0.05),
            "expand_sessions": params.get("auto_temporal_expand_sessions", True),
            "graph_depth": params.get("auto_temporal_graph_depth", 1),
        }
    if branch == "multi_session":
        return {
            "k": params.get("auto_multi_k", 20),
            "expand_sessions": params.get("auto_multi_expand_sessions", True),
        }
    if branch == "sharp":
        return {
            "auto_sharp_threshold_ratio": params.get("auto_sharp_threshold_ratio", 0.85),
            "auto_sharp_k_min": params.get("auto_sharp_k_min", 3),
            "auto_sharp_k_max": params.get("auto_sharp_k_max", 10),
        }
    if branch == "entity_anchored":
        return {
            "entity_graph": True,
            "entity_graph_depth": params.get("auto_entity_graph_depth", 1),
            "entity_graph_max_neighbors": params.get("auto_entity_graph_max_neighbors", 20),
        }
    # default branch — no branch values, pure pass-through
    return {}


# ---------------------------------------------------------------------------
# Signals summary (for capture / audit)
# ---------------------------------------------------------------------------


def signals_summary(query: str, candidates: list) -> dict:
    """Return all computed signals as a dict for the capture record."""
    return {
        "has_temporal_cues": has_temporal_cues(query),
        "has_comparison_cues": has_comparison_cues(query),
        "top_1_score": top_1_score(candidates),
        "slope_at_3": slope_at_3(candidates),
        "conv_id_diversity": conv_id_diversity(candidates),
        "named_entity_count": count_named_entities(query),
    }
