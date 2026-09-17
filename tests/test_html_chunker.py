"""HTML chunker — heading sections in, markup out.

The chunker exists because `.html`/`.htm` resolved to filetype "html" with no
registered chunker, so every HTML file was split by the TEXT chunker and its
markup — tags, inline `<script>` bodies, `<style>` rules — entered FTS and the
embedding space as prose. These tests pin the two properties that matter:
the document splits on its own headings, and none of that markup survives.
"""
from __future__ import annotations

import os
import sys

import pytest

_BIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin"))
sys.path.insert(0, _BIN)

from files_memory.chunkers import html as html_chunker  # noqa: E402

pytestmark = pytest.mark.skipif(
    not html_chunker.available, reason="beautifulsoup4 not installed")


def _write(tmp_path, body: str, name: str = "doc.html") -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


_DOC = """<html><head><title>The Title</title>
<style>body { color: red; }</style>
<script>var secret = 'do-not-index';</script>
</head><body>
<h1>Introduction</h1>
<p>First paragraph of prose.</p>
<h2>Details</h2>
<p>Second section body.</p>
<script>var inline_leak = 2;</script>
<h2>Conclusion</h2>
<p>Final words.</p>
</body></html>"""


def test_splits_on_headings(tmp_path):
    leaves = list(html_chunker.chunk(_write(tmp_path, _DOC)))
    assert [lf.division_label for lf in leaves] == [
        "Introduction", "Details", "Conclusion"]
    assert [lf.division_id for lf in leaves] == ["1", "2", "3"]
    assert [lf.extra["level"] for lf in leaves] == [1, 2, 2]
    assert all(lf.division_type == "heading" for lf in leaves)


def test_section_text_follows_its_own_heading(tmp_path):
    leaves = list(html_chunker.chunk(_write(tmp_path, _DOC)))
    by_label = {lf.division_label: lf.text for lf in leaves}
    assert "First paragraph of prose." in by_label["Introduction"]
    assert "Second section body." in by_label["Details"]
    # A section must not absorb the NEXT section's body.
    assert "Second section body." not in by_label["Introduction"]
    assert "Final words." not in by_label["Details"]


def test_script_style_and_tags_never_reach_the_text(tmp_path):
    """The defect this chunker was written to fix."""
    joined = " ".join(
        lf.text for lf in html_chunker.chunk(_write(tmp_path, _DOC)))
    for forbidden in ("do-not-index", "inline_leak", "color: red",
                      "<p>", "<h1>", "<script", "<style"):
        assert forbidden not in joined, (
            f"{forbidden!r} leaked into extracted text — this is what the text "
            f"chunker used to do to every HTML file")


def test_document_without_headings_yields_one_leaf(tmp_path):
    path = _write(tmp_path, "<html><head><title>Flat</title></head>"
                            "<body><p>Just prose.</p></body></html>")
    leaves = list(html_chunker.chunk(path))
    assert len(leaves) == 1
    assert leaves[0].division_label == "Flat"       # falls back to <title>
    assert leaves[0].extra["headings"] == 0
    assert "Just prose." in leaves[0].text


def test_empty_document_yields_nothing(tmp_path):
    """No content is not an error, and must not emit an empty leaf."""
    assert list(html_chunker.chunk(_write(tmp_path, ""))) == []
    assert list(html_chunker.chunk(
        _write(tmp_path, "<html><body></body></html>", "b.html"))) == []


def test_malformed_markup_does_not_raise(tmp_path):
    """Real-world HTML is broken; an ingest must survive it.

    html.parser is deliberately lenient — an unclosed tag soup still yields
    text rather than killing the file's ingestion.
    """
    path = _write(tmp_path, "<h1>Broken<p>unclosed<div><span>nested")
    leaves = list(html_chunker.chunk(path))
    assert leaves, "malformed HTML produced no leaves"
    assert "unclosed" in " ".join(lf.text for lf in leaves)


def test_accepts_pre_read_text(tmp_path):
    """The dispatcher may pass text it already read; the path must not be re-read."""
    path = _write(tmp_path, "<h1>OnDisk</h1><p>disk body</p>")
    leaves = list(html_chunker.chunk(path, text="<h1>Passed</h1><p>passed body</p>"))
    assert [lf.division_label for lf in leaves] == ["Passed"]
    assert "passed body" in leaves[0].text
    assert "disk body" not in leaves[0].text


def test_char_ranges_are_monotonic(tmp_path):
    """Ranges must advance — downstream slices and dedupe rely on ordering."""
    leaves = list(html_chunker.chunk(_write(tmp_path, _DOC)))
    cursor = 0
    for lf in leaves:
        assert lf.char_range_start == cursor
        assert lf.char_range_end > lf.char_range_start
        cursor = lf.char_range_end
