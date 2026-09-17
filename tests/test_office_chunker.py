"""Office chunker — binary documents in, heading-split leaves out.

Before this chunker existed, `.docx`/`.doc`/`.pptx`/`.xlsx`/`.rtf`/`.epub` all
resolved to a filetype with no registry entry and fell through to the TEXT
chunker — so a Word file's ZIP container bytes were ingested as prose. These
tests pin that binary documents now reach a real extractor and that their own
heading structure survives into the leaves.
"""
from __future__ import annotations

import os
import sys

import pytest

_BIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin"))
sys.path.insert(0, _BIN)

from files_memory import chunkers, identity  # noqa: E402
from files_memory.chunkers import office as office_chunker  # noqa: E402

pytestmark = pytest.mark.skipif(
    not office_chunker.available, reason="sharepoint-to-text not installed")

docx = pytest.importorskip("docx", reason="python-docx needed to BUILD fixtures")


def _docx(tmp_path, sections):
    """Build a .docx with (heading, body) pairs."""
    d = docx.Document()
    for heading, body in sections:
        d.add_heading(heading, level=1)
        d.add_paragraph(body)
    p = tmp_path / "doc.docx"
    d.save(str(p))
    return str(p)


def test_docx_splits_on_its_own_headings(tmp_path):
    path = _docx(tmp_path, [("Chapter One", "Body prose here."),
                            ("Chapter Two", "More prose.")])
    leaves = list(office_chunker.chunk(path))
    assert len(leaves) == 2
    assert [lf.division_label for lf in leaves] == ["Chapter One", "Chapter Two"]
    assert "Body prose here." in leaves[0].text
    assert "More prose." in leaves[1].text
    # Section 1 must not absorb section 2.
    assert "More prose." not in leaves[0].text


def test_container_bytes_never_reach_the_text(tmp_path):
    """The defect this chunker fixes: ZIP bytes ingested as prose."""
    path = _docx(tmp_path, [("Title", "Readable body.")])
    joined = " ".join(lf.text for lf in office_chunker.chunk(path))
    assert "Readable body." in joined
    # A .docx is a ZIP: these are the markers of raw-container ingestion.
    for marker in ("PK\x03\x04", "word/document.xml", "[Content_Types]"):
        assert marker not in joined, (
            f"{marker!r} leaked into extracted text — the file was read as a "
            f"container, not a document")


def test_leaves_are_tagged_with_their_source_format(tmp_path):
    """Downstream needs to know a leaf came via conversion, not native markdown."""
    path = _docx(tmp_path, [("H", "B")])
    leaves = list(office_chunker.chunk(path))
    assert leaves
    assert all(lf.extra.get("via") == "office" for lf in leaves)
    assert all(lf.extra.get("source_format") == "docx" for lf in leaves)


def test_unreadable_file_yields_nothing_and_does_not_raise(tmp_path):
    """One corrupt file must not abort an ingest run."""
    bad = tmp_path / "corrupt.docx"
    bad.write_bytes(b"this is definitely not a docx")
    assert list(office_chunker.chunk(str(bad))) == []


def test_missing_file_yields_nothing_and_does_not_raise(tmp_path):
    assert list(office_chunker.chunk(str(tmp_path / "nope.docx"))) == []


@pytest.mark.parametrize("ext,filetype", [
    (".docx", "docx"), (".doc", "doc"), (".pptx", "pptx"), (".ppt", "ppt"),
    (".xlsx", "xlsx"), (".xls", "xls"), (".rtf", "rtf"), (".epub", "epub"),
    (".odt", "odt"),
])
def test_every_office_format_routes_to_this_chunker(ext, filetype):
    """The routing half: extension -> filetype -> office chunker, not text."""
    assert identity.FILETYPE_BY_EXT.get(ext) == filetype
    mod = chunkers.get_chunker(filetype)
    assert mod.__name__.endswith(".office"), (
        f"{ext} resolved to {mod.__name__} — it would be ingested as raw bytes")
