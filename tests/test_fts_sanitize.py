"""Behavioral regression tests for the FTS5 query sanitizer.

These are deliberately NOT parity tests (those live in test_fts_parity.py and
compare the Python body against the Rust port). This file pins the *contract*
that matters to callers: whatever `_sanitize_fts` / `_compile_fts_query`
produce must be safe to hand to an FTS5 `MATCH` clause without raising.

Regression for a real, content-dependent ("intermittent") production bug: the
sanitizer's blocklist regex left FTS5 operator characters (`-` `:` `^` `/` `.`
and most other punctuation) in place, so any chatlog/memory search whose terms
contained a model name (`gpt-4o`, `claude-code`), a hyphenated id, a range
(`100-200MB`), or a `field:value` token raised
`sqlite3.OperationalError: no such column: <token>` (or `syntax error near …`)
deep inside FTS5 — while plain-word searches worked. The fix switched to an
allowlist (`_FTS_NON_TERM`: replace every non-word/non-space char with a space).
No behavioral test exercised the sanitizer against a live FTS5 table, so it
survived. See bin/memory/fts.py `_FTS_NON_TERM` / `_sanitize_fts`.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

# bin/ is on sys.path for the MCP server at runtime; mirror that for tests.
_BIN = str(Path(__file__).resolve().parent.parent / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory.fts import _compile_fts_query, _sanitize_fts  # noqa: E402

# Inputs that used to crash FTS5: each contains an operator character (`-`, `:`,
# `^`) or a boolean keyword that FTS5 would otherwise interpret as syntax.
_OPERATOR_INPUTS = [
    "gpt-4o",
    "claude-code",
    "xyzzy-nomatch-query",
    "100-200MB",
    "model_id:claude",
    "foo:bar",
    "foo^bar",
    "-leading",
    "trailing-",
    "a-b-c-d",
    "AND OR NOT",
    "NEAR/3 quux",
    "co-author: jane",
    '"alpha"beta"',  # quoted phrase with an interior quote (exact-mode path)
    '"unterminated',
]

# Inputs that must keep working unchanged (no operator chars).
_PLAIN_INPUTS = [
    "PyPI limit increase",
    "immich upload",
    "hello world",
    "single",
    "",
    "   ",
]


@pytest.fixture
def fts_table():
    """An in-memory FTS5 table mirroring how memory_items_fts is queried."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE VIRTUAL TABLE t USING fts5(x);")
    conn.execute(
        "INSERT INTO t(x) VALUES "
        "('alpha gpt-4o claude-code beta nomatch model_id claude foo bar 100 200MB')"
    )
    conn.commit()
    yield conn
    conn.close()


def _match_must_not_raise(conn: sqlite3.Connection, fts_query: str) -> int:
    """Run an FTS5 MATCH; return row count. Raises if the query is malformed."""
    if not fts_query.strip():
        return 0
    cur = conn.execute("SELECT count(*) FROM t WHERE t MATCH ?", [fts_query])
    return cur.fetchone()[0]


@pytest.mark.parametrize("raw", _OPERATOR_INPUTS + _PLAIN_INPUTS)
def test_sanitize_fts_output_is_match_safe(fts_table, raw):
    """`_sanitize_fts` output must never make FTS5 MATCH raise."""
    sanitized = _sanitize_fts(raw)
    # The contract: no FTS5 operator chars survive into a bare MATCH term.
    for ch in "-:^*()[]{}":
        assert ch not in sanitized, f"{ch!r} survived sanitization of {raw!r}: {sanitized!r}"
    # And it must actually be accepted by FTS5.
    _match_must_not_raise(fts_table, sanitized)


@pytest.mark.parametrize("mode", ["fts5", "hybrid"])
@pytest.mark.parametrize("raw", _OPERATOR_INPUTS + _PLAIN_INPUTS)
def test_compile_fts_query_output_is_match_safe(fts_table, mode, raw):
    """`_compile_fts_query` output must never make FTS5 MATCH raise."""
    compiled, ok = _compile_fts_query(raw, mode)
    if not ok:
        # ok=False is the "no matchable tokens" signal; caller skips MATCH.
        return
    _match_must_not_raise(fts_table, compiled)


def test_hyphenated_term_still_matches_content(fts_table):
    """Stripping `-` to a space aligns with the tokenizer, so `gpt-4o` still hits.

    The default FTS5 tokenizer splits indexed `gpt-4o` into `gpt` + `4o`, so a
    query sanitized to `gpt 4o` matches the row. This guards against a naive
    "delete the hyphen" fix (`gpt4o`) that would silently stop matching.
    """
    sanitized = _sanitize_fts("gpt-4o")
    assert _match_must_not_raise(fts_table, sanitized) == 1


def test_colon_term_does_not_become_column_filter(fts_table):
    """`model_id:claude` must search for the words, not filter a column."""
    sanitized = _sanitize_fts("model_id:claude")
    # Both tokens are present in the row; should match, not raise on a bad column.
    assert _match_must_not_raise(fts_table, sanitized) == 1


def test_plain_query_is_untouched():
    """Operator-free queries pass through with only whitespace trimming."""
    assert _sanitize_fts("PyPI limit increase") == "PyPI limit increase"
    assert _sanitize_fts("  hello world  ") == "hello world"
