"""Nothing may depend on a CPython feature newer than the declared floor.

``pyproject.toml`` declares ``requires-python = ">=3.12"``, so 3.12 is a
SUPPORTED runtime, not an aspiration. The risk this guards is specific: a
construct newer than the floor works on every machine the developers use,
passes CI on the newest matrix cell, and fails only for the users running the
oldest version we promise to support.

That is not hypothetical. Found 2026-09-12, while the floor was still 3.11:
the last THREE pushes to main had failing py3.11 lanes on macOS and Ubuntu::

    AttributeError: type object 'StorageBackend' has no attribute
    '__protocol_attrs__'

``__protocol_attrs__`` is a CPython 3.12 addition, so that assertion could
never have run on the floor we promised. It went unnoticed because PRs ran only
3.12 -- the floor lane existed solely on main pushes, where nobody was looking.
The matrix now runs the FLOOR on every PR for that reason.

This guard is cheap and static: it scans for a small set of known post-floor
internals rather than emulating an older interpreter. It cannot catch
everything -- only a real floor lane does that -- but it catches the shape that
actually bit us, at no CI cost.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FLOOR = (3, 12)
_SCAN_DIRS = ("bin", "tests", "m3_memory")
_SKIP_PARTS = {"__pycache__", ".venv", "build", "node_modules", "benchmarks"}

# Attributes added AFTER the floor, with the version that introduced them.
# itertools.batched, typing.override and __protocol_attrs__ were 3.12 additions
# and are LEGAL now -- they belong here only if the floor is ever lowered.
_TOO_NEW = {
    "__static_attributes__": "3.13 (class internal)",
    "__firstlineno__": "3.13 (class internal)",
}

_TOO_NEW_CALLS = {
    r"\bwarnings\.deprecated\b": "3.13 — raise a DeprecationWarning directly",
}


def _py_files():
    for d in _SCAN_DIRS:
        base = _ROOT / d
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if set(p.parts) & _SKIP_PARTS:
                continue
            yield p


def _code_only(src: str) -> str:
    """Strip comments and string literals, keeping the rest of each line.

    A file that DOCUMENTS why it avoids a too-new attribute -- this one, and the
    test whose docstring explains the history -- must not be flagged for saying
    the word. That mention-vs-use distinction has produced repeated false
    positives in this repo; the tokenizer settles it, and blanking the SPAN
    rather than dropping the line keeps a real use on a line that also carries
    a string.
    """
    import io
    import tokenize

    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return src
    lines = src.splitlines()
    scrub: dict[int, str] = {}
    prose = {tokenize.COMMENT, tokenize.STRING}
    fstring_mid = getattr(tokenize, "FSTRING_MIDDLE", None)
    if fstring_mid is not None:
        prose.add(fstring_mid)
    for tok in toks:
        if tok.type not in prose:
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        for ln in range(srow, erow + 1):
            if ln - 1 >= len(lines):
                continue
            text = scrub.get(ln, lines[ln - 1])
            a = scol if ln == srow else 0
            b = ecol if ln == erow else len(text)
            scrub[ln] = text[:a] + " " * max(0, min(b, len(text)) - a) + text[b:]
    return "\n".join(scrub.get(i, ln) for i, ln in enumerate(lines, 1))


@pytest.mark.parametrize("attr,why", sorted(_TOO_NEW.items()))
def test_no_post_floor_dunder_attributes(attr, why):
    hits = []
    for p in _py_files():
        try:
            code = _code_only(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if attr in code:
            hits.append(str(p.relative_to(_ROOT)))
    floor = ".".join(str(x) for x in _FLOOR)
    assert not hits, f"{attr} is {why}, but the floor is {floor}. Used in: {hits}"


@pytest.mark.parametrize("pattern,why", sorted(_TOO_NEW_CALLS.items()))
def test_no_post_floor_stdlib_calls(pattern, why):
    rx = re.compile(pattern)
    hits = []
    for p in _py_files():
        try:
            code = _code_only(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if rx.search(code):
            hits.append(str(p.relative_to(_ROOT)))
    assert not hits, f"{pattern} is {why}. Used in: {hits}"


def test_the_guard_can_actually_fail():
    """§12c: plant a violation and watch it trip.

    Pins mention-vs-use in BOTH directions -- a real use must be caught, and the
    same token inside a comment or string must not be. A guard that only proves
    it stays quiet has not been shown to catch anything.
    """
    used = _code_only("x = Cls.__static_attributes__\n")
    assert "__static_attributes__" in used, "a real use is invisible to the scan"

    mentioned = _code_only('S = "we avoid __static_attributes__ here"\n')
    assert "__static_attributes__" not in mentioned, (
        "a mention inside a string is counted as a use"
    )

    commented = _code_only("# never touch __static_attributes__\n")
    assert "__static_attributes__" not in commented, "a comment is counted as a use"

    both = _code_only('LOG = "no __static_attributes__"\ny = Cls.__static_attributes__\n')
    assert both.count("__static_attributes__") == 1, (
        "a line carrying both a mention and a real use must count exactly one"
    )


def test_declared_floor_matches_what_this_guard_assumes():
    """If the floor moves, this guard must move with it.

    Otherwise it silently over-restricts (flagging constructs that are now
    legal) or under-restricts (missing ones that are not), and the next person
    deletes it rather than updating it.
    """
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'requires-python\s*=\s*"([^"]+)"', pyproject)
    assert m, "requires-python not found in pyproject.toml"
    floor = ".".join(str(x) for x in _FLOOR)
    assert f">={floor}" in m.group(1), (
        f"declared floor is {m.group(1)!r} but this guard assumes >={floor}; "
        f"update _FLOOR and _TOO_NEW together"
    )


def test_the_pr_matrix_gates_on_the_floor():
    """The root cause of the 2026-09-12 breakage.

    A version we PROMISE but never gate on will break silently -- it took three
    main pushes to notice. The PR cell must be the floor, not a convenient
    middle version.
    """
    ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    floor = ".".join(str(x) for x in _FLOOR)
    m = re.search(r'pull_request".*?python-version":\s*\[([^\]]+)\]', ci, re.S)
    assert m, "could not find the pull_request matrix cell in ci.yml"
    assert f'"{floor}"' in m.group(1), (
        f"the PR matrix runs {m.group(1)} but the declared floor is {floor}; "
        f"a floor that is never exercised on PRs breaks unnoticed"
    )


def test_every_source_file_parses_at_the_floor():
    """Syntax, not just attributes.

    ``ast.parse(..., feature_version=_FLOOR)`` rejects syntax newer than the
    floor -- a construct that parses fine on a newer runner and is a hard
    SyntaxError for a user on the oldest version we support.
    """
    bad = []
    for p in _py_files():
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            ast.parse(src, feature_version=_FLOOR)
        except SyntaxError as exc:
            bad.append(f"{p.relative_to(_ROOT)}: {exc.msg}")
    floor = ".".join(str(x) for x in _FLOOR)
    assert not bad, (
        f"files that do not parse on the {floor} floor:\n  " + "\n  ".join(bad)
    )
