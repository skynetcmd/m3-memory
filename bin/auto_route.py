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

import logging
import os
import re
from typing import Any

_log = logging.getLogger(__name__)

# ── Project Oxidation: optional Rust route decider (SHADOW MODE ONLY) ─────────
# m3_core_rs is an optional dependency (pip install m3-memory[oxidation]).
# M3_CORE_RS_DISABLE=1 forces the Python path even when the wheel is installed
# — the load-bearing kill-switch from the oxidation plan §9.6. Import failure
# is non-fatal: auto_route runs fully on the Python path without the core.
_OXIDATION_DISABLED = os.environ.get("M3_CORE_RS_DISABLE", "0").lower() in ("1", "true", "yes")
m3_core_rs = None
if not _OXIDATION_DISABLED:
    try:
        import m3_core_rs  # type: ignore
    except ImportError:
        m3_core_rs = None  # extra not installed — Python path is the default

# Shadow-mode flag. Uses M3_ROUTE_SHADOW_MODE (plan §9.6) with values off/log;
# `enforce` is intentionally NOT implemented — cutover is out of scope here.
# Anything other than "log" (case-insensitive) means shadow is OFF.
_ROUTE_SHADOW_MODE = os.environ.get("M3_ROUTE_SHADOW_MODE", "off").lower()
_ROUTE_SHADOW = _ROUTE_SHADOW_MODE == "log"

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
# Rust shadow comparison (observe-only — never changes routing)
# ---------------------------------------------------------------------------


def _map_rust_to_py_branch(query: str, rs_branch: str) -> str:
    """Maps Rust pre-retrieval route branches to conceptual Python branch names."""
    # 1. Temporal cues fallback (regex check matches Python temporal decider)
    if has_temporal_cues(query):
        return "temporal"
    
    # 2. Named entities match entity anchored
    if rs_branch == "entity":
        return "entity_anchored"
    
    # 3. Lexical maps directly to default fallback
    if rs_branch == "lexical":
        return "default"
    
    # 4. Semantic maps to default or multi_session
    if rs_branch == "semantic":
        # If the query has comparison cues, map to multi_session
        if has_comparison_cues(query):
            return "multi_session"
        return "default"
        
    return rs_branch


def _route_shadow_compare(query: str, py_branch: str) -> None:
    """Run the Rust route decider alongside Python and log any disagreement.

    SHADOW MODE ONLY — this function never affects the returned branch. Python
    stays fully authoritative; this just collects data for a *later* cutover
    decision (oxidation plan §4c.5 / §9.6).

    NOTE: some disagreement is EXPECTED and not a bug. The Python decider uses
    post-retrieval signals derived from the `candidates` list (top_1_score,
    slope_at_3, conv_id_diversity); the Rust `decide_route` only sees the query
    string and has no candidates input. The two also use different branch-name
    vocabularies. The shadow's job is to *quantify* the disagreement rate, not
    to achieve zero disagreement.

    No-op when the Rust core is unavailable or shadow mode is off. Any failure
    is caught and logged — a shadow error must never break routing.
    """
    if m3_core_rs is None or not _ROUTE_SHADOW:
        return
    try:
        signals = m3_core_rs.extract_signals(query)
        decision = m3_core_rs.decide_route(query, signals)
        rs_branch = decision.branch
        mapped_branch = _map_rust_to_py_branch(query, rs_branch)
        
        if mapped_branch == py_branch:
            _log.debug(
                "route shadow AGREE: branch=%s query=%r rs_confidence=%.3f",
                py_branch, query, decision.confidence,
            )
        else:
            _log.warning(
                "route shadow DISAGREE: py_branch=%s rs_branch=%s mapped_branch=%s query=%r "
                "rs_confidence=%.3f rs_signal_breakdown=%s",
                py_branch, rs_branch, mapped_branch, query, decision.confidence,
                decision.signal_breakdown,
            )
    except Exception as exc:  # noqa: BLE001 — shadow must never break routing
        _log.warning("route shadow comparison failed (non-fatal): %s", exc)


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
    if _ROUTE_SHADOW_MODE == "enforce" and m3_core_rs is not None:
        try:
            signals = m3_core_rs.extract_signals(query)
            decision = m3_core_rs.decide_route(query, signals)
            rs_branch = decision.branch
            mapped_branch = _map_rust_to_py_branch(query, rs_branch)
            _log.debug(
                "route enforce mode: rs_branch=%s -> mapped_branch=%s query=%r confidence=%.3f",
                rs_branch, mapped_branch, query, decision.confidence
            )
            return mapped_branch
        except Exception as exc:
            _log.warning("Rust enforce-mode routing failed, falling back to Python decider: %s", exc)

    branch = _decide_branch_impl(query, candidates, params)
    # Shadow hook — runs the Rust route decider alongside Python and logs
    # disagreements. Observe-only: `branch` is returned unchanged regardless.
    _route_shadow_compare(query, branch)
    return branch


def _decide_branch_impl(query: str, candidates: list, params: dict) -> str:
    """Authoritative Python branch decision (see decide_branch docstring)."""
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
