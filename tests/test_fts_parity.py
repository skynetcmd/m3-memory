"""Byte-exact parity gate: Rust m3_core_rs FTS vs the pure-Python fts.py path.

Oxidation Task (M3-v3 Milestone 4): _sanitize_fts and _compile_fts_query in
bin/memory/fts.py route through m3_core_rs.sanitize_fts / compile_fts_query when
the extension is present, with the regex/split Python body as fallback. FTS
recall depends on these matching EXACTLY (including Python quirks like a lone
quote char being treated as an exact phrase), so any divergence is a finding.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

pytest.importorskip("m3_core_rs")
import m3_core_rs  # noqa: E402

from memory import fts as ftsmod  # noqa: E402

# Inputs that stress every branch: operators, exact phrases (incl. lone quote),
# punctuation, Unicode, truncation, multi-space, mixed quotes, casing.
INPUTS = [
    "red wine", "wine", "AND OR NOT *(foo)", "ANDROID phones", "NEAR/3 quux",
    '"red wine"', "'red wine'", '"', "'", "''", '""', "  ", "",
    "Hello, World!", "a.b:c;d/e?f", "café", "naïve query", "MCP-tool",
    "content_hash", "agent_id", "what did Caroline say about wine",
    "DROP TABLE", "foo*bar", "(parens)", "[brackets]", "{braces}",
    "multi   space    tokens", "trailing space ", " leading space",
    "ALLCAPS QUERY", "Mixed Case Thing", "123 456", "a1b2c3",
    "one", "two words", "three word phrase here", "x" * 600,
    "réseau électrique", "日本語 query", "emoji 🎉 test",
    "NOTthis", "ORnot", "ANDy", "near miss", "a-b-c", "under_score",
    "'quoted start", "quoted end'", "mismatch\"quote'", "tab\tsep", "new\nline",
]
MODES = ["fts5", "hybrid", "semantic", "exact", ""]


def _py_sanitize(query, max_len=500):
    """The pure-Python _sanitize_fts body, regardless of m3_core_rs presence.

    Mirrors the two-pass fts.py body: operator words removed, then every
    non-word/non-space character replaced with a space (FTS5-MATCH allowlist).
    """
    if len(query) > max_len:
        query = query[:max_len]
    query = ftsmod._FTS_OPERATORS.sub(" ", query)
    query = ftsmod._FTS_NON_TERM.sub(" ", query)
    return query.strip()


def _py_compile(query, mode):
    """The pure-Python _compile_fts_query body (mirrors fts.py fallback)."""
    is_exact_query = (query.startswith('"') and query.endswith('"')) or (
        query.startswith("'") and query.endswith("'")
    )
    if is_exact_query:
        inner = query[1:-1].replace('"', '""')
        return f'"{inner}"', True
    clean = _py_sanitize(query)
    clean = ftsmod._sanitize_for_searchable(clean)
    if not clean.strip():
        return "", False
    clean = clean.strip()
    if mode == "fts5":
        toks = [t for t in clean.split() if t]
        if len(toks) > 1:
            return " OR ".join(toks), True
        return (f"{clean}*" if clean.isalnum() else clean), True
    if " " not in clean and clean.isalnum():
        return f"{clean}*", True
    return clean, True


@pytest.mark.parametrize("q", INPUTS)
def test_sanitize_parity(q):
    assert m3_core_rs.sanitize_fts(q, 500) == _py_sanitize(q, 500), q


@pytest.mark.parametrize("q", INPUTS)
@pytest.mark.parametrize("mode", MODES)
def test_compile_parity(q, mode):
    rs = tuple(m3_core_rs.compile_fts_query(q, mode))
    py = _py_compile(q, mode)
    assert rs == py, f"q={q!r} mode={mode!r} rust={rs!r} py={py!r}"


def test_lone_quote_quirk_preserved():
    """Regression fence: a lone quote is start==end -> exact phrase -> ('""', True).
    The Rust port must NOT 'fix' this; fts.py is the source of truth."""
    assert tuple(m3_core_rs.compile_fts_query('"', "fts5")) == ('""', True)
    assert tuple(m3_core_rs.compile_fts_query("'", "hybrid")) == ('""', True)


def test_fts_module_uses_rust_when_present():
    """The wired fts.py functions return the same result as direct Rust calls."""
    for q in ["red wine", "wine", '"exact phrase"', "AND OR x"]:
        for mode in MODES:
            assert ftsmod._compile_fts_query(q, mode) == tuple(
                m3_core_rs.compile_fts_query(q, mode)
            ), f"{q!r}/{mode!r}"
