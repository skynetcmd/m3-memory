"""Guard: backend-varying SQL idioms must live behind the seam, not at call sites.

DESIGN_PHILOSOPHIES §10a — *duplicated predicate logic is the defect,
independent of correctness. Copies drift.* Two incidents in this repo:

  - `memory/search.py` carried three hand-maintained copies of the row-scoping
    predicates; a 2026-07-14 tenancy fix reached all three but `type_filter` /
    `agent_filter` reached only one, so a filtered search silently returned
    UNFILTERED rows on both backends at score 1.0.
  - `chatlog_core.py` carried four copies of the since/until date-bound pair.
    All four shared the same bug: a bare `YYYY-MM-DD` compared against a
    timestamp column excluded the entire requested day on SQLite and kept only
    the midnight row on PostgreSQL (fixed 2026-09-07, commit c82ca545).

Converting the existing copies does not stop the NEXT author writing copy N+1.
This test does. It fails on any raw dialect-specific idiom in feature code,
naming the seam method to use instead.

Adding a legitimate exception: extend `_ALLOWED` with a one-line reason. That
is deliberately a visible, reviewable edit rather than a silent new copy.
"""
from __future__ import annotations

import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BIN = _ROOT / "bin"

# Files that legitimately contain the raw idiom.
_ALLOWED = {
    # The seam itself defines these fragments.
    "memory/backends/dialect.py",
    "memory/backends/sqlite_backend.py",
    "memory/backends/postgres_backend.py",
    # Import shims: modules that open SQLite by path (Finding L) carry a
    # pure-Python fallback definition for standalone execution. The shim's
    # fallback body necessarily spells the idiom out once.
    "embed_backfill.py",
    "memory/doctor.py",
    "chatlog_status.py",
    "backfill_content_hash.py",
    "chatlog_strip_framing_backfill.py",
    "m3_chatlog_enrich_backfill.py",
    # Session-start hook: routes through the seam, but keeps a SQLite fallback
    # inline because it must NEVER break session start on a seam import error.
    # It only ever opens a SQLite file by path.
    "hooks/chatlog/session_start_capture_check.py",
    # DDL only: `DEFAULT (datetime('now'))` column defaults. The files schema
    # module emits its own per-backend DDL; a column default in CREATE TABLE is
    # not a query-path portability leak.
    "files_memory/schema.py",
}

_RULES = [
    (
        "byte_length",
        re.compile(r"LENGTH\s*\(\s*CAST\s*\(.*?AS\s+BLOB\s*\)\s*\)", re.I),
        "Dialect.byte_length(column) — PG needs octet_length(); PG's length() "
        "counts CHARACTERS and would be ~3x wrong on CJK.",
    ),
    (
        # The byte_length rule above catches the SQLite-correct spelling. This
        # one catches the spelling that is silently WRONG on PostgreSQL: a bare
        # LENGTH() on a text column. Added 2026-09-08 after a new doctor probe
        # shipped LENGTH(COALESCE(mi.content,'')) and the guard PASSED it --
        # the pattern only knew the CAST form, i.e. it policed the fixed shape
        # and not the broken one.
        #
        # Narrowed to `content`: size caps and window decisions are byte-based
        # there, so a character count silently changes behaviour per encoding.
        # Flagging every LENGTH() in the tree would be noise, and noise trains
        # readers to ignore the rule (§3: a false alarm is a violation too).
        "bare_length_on_content",
        # (?<!byte_) so the CORRECT call -- dialect().byte_length("...content...")
        # -- is not flagged. A rule that fires on the fixed form is a false
        # alarm, which §3 counts as a violation in its own right; the first
        # draft of this pattern did exactly that.
        re.compile(r"(?<!byte_)LENGTH\s*\(\s*(?!CAST|TRIM)[^)]*content[^)]*\)", re.I),
        "Dialect.byte_length(column) — a bare LENGTH() on content counts "
        "CHARACTERS on PostgreSQL, not bytes (~3x under-count on CJK).",
    ),
    (
        "has_content",
        re.compile(r"LENGTH\s*\(\s*TRIM\s*\(\s*COALESCE\s*\(", re.I),
        "Dialect.has_content(column)",
    ),
    (
        "date_bound",
        # Narrow to the ACTUAL defect: a USER-SUPPLIED `since`/`until` bound
        # dropped straight into a timestamp comparison. A machine-generated
        # watermark (pg_sync) is always a full ISO timestamp, so
        # normalize_date_bound would pass it through unchanged -- flagging it
        # would be noise that trains readers to ignore this test.
        re.compile(
            r"(?:since|until)\b[^\n]*?\b\w*(?:created_at|updated_at)\s*[<>]=?"
            r"|\b\w*(?:created_at|updated_at)\s*[<>]=?[^\n]*?\b(?:since|until)\b",
            re.I,
        ),
        "Dialect.date_bound(column, side) + normalize_date_bound(value, side) — "
        "a bare YYYY-MM-DD vs a timestamp column is wrong on BOTH backends, in "
        "two different ways.",
    ),
    (
        "now_minus_days",
        # QUERY predicates only. `DEFAULT (datetime('now'))` in DDL is a
        # different construct -- a column default, emitted per-backend by the
        # schema module, not a portability leak in a query path.
        re.compile(r"^(?![^\n]*\bDEFAULT\b)[^\n]*datetime\s*\(\s*['\"]now['\"]", re.I),
        "Dialect.now_minus_days() / now_minus_minutes() / age_days_gt() — "
        "datetime('now', ?) is SQLite-only and raises on PostgreSQL.",
    ),
]


def _prose_lines(src: str) -> set[int]:
    """Line numbers occupied by DOCSTRINGS only.

    Deliberately NOT "all string literals". The SQL this guard polices lives
    inside triple-quoted f-strings, so skipping every string would blind the
    rule completely — the opposite mistake, and a far worse one.

    A docstring is a bare string expression standing alone as a statement
    (module/class/function docs, or a free-floating comment-block string). An
    f-string is never a docstring, so `sql = f\"\"\"...\"\"\"` is still scanned.
    """
    import ast

    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()

    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        val = node.value
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            end = getattr(val, "end_lineno", val.lineno)
            out.update(range(val.lineno, end + 1))
    return out


def _python_sources():
    for path in sorted(_BIN.rglob("*.py")):
        rel = path.relative_to(_BIN).as_posix()
        if rel in _ALLOWED or "__pycache__" in rel:
            continue
        yield rel, path


class TestSqlDialectDrift(unittest.TestCase):
    def test_no_raw_dialect_idioms_in_feature_code(self):
        violations: list[str] = []
        for rel, path in _python_sources():
            text = path.read_text(encoding="utf-8", errors="replace")
            prose = _prose_lines(text)
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue  # a comment explaining the idiom is not a use
                if lineno in prose:
                    # A DOCSTRING describing the predicate is not a use of it.
                    # m3_entities_gliner.py documents its filter as
                    # "length(mi.content) > min_content_len" — flagging that
                    # would penalise a module for explaining itself, and a rule
                    # that fires on correct code trains readers to ignore it
                    # (§3). Same trap the raw-connection guard hit.
                    continue
                # Trailing comment: strip it. A line like
                #   _since = _d.now_minus_days(_p)  # e.g. "datetime('now',...)"
                # is the CORRECT seam call annotated with the SQL it renders --
                # flagging it would penalise the very fix this test enforces.
                code = stripped.split("#", 1)[0] if "#" in stripped else stripped
                if not code.strip():
                    continue
                for name, pattern, fix in _RULES:
                    if pattern.search(code):
                        violations.append(
                            f"{rel}:{lineno} raw '{name}' idiom\n"
                            f"      {stripped[:100]}\n"
                            f"      -> use {fix}"
                        )
        self.assertEqual(
            violations, [],
            "Backend-specific SQL found outside the seam (§10a). Route it "
            "through memory/backends/dialect.py, or add a reasoned entry to "
            "_ALLOWED in this test:\n  " + "\n  ".join(violations),
        )

    def test_allowlist_entries_all_exist(self):
        """A stale allowlist entry silently un-guards a file that was renamed."""
        missing = [r for r in _ALLOWED if not (_BIN / r).exists()]
        self.assertEqual(missing, [], f"_ALLOWED names non-existent files: {missing}")

    def test_the_guard_actually_matches(self):
        """Self-check: the patterns must fire on the known-bad forms, or this
        test is a no-op that passes forever."""
        cases = [
            ("byte_length", "where.append(\"LENGTH(CAST(mi.content AS BLOB)) <= ?\")"),
            # The real 2026-09-08 miss: this exact spelling shipped in a doctor
            # probe and the guard passed it.
            ("bare_length_on_content",
             "SELECT mi.id, LENGTH(COALESCE(mi.content,'')) FROM memory_items mi"),
            ("has_content", "\"LENGTH(TRIM(COALESCE(mi.content, ''))) > 0\","),
            ("date_bound", 'if until: clauses.append(f"mi.created_at<={_p}")'),
            ("now_minus_days", "where.append(\"mi.created_at < datetime('now', ?)\")"),
        ]
        by_name = {n: p for n, p, _ in _RULES}
        for name, sample in cases:
            self.assertRegex(sample, by_name[name], f"{name} pattern went blind")


if __name__ == "__main__":
    unittest.main()
