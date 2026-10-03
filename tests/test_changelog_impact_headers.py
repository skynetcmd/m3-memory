"""Every changelog bullet above the marker carries an impact header.

A changelog's job is to let an operator answer "am I affected, and what do I
do" without reading a causal narrative. m3's public entries were prose-only, so
three changes in one release — one affecting all installs, one reachable only
with PostgreSQL AND a socket DSN, one with no user-visible effect at all — read
as three similar bullets.

Scope is deliberately limited to sections ABOVE the `impact-headers` marker.
History is NOT backfilled wholesale: writing a scope or data-status claim for an
old entry means asserting something nobody re-verified, and a confident header
that is wrong is worse than none — the same false-green failure this project
treats as cardinal, in the document people cite as evidence of its rigour.

The convention rots without enforcement, which is why this file exists rather
than a line in a style guide. Same reasoning as test_doc_fact_drift.py and
`sync_manifest_versions.py --check`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CHANGELOG = REPO / "CHANGELOG.md"

MARKER = "<!-- impact-headers:"
REQUIRED_FIELDS = ("**Affected:**", "**Action:**", "**Data status:**")

# "## [2026.10.1.1] — ..." or "## [Unreleased]"
_VERSION_HEADING = re.compile(r"^## \[([^\]]+)\]")


def _text() -> str:
    return CHANGELOG.read_text(encoding="utf-8")


def _above_marker(text: str) -> str:
    """The part of the changelog the convention applies to."""
    idx = text.find(MARKER)
    assert idx != -1, (
        f"the {MARKER!r} marker is missing from CHANGELOG.md. It defines where "
        "the impact-header convention starts; without it this test cannot tell "
        "new entries from history. Do not delete it to make this test pass."
    )
    return text[:idx]


def _bullets_by_section(text: str) -> list[tuple[str, str]]:
    """Return (version, bullet_block) for every top-level bullet above the marker.

    A bullet starts with "- " at column 0 and runs until the next such bullet,
    the next heading, or the end — so its continuation lines (which is where the
    header footer lives) stay attached.
    """
    out: list[tuple[str, str]] = []
    version: str | None = None
    current: list[str] | None = None

    def _flush() -> None:
        if version and current:
            out.append((version, "\n".join(current)))

    for line in text.splitlines():
        m = _VERSION_HEADING.match(line)
        if m:
            _flush()
            current = None
            version = m.group(1)
            continue
        if line.startswith("## "):          # a non-version H2 (e.g. policy notes)
            _flush()
            current = None
            version = None
            continue
        if line.startswith("- "):
            _flush()
            current = [line]
            continue
        if current is not None:
            current.append(line)
    _flush()
    return out


def test_the_marker_exists():
    _above_marker(_text())


def test_at_least_one_section_is_under_the_convention():
    """A marker pushed to the very top would vacuously satisfy every other test."""
    bullets = _bullets_by_section(_above_marker(_text()))
    assert bullets, (
        "no changelog bullets found above the impact-headers marker — either the "
        "marker drifted to the top of the file (making this suite vacuous) or the "
        "bullet parser no longer matches the file's shape"
    )


def test_every_bullet_above_the_marker_has_all_impact_fields():
    missing: list[str] = []
    for version, block in _bullets_by_section(_above_marker(_text())):
        absent = [f for f in REQUIRED_FIELDS if f not in block]
        if absent:
            first = block.splitlines()[0][:72]
            missing.append(f"[{version}] {first!r} is missing {absent}")
    assert not missing, (
        "changelog bullets without a complete impact header:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd a footer line to each, e.g.:\n"
        "  **Affected:** all installs · **Action:** upgrade · "
        "**Data status:** none"
    )


def test_data_status_is_never_left_blank():
    """`Data status:` with nothing after it is the failure this field exists to
    prevent — an operator cannot tell "verified none" from "nobody looked"."""
    blank: list[str] = []
    for version, block in _bullets_by_section(_above_marker(_text())):
        for m in re.finditer(r"\*\*Data status:\*\*(.*)", block):
            if not m.group(1).strip(" ·\t"):
                blank.append(f"[{version}] {block.splitlines()[0][:72]!r}")
    assert not blank, "empty `Data status:` in:\n  " + "\n  ".join(blank)


# The backfill stops at 2026-09-01 by decision: entries older than that would
# need scope and data-impact claims nobody has re-verified, and a confident
# header that is wrong is worse than none.
PRE_CONVENTION_SECTION = "## [2026.8.30.1]"


def test_history_below_the_marker_is_not_required_to_comply():
    """Pins the scoping decision itself. Pre-September sections have no headers
    and that must stay a PASSING state, or the next person 'fixes' the gap by
    inventing claims about entries nobody re-verified."""
    text = _text()
    below = text[text.find(MARKER) :]
    assert PRE_CONVENTION_SECTION in below, (
        f"{PRE_CONVENTION_SECTION} is expected to sit BELOW the marker as "
        "pre-convention history; if the marker moved past it, those entries "
        "now need real verified headers rather than invented ones"
    )
    # It genuinely has none — that is the point.
    section = below.split(PRE_CONVENTION_SECTION, 1)[1].split("\n## [", 1)[0]
    assert "**Affected:**" not in section


CONVENTION_DOC = REPO / ".claude/rules/changelog-impact-headers.md"


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_the_convention_doc_names_each_required_field(field):
    """The enforced fields and the documented fields must not drift apart.

    The convention is documented for contributors, NOT in the public changelog:
    a release note about the changelog's own format is noise to a reader
    deciding whether to upgrade.
    """
    assert CONVENTION_DOC.is_file(), f"{CONVENTION_DOC} is missing"
    text = CONVENTION_DOC.read_text(encoding="utf-8")
    assert field in text, (
        f"{field} is enforced by this test but not documented in "
        f"{CONVENTION_DOC.name}"
    )
