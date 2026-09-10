"""Gate the documented facts that silently rot when code changes.

Every claim here was found WRONG in the docs at least once (2026-09-10 doc V&V):
type/relationship catalogues drifted by 2 and 4 entries, four numeric defaults
were stale, and the test count was quoted as three different numbers on five
pages. None of it broke a test, because prose is not executed.

The rule these tests encode: a documented *number or enumeration* that has a
single source of truth in code must be derived from that source, not retyped.
Where a doc restates one, this file asserts the two still agree.

Deliberately NOT gated: prose descriptions, and counts whose source of truth is
the filesystem in a way that churns per-commit (see the band in
``test_test_count_claims_are_in_band``).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- enumerations


def test_memory_type_count_matches_catalog() -> None:
    """Docs quoting "N canonical" types must match VALID_MEMORY_TYPES.

    Drifted to 34 vs 36 (missing ``procedure`` and ``synthesis``) because two
    types were added to the frozenset without touching the prose table.
    """
    from catalog.spec import VALID_MEMORY_TYPES

    n = len(VALID_MEMORY_TYPES)
    text = _read("docs/AGENT_INSTRUCTIONS.md")
    claims = re.findall(r"\((\d+) canonical", text)
    assert claims, "AGENT_INSTRUCTIONS.md no longer states a canonical type count"
    for c in claims:
        assert int(c) == n, f"doc says {c} canonical memory types, code has {n}"

    # The enumeration itself, not just the count: every type must be listed.
    for t in VALID_MEMORY_TYPES:
        assert f"`{t}`" in text, f"memory type {t!r} is missing from the doc table"


def test_relationship_type_claims_match_code() -> None:
    """Docs quoting "N relationship types" must match VALID_RELATIONSHIP_TYPES.

    CORE_FEATURES said 9 and AGENT_INSTRUCTIONS listed 7; the code has 11. The
    four chat/handoff edges (``precedes``/``follows``/``message``/``handoff``)
    were invisible in the docs, so the graph looked smaller than it is.
    """
    from memory_core import VALID_RELATIONSHIP_TYPES

    n = len(VALID_RELATIONSHIP_TYPES)
    for rel in ("docs/CORE_FEATURES.md", "docs/AGENT_INSTRUCTIONS.md"):
        text = _read(rel)
        for c in re.findall(r"(?:supports |All )(\d+) (?:relationship |)types", text):
            assert int(c) == n, f"{rel} says {c} relationship types, code has {n}"
        for t in VALID_RELATIONSHIP_TYPES:
            assert f"`{t}`" in text, f"{rel}: relationship type {t!r} not listed"


# ------------------------------------------------------------ numeric defaults

# (env var, docs that quote its default). The value is read from config at run
# time -- never hardcoded here, or this test would be one more place to drift.
_GATED_DEFAULTS = {
    "CONTRADICTION_THRESHOLD": ("docs/ARCHITECTURE.md", "docs/AGENT_INSTRUCTIONS.md"),
    "FACT_ENRICH_MAX_ATTEMPTS": ("docs/ARCHITECTURE.md",),
    "SEARCH_ROW_CAP": ("docs/TECHNICAL_DETAILS.md",),
}


@pytest.mark.parametrize("name,docs", sorted(_GATED_DEFAULTS.items()))
def test_documented_default_matches_config(name: str, docs: tuple[str, ...]) -> None:
    """A default quoted in prose must equal ``memory.config``'s value.

    All three of these were wrong: cosine 0.85 (really 0.92), max-attempts 5
    (really 3), row cap 500 (really 5000 -- a 10x error that would mislead
    anyone sizing a search).

    The value is matched only in a window around the var's own name. A bare
    file-wide substring search is not a gate: when this test first ran,
    restoring the "default 5" defect did not fail it, because some unrelated
    "3" elsewhere in the file satisfied the search. The window is what makes
    the assertion about *this* claim.
    """
    from memory import config

    value = getattr(config, name)
    rendered = f"{value:g}" if isinstance(value, float) else str(value)
    # Accept a thousands separator, as the docs use one for large ints.
    variants = {rendered, f"{value:,}"} if isinstance(value, int) else {rendered}

    for rel in docs:
        text = _read(rel)
        # Look BOTH ways: prose puts the value before the var name as often as
        # after it ("cosine >= 0.92, `M3_CONTRADICTION_THRESHOLD`"), and a
        # forward-only window produced a false failure on exactly that phrasing.
        windows = [
            text[max(0, m.start() - 200) : m.end() + 200]
            for m in re.finditer(rf"\b(?:M3_)?{re.escape(name)}\b", text)
        ]
        # Table rows name the var in one cell and the default in the next, so
        # also allow a row that starts with the var name.
        windows += [
            line
            for line in text.splitlines()
            if line.lstrip().startswith("|") and name in line
        ]
        assert windows, f"{rel} no longer mentions {name}"
        # Match the value as a NUMBER, not a substring. A bare `"3" in window`
        # is satisfied by the 3 inside "M3_FACT_ENRICH_MAX_ATTEMPTS" itself, so
        # a single-digit default can never be gated that way -- restoring the
        # wrong "default 5" passed until this became a word-boundary match.
        num = re.compile(r"(?<![\w.,])(?:" + "|".join(re.escape(v) for v in variants) + r")(?![\w.])")
        assert any(num.search(w) for w in windows), (
            f"{rel} quotes a stale default for {name}: the real value is "
            f"{rendered}, which appears nowhere near the variable's own name"
        )


def test_contradiction_title_gate_is_not_documented_as_strict() -> None:
    """The title gate defaults to ``loose``; docs claimed a title must match.

    This one is worse than a wrong number: "same-type, same-title" told readers
    contradictions are only caught between identically titled memories, which
    understates the feature and invites duplicate facts.
    """
    from memory import config

    assert config.CONTRADICTION_TITLE_GATE == "loose", (
        "default title gate changed -- revisit the prose in ARCHITECTURE.md and "
        "AGENT_INSTRUCTIONS.md, which now document 'loose'"
    )
    for rel in ("docs/ARCHITECTURE.md", "docs/AGENT_INSTRUCTIONS.md"):
        assert "same-type, same-title memory exists" not in _read(rel), (
            f"{rel} reintroduced the 'same-title' claim, which is false under "
            "the default loose title gate"
        )


# ------------------------------------------------------------------ test count


def _collected_test_count() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--collect-only"],
        cwd=REPO,
        capture_output=True,
        text=True,
    ).stdout
    m = re.search(r"(\d+) tests? collected", out)
    if not m:  # pragma: no cover - collection itself is broken; other tests say so
        pytest.skip("could not parse pytest collection output")
    return int(m.group(1))


# Wide enough that ordinary commits don't fail the build, narrow enough that a
# number quoted from a different era does. The docs said 2,400 / 2,501 / 4,048
# simultaneously; a 500-test band catches that without churning.
_TEST_COUNT_BAND = 500

# There are TWO honest suite sizes and conflating them is what made this a mess:
# ~2,785 `def test_` FUNCTIONS in source, and ~4,048 COLLECTED cases once
# parametrisation expands them. README's floor is checked against the function
# count by test_tool_count_drift.test_readme_test_count_floor_is_honest -- that
# test owns README, so this one must accept either measure or the two gates
# would demand different numbers on the same page.


@pytest.mark.slow
def test_test_count_claims_are_in_band() -> None:
    """Every doc quoting a suite size must be near one of the two real measures.

    Accepts either the collected-case count or the source-function count: a page
    may legitimately quote either, as long as it is not quoting a number from a
    previous era (2,400 and 2,501 were both still in the tree, alongside 4,048).
    """
    collected = _collected_test_count()
    functions = len(
        re.findall(
            r"^\s*(?:async )?def test_",
            "\n".join(
                p.read_text(encoding="utf-8", errors="ignore")
                for p in sorted((REPO / "tests").glob("test_*.py"))
            ),
            re.M,
        )
    )
    pat = re.compile(r"([\d,]{3,})\+?\s*(?:collected )?tests?\b")
    problems = []
    for path in [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]:
        for raw in pat.findall(path.read_text(encoding="utf-8")):
            claimed = int(raw.replace(",", ""))
            if claimed < 100:  # "41 end-to-end tests" and friends: not the suite
                continue
            if min(abs(claimed - collected), abs(claimed - functions)) > _TEST_COUNT_BAND:
                problems.append(
                    f"{path.name}: claims {claimed:,}; real counts are "
                    f"{functions:,} test functions / {collected:,} collected"
                )
    assert not problems, "stale suite-size claims:\n  " + "\n  ".join(problems)


# ------------------------------------------------------- native-core wheel tag


def _rust_core_pins() -> tuple[str, str]:
    """(tag, version) from the installer -- the single source of truth."""
    src = (REPO / "m3_memory" / "rust_core_install.py").read_text(encoding="utf-8")
    tag = re.search(r'M3_CORE_RS_GIT_TAG\s*=\s*"([^"]+)"', src)
    ver = re.search(r'M3_CORE_RS_VERSION\s*=\s*"([^"]+)"', src)
    assert tag and ver, "rust_core_install.py no longer pins a tag/version"
    return tag.group(1), ver.group(1)


def test_no_doc_pins_a_stale_rust_core_tag() -> None:
    """A quoted `m3-core-rs` tag must be the one the installer actually fetches.

    pyproject carried `v2026.7.29` while the installer had moved to `v2026.9.7`,
    so the documented `pip install ...@<tag>` line fetched a superseded core.
    Nothing caught it: the tag lives in a comment, and comments aren't executed.
    """
    tag, _ = _rust_core_pins()
    # Only tags presented as SOMETHING TO INSTALL. A tag inside a historical
    # heading is correct as written -- ROADMAP.md's "m3-core-rs 3.6.6 wheels
    # (v2026.06.07)" records a past milestone, and flagging it was a false
    # positive on this test's first run.
    pat = re.compile(
        r"(?:pip install|gh release download"
        r"|git\+https://github\.com/skynetcmd/m3-core-rs"
        r"|Releases? for tag|Releases \(tag)"
        r"[^\n]{0,160}?(v20\d\d\.\d+\.\d+)"
    )
    stale = []
    for path in [
        REPO / "pyproject.toml",
        REPO / "README.md",
        REPO / "INSTALL.md",
        *sorted((REPO / "docs").glob("*.md")),
    ]:
        if path.name == "CHANGELOG.md":
            continue  # history: old tags are correct there
        for found in pat.findall(path.read_text(encoding="utf-8")):
            if found != tag:
                stale.append(f"{path.name}: pins {found}, installer uses {tag}")
    assert not stale, "stale m3-core-rs tag(s):\n  " + "\n  ".join(stale)


def test_docs_do_not_call_pypi_the_official_wheel_channel() -> None:
    """The GitHub Release is the official channel; PyPI is a partial mirror.

    It cannot host the CUDA wheels at all (they exceed PyPI's 100 MB per-file
    limit by an order of magnitude), and the mirror can lag the Release. So
    `rust_core_install.py` cascades Release -> PyPI -> source, and any doc that
    tells a user to get the core "straight from PyPI" contradicts the resolver.
    """
    banned = (
        "pulls those\nstraight from PyPI",
        "straight from PyPI. Read on",
        "serves the smaller backends from PyPI",
    )
    problems = []
    for path in sorted((REPO / "docs").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for phrase in banned:
            if phrase in text:
                problems.append(f"{path.name}: {phrase!r}")
    assert not problems, (
        "doc(s) present PyPI as the primary wheel channel:\n  "
        + "\n  ".join(problems)
    )
