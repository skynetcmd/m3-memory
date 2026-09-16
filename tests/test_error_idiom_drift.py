"""Guard: a DIAGNOSTIC error states what was observed and what to inspect.

DESIGN_PHILOSOPHIES §3 — "State what you OBSERVED; mark what you INFERRED." An
error message is a claim about the world and must carry its evidence level:

  * ``observed:`` — measured values only (counts, resolved paths, actual state)
  * ``cause:``    — ONLY when the mechanism is verified in code
  * ``possible:`` — candidate explanations when it is not
  * ``inspect:``  — the knob, env var or file the operator should go look at

The cost of not doing this is concrete and shipped: "Check embedder
availability" stated a guess as a diagnosis and sent an operator to inspect a
perfectly healthy server. The corrected message reads ``observed: 5 failed
batch(es), 0 content-shaped`` / ``possible: server unreachable, overloaded, or a
network path issue`` / ``inspect: M3_EMBED_FALLBACK_URL``.

WHAT THIS GUARD DOES **NOT** COVER — and deliberately so.

Only DIAGNOSTIC errors are in scope: the ones where an operator has to go look
at something (a service is unreachable, a path resolved wrong, a backend
failed). A CONTRACT VIOLATION is not in scope and must not be "fixed" to pass
this test::

    raise ValueError("conversation_id is required")      # already correct
    raise ValueError(f"role must be one of {VALID_ROLES}")  # already correct

For those, the observed value IS the argument and the knob IS the parameter
name. Adding ``observed:``/``inspect:`` ceremony to them makes every message
longer and trains readers to skim — which §3 calls out in its own terms: "a
false alarm is a §3 violation, not a safe default." A guard that pushes authors
toward noise is worse than no guard.

WHY A RATCHET AND NOT A HARD ZERO.

Measured 2026-09-16: 30 diagnostic sites, 2 labelled — 28 unlabelled, brought
down to 17 the same day. A hard zero fails on day
one, and a guard nobody can make pass gets deleted — which is strictly worse
than one that only ever tightens. This pins the current count of UNLABELLED
diagnostic sites so the debt can shrink but never grow.

To LOWER the budget after labelling sites: run this file, take the number it
reports, and lower ``_BUDGET``. That is the intended workflow.

To ADD a legitimate unlabelled diagnostic: that is almost always wrong. Label
it instead. If it genuinely is a contract violation that this heuristic
mis-flags, add the file to ``_EXEMPT`` with a one-line reason — a visible,
reviewable edit rather than a silent new copy.
"""
from __future__ import annotations

import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Current floor. Lower this as sites are labelled; never raise it.
# 2026-09-16: 28 -> 17 after labelling the enrich, crypto/auth and
# server/backend subsystems.
_BUDGET = 17

_SKIP_DIRS = {
    "build",            # packaging copy of the tree
    "tests",            # fixtures raise deliberately malformed errors
    "to_be_deleted",
    ".git",
    "__pycache__",
    "node_modules",
    ".scratch",
}

# Files whose flagged sites are contract violations this heuristic mis-reads.
# One line of reason each, so the exemption is reviewable.
_EXEMPT: dict[str, str] = {
    # _validate_role() validates an argument that becomes a filename; the
    # message already names the observed value and the allowed set.
    "bin/m3_halt.py": "argument validation, not a diagnostic",
}

# A raise/exit whose text mentions a knob, an endpoint, or a failure verb —
# i.e. something the operator would have to go and look at.
_DIAGNOSTIC = re.compile(
    r'(raise\s+(?:ValueError|RuntimeError|OSError)\(|sys\.exit\()f?"[^"]*'
    r'(M3_[A-Z_]+|http://|https://|unreachable|failed to|could not'
    r'|timeout|refused|not found)',
    re.IGNORECASE,
)

_LABEL = re.compile(r"observed:|possible:|cause:|inspect:")

# How far past the match to look for a label — long enough to cover an
# implicit-concatenation message spanning several source lines.
_WINDOW = 400


def _unlabelled_sites() -> list[str]:
    found: list[str] = []
    for path in (_ROOT / "bin").rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(_ROOT).as_posix()
        if rel in _EXEMPT:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _DIAGNOSTIC.finditer(text):
            if not _LABEL.search(text[match.start(): match.start() + _WINDOW]):
                line = text[: match.start()].count("\n") + 1
                found.append(f"{rel}:{line}")
    return sorted(found)


class ErrorIdiomDriftTest(unittest.TestCase):
    def test_unlabelled_diagnostics_do_not_grow(self):
        sites = _unlabelled_sites()
        self.assertLessEqual(
            len(sites),
            _BUDGET,
            "Unlabelled diagnostic error sites rose from "
            f"{_BUDGET} to {len(sites)}. A diagnostic error must say what was "
            "observed and what to inspect (DESIGN_PHILOSOPHIES §3). New or "
            "changed sites:\n  " + "\n  ".join(sites),
        )

    def test_budget_is_not_stale(self):
        """If the count has dropped, the budget must come down with it.

        Without this, the guard silently stops ratcheting: someone labels ten
        sites, the budget stays high, and ten new unlabelled ones can appear
        without failing anything.
        """
        sites = _unlabelled_sites()
        self.assertGreaterEqual(
            len(sites),
            _BUDGET,
            f"Unlabelled diagnostics dropped to {len(sites)} but _BUDGET is "
            f"still {_BUDGET}. Lower _BUDGET to {len(sites)} to lock in the "
            "improvement.",
        )

    def test_the_guard_actually_matches(self):
        """Self-check: the patterns must match a real message of each kind.

        A drift guard blind to the shape it polices passes forever while the
        thing it guards rots.
        """
        diagnostic = (
            'raise RuntimeError(f"embed failed. '
            'observed: {n} failed batch(es). inspect: M3_EMBED_FALLBACK_URL")'
        )
        self.assertTrue(_DIAGNOSTIC.search(diagnostic), "pattern misses a diagnostic")
        self.assertTrue(_LABEL.search(diagnostic), "label pattern misses observed:")

        unlabelled = 'sys.exit("could not reach the server at http://127.0.0.1:8082")'
        self.assertTrue(_DIAGNOSTIC.search(unlabelled), "pattern misses an exit")
        self.assertFalse(_LABEL.search(unlabelled), "label pattern false-positives")

        contract = 'raise ValueError("conversation_id is required")'
        self.assertIsNone(
            _DIAGNOSTIC.search(contract),
            "contract violations must stay OUT of scope — see the module "
            "docstring; flagging them pushes authors toward noise",
        )


if __name__ == "__main__":
    unittest.main()
