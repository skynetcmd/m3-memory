"""FTS5 query helpers and title-overlap math.

Pulled out of legacy `bin/memory_core.py` in Phase 2. Pure functions with
no I/O. The regexes and translation tables are module-level constants set
at import; everything else is stateless.

Used by the search and write paths. Re-exported through `memory_core` for
back-compat with the 22+ callers that import these directly.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from . import config

__all__ = [
    "_sanitize_fts",
    "_sanitize_for_searchable",
    "_compile_fts_query",
    "_augment_title_with_role",
    "_query_title_token_set",
    "_title_overlap_from_qset",
    "_query_title_overlap",
    "_TEMPORAL_QUERY_RE",
    "_DATE_RE_ISO",
    "_DATE_RE_LONG",
    "_DATE_MONTHS",
    "_TEMPORAL_ROUTER_PATTERNS",
    "_TEMPORAL_ROUTER_RE",
    "_ENTITY_MENTION_PATTERNS",
    "_ENTITY_MENTION_RE",
    "_EVENT_VERB_LIST",
    "_EVENT_SENT_SPLIT",
    "_EVENT_DATE_HINT",
    "_EVENT_VERB_RE",
    "_EVENT_PROPER_NOUN",
]
import logging

logger = logging.getLogger("memory.fts")


from . import config

# ──────────────────────────────────────────────────────────────────────────────
# Temporal & Entity patterns (hoisted from search.py to break cycles)
# ──────────────────────────────────────────────────────────────────────────────
_DATE_RE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_RE_LONG = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|"
    r"apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)(?:\s+,?\s*(\d{4}))?\b",
    re.IGNORECASE
)
_DATE_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12
}

_TEMPORAL_QUERY_RE = re.compile(
    r"\b(?:last|next|past|this|current|previous)\b|\b(?:today|yesterday|tomorrow)\b|"
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|"
    r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b|"
    r"\d{4}-\d{2}-\d{2}|"  # ISO date
    r"\b(?:morning|afternoon|evening|night|week|month|year|ago|recently|lately)\b",
    re.IGNORECASE
)

# Module-level temporal regex — same patterns memory `2d1d5812` documented;
# 100% recall on LongMemEval temporal-reasoning, low FPR on others.
# (Restored from pre-Phase-7+8 search.py; the refactor lost ~10 patterns
# when consolidating into fts.py — see test_memory_search_routed.py.)
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

_ENTITY_MENTION_PATTERNS = (
    r'"[^"]+"',                            # double-quoted strings
    r"'[^']+'",                            # single-quoted strings
    r"\b(?:19|20)\d{2}\b",                # 4-digit years (1900–2099)
    r"\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2}\b",   # Month Day
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*",   # Capitalized noun phrases
)
_ENTITY_MENTION_RE = re.compile("|".join(_ENTITY_MENTION_PATTERNS))

# Event extraction patterns
_EVENT_VERB_LIST = (
    "decided", "started", "finished", "completed", "installed", "configured",
    "updated", "removed", "fixed", "bought", "purchased", "booked", "traveled",
    "met", "attended", "presented", "wrote", "released", "launched", "deployed",
)
_EVENT_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_EVENT_DATE_HINT = re.compile(
    r"\b(?:today|yesterday|tomorrow|last|next|this|on|at|in|ago|from|to)\b|\d{4}-\d{2}-\d{2}",
    re.IGNORECASE
)
_EVENT_VERB_RE = re.compile(
    r"\b(" + "|".join(_EVENT_VERB_LIST) + r")\b", re.IGNORECASE
)
_EVENT_PROPER_NOUN = re.compile(r"\b([A-Z][a-z]{2,})\b")


# ──────────────────────────────────────────────────────────────────────────────
# FTS5 sanitization
# ──────────────────────────────────────────────────────────────────────────────
# Strips FTS5 operators from user input so a query like "AND OR NOT *(foo)" can't
# turn into a parse error or query injection. Operator words are matched as
# whole words; bracket / wildcard punctuation is matched anywhere.
_FTS_OPERATORS = re.compile(r"\b(OR|AND|NOT|NEAR)\b|[*()\[\]{}]")


def _sanitize_fts(query: str, max_len: int = 500) -> str:
    """Strip FTS5 operators from user input to prevent query injection.

    Oxidation: routed through m3_core_rs.sanitize_fts (byte-exact Rust port,
    no regex engine) when the extension is present; the regex body below is the
    parity fallback. Parity gated by tests/test_fts_parity.py.
    """
    _rs = config.m3_core_rs
    if _rs is not None:
        try:
            return _rs.sanitize_fts(query, max_len)
        except Exception:  # noqa: BLE001 — FFI hiccup falls back to Python
            pass
    if len(query) > max_len:
        query = query[:max_len]
    return _FTS_OPERATORS.sub(" ", query).strip()


# Mirror of the SQLite mi_fts_insert trigger sanitization. The trigger
# lowercases and replaces these 8 punctuation chars with spaces before storing
# in content_searchable / title_searchable. Query-side text must apply the same
# transform so MATCH terms align with what FTS5 indexed.
_SEARCHABLE_PUNCT = str.maketrans({c: " " for c in "?!:.,;/\"'"})


def _sanitize_for_searchable(text: str) -> str:
    """Apply the same lowercase + depunctuate transform as the FTS triggers."""
    if not text:
        return ""
    return text.lower().translate(_SEARCHABLE_PUNCT)


# Cached compilation of (raw_query, mode) -> (fts_string, ok). Hot path: search
# is called repeatedly with similar queries inside a session, and the cache
# absorbs the regex + splitting cost.
@lru_cache(maxsize=2048)
def _compile_fts_query(query: str, mode: str) -> tuple[str, bool]:
    """Compile a raw user query into an FTS5 MATCH string.

    Returns ``(fts_query, ok)``. When ``ok`` is False the caller should treat
    this as "no matchable tokens"; in ``mode == "fts5"`` that means return no
    results, in any other mode that means fall back to semantic-only.

    Exact-mode preserves the quoted phrase as-is; otherwise the query is
    depunctuated to match the FTS trigger's normalized storage, then either
    wildcarded (single-token alnum) or OR-joined (multi-token in ``fts5`` mode)
    or passed straight through.

    Oxidation: routed through m3_core_rs.compile_fts_query (byte-exact Rust port)
    when the extension is present; the Python body below is the parity fallback.
    The @lru_cache wraps this function, so the FFI crossing is paid once per
    unique (query, mode). Parity gated by tests/test_fts_parity.py.
    """
    _rs = config.m3_core_rs
    if _rs is not None:
        try:
            return tuple(_rs.compile_fts_query(query, mode))
        except Exception:  # noqa: BLE001 — FFI hiccup falls back to Python
            pass
    is_exact_query = (query.startswith('"') and query.endswith('"')) or (
        query.startswith("'") and query.endswith("'")
    )
    if is_exact_query:
        return f'"{query[1:-1]}"', True
    clean = _sanitize_fts(query)
    clean = _sanitize_for_searchable(clean)
    if not clean.strip():
        return "", False
    clean = clean.strip()
    if mode == "fts5":
        toks = [t for t in clean.split() if t]
        if len(toks) > 1:
            return " OR ".join(toks), True
        return (f"{clean}*" if clean.isalnum() else clean), True
    # hybrid / semantic fallback path
    if " " not in clean and clean.isalnum():
        return f"{clean}*", True
    return clean, True


# ──────────────────────────────────────────────────────────────────────────────
# Title overlap (ranker helper)
# ──────────────────────────────────────────────────────────────────────────────
_TOKEN_SPLIT = re.compile(r"[^\w]+", re.UNICODE)


def _augment_title_with_role(title: str, metadata: str | dict | None) -> str:
    """Prepend '[role] ' to title when metadata carries a person-name role.

    Makes the speaker visible to FTS so queries like 'what did Caroline say
    about X' can match turns by Caroline. Idempotent: skips when title is
    already bracket-prefixed. Gated by config.SPEAKER_IN_TITLE.
    """
    if not config.SPEAKER_IN_TITLE:
        return title or ""
    t = (title or "").strip()
    if t.startswith("["):
        return t
    if not metadata:
        return t
    try:
        meta = metadata if isinstance(metadata, dict) else json.loads(metadata)
    except (json.JSONDecodeError, TypeError):
        return t
    role = (meta.get("role") or "").strip()
    # Only prepend when role looks like a proper name (avoid 'user'/'assistant'
    # generics which add noise without helping real-world queries).
    if not role or role.lower() in ("user", "assistant", "system", "tool"):
        return t
    return f"[{role}] {t}".strip()


@lru_cache(maxsize=1024)
def _query_title_token_set(query: str) -> frozenset[str]:
    """Tokenize a query into the set used for title-overlap scoring.

    Hoisted out of `_query_title_overlap` so callers in a hot loop can
    compute it once and reuse it across many titles. Returns frozenset
    for safe sharing.

    lru-cached at 1024 entries: searches frequently repeat the same query
    (bench harnesses, redo-typed-query flows, paginated search), and
    tokenization is pure-Python regex+split work — cache hit avoids it
    entirely. Frozenset return makes the cached value safe to share
    across concurrent callers.
    """
    if not query:
        return frozenset()
    return frozenset(t for t in _TOKEN_SPLIT.split(query.lower()) if len(t) > 2)


def _title_overlap_from_qset(q_tokens: frozenset[str], title: str) -> float:
    """Same as `_query_title_overlap` but with the query token set precomputed."""
    if not q_tokens or not title:
        return 0.0
    t_tokens = {t for t in _TOKEN_SPLIT.split(title.lower()) if len(t) > 2}
    if not t_tokens:
        return 0.0
    overlap = q_tokens & t_tokens
    return len(overlap) / len(q_tokens) if q_tokens else 0.0


def _query_title_overlap(query: str, title: str) -> float:
    """Fraction of query tokens that also appear in title. 0.0 when no overlap.

    Used as a small ranker boost for titles that literally echo query terms.
    Kept for back-compat with single-call callers; hot loops should use
    `_query_title_token_set` once + `_title_overlap_from_qset` per title.
    """
    if not query or not title:
        return 0.0
    return _title_overlap_from_qset(_query_title_token_set(query), title)
