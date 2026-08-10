r"""Tenancy predicates must build their placeholders from the SEAM, never `?`.

A hardcoded `?` in an ordinary query is a portability bug. In a TENANCY filter it
is a security bug, because of how it fails:

  * on PostgreSQL `?` is a syntax error (verified against a live PG 16:
    `SELECT ... WHERE 1=1 AND user_id = ?` -> SyntaxError),
  * the expansion paths that carry these filters wrap their queries in
    `except Exception: logger.debug(...)`,
  * so the query dies silently and the expansion is dropped — and any caller
    that catches higher up and *continues* proceeds with the tenancy clause
    gone.

`graph._session_neighbor_ids` and `search_routing` were hardened for exactly
this on 2026-07-14. Two sites in `memory/search.py` (`_maybe_expand_routed` and
the two-stage observation expansion) were missed and still used `?` — found
2026-08-10 while investigating a one-off CI failure of
test_search_tenant_isolation.

This is a STATIC scan rather than a behavioural test on purpose: the failure it
guards is invisible on SQLite (where `?` is valid), so only reading the source
catches it before a PG deployment does.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"

# A tenancy predicate being appended to a SQL fragment with a literal `?`.
# Matches:  _tenancy_sql += " AND user_id = ?"
_RAW_TENANCY = re.compile(
    r"""\+=\s*["'][^"']*\bAND\s+(?:user_id|scope)\s*=\s*\?""",
    re.IGNORECASE,
)

# An IN-list built by hand rather than via dialect().placeholder(n).
_RAW_IN_LIST = re.compile(r"""["']\s*,\s*["']\s*\.join\(\s*\[?\s*["']\?["']""")


def _py_sources() -> list[Path]:
    return [p for p in _BIN.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize("path", _py_sources(), ids=lambda p: p.name)
def test_no_raw_qmark_in_a_tenancy_predicate(path):
    """`user_id`/`scope` filters must use dialect().param()."""
    src = path.read_text(encoding="utf-8", errors="ignore")
    hits = [
        f"{path.name}:{i}: {line.strip()}"
        for i, line in enumerate(src.splitlines(), 1)
        if _RAW_TENANCY.search(line)
    ]
    assert not hits, (
        "tenancy filter built with a hardcoded '?' — a syntax error on "
        "PostgreSQL, swallowed by the surrounding except, which drops the "
        "tenancy clause:\n  " + "\n  ".join(hits)
    )


def test_the_two_known_sites_are_fixed():
    """Pin the specific regression rather than trusting the scan alone."""
    src = (_BIN / "memory" / "search.py").read_text(encoding="utf-8")
    assert ' AND user_id = ?' not in src, "search.py still hardcodes a tenancy '?'"
    assert ' AND scope = ?' not in src, "search.py still hardcodes a tenancy '?'"
    # And the seam form IS present.
    assert "param()" in src


def test_expansion_in_lists_use_the_seam():
    """The IN-lists these tenancy filters ride along with must port too.

    Fixing only the tenancy clause would still leave the query unrunnable on
    PostgreSQL, so the guard covers both halves of the same statement.
    """
    src = (_BIN / "memory" / "search.py").read_text(encoding="utf-8")
    offenders = [
        f"search.py:{i}: {line.strip()}"
        for i, line in enumerate(src.splitlines(), 1)
        if _RAW_IN_LIST.search(line)
    ]
    assert not offenders, (
        "IN-list built with hardcoded '?' instead of dialect().placeholder(n):\n  "
        + "\n  ".join(offenders)
    )
