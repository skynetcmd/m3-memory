#!/usr/bin/env python3
"""Advisory design-philosophy linter — warns on concrete anti-patterns.

Scans the Python files changed in the push range against the *mechanically
checkable* rules from docs/DESIGN_PHILOSOPHIES.md, and prints a warning citing
the tenet for each hit. It is ADVISORY: it always exits 0 and never blocks a
push — many hits are legitimate (a one-off migration's SELECT *, an inline
PRAGMA in a test fixture). It surfaces drift; the author judges.

What it CAN check (high-confidence, low false-positive — the doc names these
as explicit bans):
  §4 Efficiency / §8 Perf   — no `SELECT *`; reuse the pool, don't `sqlite3.connect`
  §10 DB hygiene            — `apply_pragmas`, never inline `PRAGMA`
  §3 Robustness / §12       — every `ToolSpec(` has a `description=`

Deliberately NOT checked by regex: f-string / %-format / .format into
`execute()`. A regex can't distinguish a SQL-injection (interpolating user
data) from safe clause-building (interpolating a fixed `WHERE {clause}` whose
*values* are still `?`-bound). It flagged too many safe call-sites to be
useful advice. §6's parameterized-SQL rule is enforced by Bandit in CI
(B608) and by review, not here.

What it CANNOT check (judgment tenets — left to review / bench / PR checklist):
  §5 Effectiveness (pre-registered metric), §8 perf budgets (need a bench run),
  §2 modularity identity (covered by parity tests), "one feature per PR".

Run standalone:
    python bin/check_design_philosophy.py                # changed-vs-origin/main
    python bin/check_design_philosophy.py --all          # whole bin/ + m3_memory/
    python bin/check_design_philosophy.py --range A..B    # explicit range
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Files where an anti-pattern is expected/legitimate — never warn on these.
_SKIP_SUBSTR = (
    "/tests/", "\\tests\\", "test_",
    "migrate_", "migration", "/benchmarks/", "\\benchmarks\\",
    "check_design_philosophy.py",  # this file documents the patterns
)

# Each rule: (compiled regex, short message, tenet citation). Regexes are
# deliberately conservative — better to under-warn than cry wolf on an
# advisory check.
_RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"SELECT\s+\*", re.I),
     "SELECT * — project only needed columns",
     "§4 Efficiency / §8 Performance"),
    (re.compile(r"sqlite3\.connect\("),
     "sqlite3.connect( — reuse the _db() pool, don't spawn connections",
     "§4 Efficiency / §8 Performance (connection pool reuse)"),
    (re.compile(r"""\.execute\w*\(\s*["']\s*PRAGMA""", re.I),
     "inline PRAGMA — route through apply_pragmas(profile_for_db(path))",
     "§10 Database hygiene"),
]

# ToolSpec without a description= is a §3/§12 smell — checked specially since
# it can span multiple lines.
# Match the actual constructor call `ToolSpec(` only — no space before the
# paren, and at a statement/argument boundary — so prose like "every ToolSpec
# (~85 tools)" in a docstring doesn't false-match.
_TOOLSPEC_RE = re.compile(r"(?:^|[\s(,=\[])ToolSpec\(")


def _changed_py_files(rng: str | None, scan_all: bool) -> list[Path]:
    if scan_all:
        return [p for d in ("bin", "m3_memory")
                for p in (_ROOT / d).rglob("*.py")]
    if rng is None:
        # default: committed changes not yet on origin/main; fall back to HEAD
        has_main = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "origin/main"],
            cwd=_ROOT, capture_output=True).returncode == 0
        rng = "origin/main..HEAD" if has_main else "HEAD"
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=d", rng, "--", "*.py"],
        cwd=_ROOT, capture_output=True, text=True)
    return [_ROOT / line for line in out.stdout.splitlines() if line.strip()]


def _skip(path: Path) -> bool:
    s = str(path).replace("\\", "/").lower()
    return any(tok.replace("\\", "/").lower() in s for tok in _SKIP_SUBSTR)


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return (lineno, message, tenet) hits for one file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    hits: list[tuple[int, str, str]] = []
    text = "\n".join(lines)

    for rx, msg, tenet in _RULES:
        for m in rx.finditer(text):
            lineno = text.count("\n", 0, m.start()) + 1
            hits.append((lineno, msg, tenet))

    # ToolSpec(...) blocks: warn if the parenthesized call has no description=.
    for m in _TOOLSPEC_RE.finditer(text):
        # crude balanced-paren scan from the ToolSpec( opening
        i = text.index("(", m.start())  # the actual constructor's open paren
        depth = 0
        end = len(text) - 1
        for j in range(i, len(text)):  # scan to the true matching paren, no cap
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        block = text[m.start():end + 1]
        if "description" not in block:
            lineno = text.count("\n", 0, m.start()) + 1
            hits.append((lineno, "ToolSpec without description= — tool descriptions "
                                 "inform agent routing", "§3 Robustness / §12 Tool-shape"))
    return sorted(set(hits))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="scan all of bin/ + m3_memory/")
    ap.add_argument("--range", dest="rng", default=None,
                    help="git range to scan (default origin/main..HEAD)")
    args = ap.parse_args(argv)

    files = [p for p in _changed_py_files(args.rng, args.all) if not _skip(p)]
    total = 0
    for path in files:
        hits = _scan_file(path)
        if not hits:
            continue
        rel = path.relative_to(_ROOT)
        for lineno, msg, tenet in hits:
            print(f"[philosophy] {rel}:{lineno} — {msg}  ({tenet})", file=sys.stderr)
            total += 1

    if total:
        print(f"[philosophy] {total} advisory warning(s) vs DESIGN_PHILOSOPHIES.md "
              f"— review, but NOT blocking. Legitimate exceptions are fine.",
              file=sys.stderr)
    else:
        print("[philosophy] no anti-patterns in changed files.")
    return 0  # ALWAYS advisory — never blocks


if __name__ == "__main__":
    raise SystemExit(main())
