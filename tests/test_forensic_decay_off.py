"""Forensic reads can rank on the UNDECAYED importance.

Decay overwrites `importance` in place, so once a memory has faded there is no
way to ask "how important was this when it was written?" — which breaks the
three things that need it: a point-in-time read ("what did I believe on
2026-03-01"), a GDPR export of the original record, and any explanation of why a
row ranked where it did.

`importance_raw` (migration 049) preserves it, and `apply_decay=False` ranks on
that column instead.

⚠ THIS IS NOT A GENERAL SEARCH SWITCH. Ranking stale memories as though they
were fresh is precisely the context pollution decay exists to prevent. It is for
audit and export paths. The default stays decay-on, and the default SELECT stays
byte-identical — nobody doing an ordinary search pays for the extra column.

⚠ WHY THESE TESTS ASSERT AT THE SEAM AND THE SOURCE rather than end-to-end
through memory_search_scored_impl: the retrieval path in front of the ranker has
an FTS short-circuit that returns score 1.0 for exact-phrase hits WITHOUT
scoring, and a query that misses returns no candidates at all. Reaching the
hybrid scorer with a controlled corpus needs a query that matches loosely but not
exactly, which is fragile to seed and would make these tests flaky rather than
informative. The decision under test — WHICH COLUMN feeds the ranker — is
verifiable directly, and that is what is pinned here.
"""
from __future__ import annotations

import inspect
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory import search as S  # noqa: E402


def _src() -> str:
    return inspect.getsource(S.memory_search_scored_impl)


# ── the parameter exists at the convergence point ────────────────────────────

def test_apply_decay_defaults_to_on():
    """Decay-aware ranking is the default; forensic reads opt out."""
    sig = inspect.signature(S.memory_search_scored_impl)
    assert "apply_decay" in sig.parameters
    assert sig.parameters["apply_decay"].default is True


def test_the_flag_lives_on_the_single_scoring_owner():
    """⚠ §2: enforce at the CONVERGENCE POINT, not the call sites.

    memory_search_scored_impl is re-entered from seven places (routed x4,
    multi_db, plain, dispatch). Threading a kwarg through all of them is the
    drift pattern §10a has already been burned by in this exact file — three
    copies of the scoping predicates, where a tenancy fix reached all three but
    type_filter reached only one.

    Resolving it once here means every caller inherits the correct default and
    an eighth caller is correct by construction.
    """
    assert "apply_decay" in _src()
    # The sibling impls must NOT each carry their own copy of the decision.
    for name in ("memory_search_impl", "memory_search_routed_impl"):
        fn = getattr(S, name, None)
        if fn is None:
            continue
        body = inspect.getsource(fn)
        assert "importance_raw" not in body, (
            f"{name} reads importance_raw directly — the decision has been "
            "duplicated away from the convergence point."
        )


# ── the default path must not pay for the feature ────────────────────────────

def test_importance_raw_is_only_selected_when_decay_is_off():
    """§4: project only the columns you need.

    Same conditional-append shape as recency_bias -> valid_from and
    CONFIDENCE_RANKING -> confidence. An unconditional append would widen the
    SELECT for every ordinary search.

    ⚠ CALLS THE RESOLVER rather than grepping the impl's source. This used to
    assert on a substring of memory_search_scored_impl, which passed for the
    wrong reason: the append existed there, but TWO OTHER PATHS (the FTS
    exact-match short-circuit and the no-embedder FTS-only fallback) had their
    own copies of the allowlist and never appended it at all. A forensic read
    served by either silently ranked on the decayed value. The text was right
    and the behaviour was wrong -- so assert the behaviour.
    """
    assert "importance_raw" in S._resolve_extra_columns([], apply_decay=False), (
        "a forensic read does not project importance_raw, so it would rank on "
        "the decayed importance"
    )
    assert "importance_raw" not in S._resolve_extra_columns([], apply_decay=True), (
        "the default search fetches a column it does not use"
    )


def test_every_path_resolves_columns_through_one_owner():
    """⚠ THE DEFECT THIS FILE MISSED. Three copies of the allowlist existed --
    the main path, the short-circuit, and the FTS-only fallback -- and only one
    of them knew about importance_raw.

    That block has drifted this way before: its own comment records tenancy
    predicates going missing from it in 2026-07, leaking cross-tenant rows,
    because the early-return path never reached the filtered branch. A second
    allowlist literal in this module is the defect, independent of whether this
    particular column is in it (§10a).
    """
    src = inspect.getsource(S)
    # The SET LITERAL specifically -- a caller passing a couple of column names
    # (line ~2470) or a docstring listing them is not a second allowlist. Anchor
    # on the pair that only the allowlist definition contains.
    copies = src.count('"corroboration_count", "contradiction_count",')
    assert copies == 1, (
        f"the extra-column allowlist appears {copies} times — import "
        f"_resolve_extra_columns instead of writing a second copy"
    )
    # And the filter itself: every path must call the resolver, not re-derive it.
    assert "if c in _allowed_extra" not in src, (
        "a path is filtering extra columns with its own local allowlist again"
    )


def test_importance_raw_is_allowlisted():
    """The allowlist is the gate; an un-allowlisted column is silently dropped."""
    assert "importance_raw" in S._ALLOWED_EXTRA


# ── the scoring decision ─────────────────────────────────────────────────────

# ⚠ CALL THE REAL FUNCTION, never a mirror of it. An earlier version of this
# file reimplemented the selection logic here; planting a removed NULL-fallback
# into the production code then passed every test, because the test was
# validating its own copy. §2: duplicated resolution logic is the defect,
# independent of correctness.
_pick = S._ranking_importance


def test_decay_off_ranks_on_the_undecayed_value():
    row = {"importance": 0.01, "importance_raw": 0.99}
    assert _pick(row, True) == pytest.approx(0.01)
    assert _pick(row, False) == pytest.approx(0.99)


def test_the_difference_is_large_enough_to_matter():
    """A forensic read must actually re-rank, or the flag is decoration (§5)."""
    from memory.config import IMPORTANCE_WEIGHT

    row = {"importance": 0.01, "importance_raw": 0.99}
    delta = IMPORTANCE_WEIGHT * (_pick(row, False) - _pick(row, True))
    assert delta > 0.1, (
        f"decay-off changes the score by only {delta:.4f} — comparable to "
        "rounding, so the forensic view would be indistinguishable from the "
        "normal one."
    )


@pytest.mark.parametrize("raw", [None, 0.0])
def test_missing_or_null_raw_falls_back_to_importance(raw):
    """Pre-049 rows and rows written before the backfill must not read as 0.

    A NULL importance_raw scoring as 0.0 would send every old memory to the
    bottom of a forensic query — the opposite of what the view is for.
    """
    row = {"importance": 0.42, "importance_raw": raw}
    expected = 0.42 if raw is None else 0.0
    assert _pick(row, False) == pytest.approx(expected)


def test_a_row_without_the_column_at_all_falls_back(_row_types=None):
    """A pre-049 DB has no importance_raw column; the read must not raise.

    Exercised against real row shapes rather than the source text: a sqlite3.Row
    raises IndexError for an unknown key, a plain dict raises KeyError, and a
    tuple-like row raises TypeError. All three must degrade to `importance`.
    """
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    legacy = conn.execute("SELECT 0.42 AS importance").fetchone()
    assert _pick(legacy, False) == pytest.approx(0.42), (
        "a pre-049 row (no importance_raw column) did not fall back"
    )

    assert _pick({"importance": 0.42}, False) == pytest.approx(0.42)


def test_the_selection_lives_in_one_named_function():
    """The decision must be callable, so tests exercise IT and not a copy.

    This is the guard for the mistake that produced it: the logic was inlined,
    the test reimplemented it, and planting a removed NULL-fallback passed
    everything because the test validated its own duplicate.
    """
    assert callable(getattr(S, "_ranking_importance", None))
    assert "_ranking_importance(row, apply_decay)" in _src(), (
        "the scoring site stopped calling the shared selector — the logic has "
        "been inlined again and the tests below now check a copy."
    )
