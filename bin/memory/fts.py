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


# ──────────────────────────────────────────────────────────────────────────────
# FTS5 sanitization
# ──────────────────────────────────────────────────────────────────────────────
# Strips FTS5 operators from user input so a query like "AND OR NOT *(foo)" can't
# turn into a parse error or query injection. Operator words are matched as
# whole words; bracket / wildcard punctuation is matched anywhere.
_FTS_OPERATORS = re.compile(r"\b(OR|AND|NOT|NEAR)\b|[*()\[\]{}]")


def _sanitize_fts(query: str, max_len: int = 500) -> str:
    """Strip FTS5 operators from user input to prevent query injection."""
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
    """
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


def _query_title_token_set(query: str) -> frozenset[str]:
    """Tokenize a query into the set used for title-overlap scoring.

    Hoisted out of `_query_title_overlap` so callers in a hot loop can compute
    it once and reuse it across many titles. Returns frozenset for safe sharing.
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
