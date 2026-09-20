"""iWork chunker — .pages / .key / .numbers via the bundled QuickLook preview.

All three Apple formats are one ZIP container holding an `Index/*.iwa` payload
and, normally, a rendered `QuickLook/Preview.pdf`. The chunker reads the preview
with pypdf (already a core dependency) and only falls back to IWA parsing when
the optional `[iwork]` extra is installed — so the common path costs nothing.

These tests build a real bundle rather than mocking the reader: the preview path
is the one every user hits, and mocking it would test the mock.
"""
from __future__ import annotations

import os
import sys
import zipfile

import pytest

_BIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin"))
sys.path.insert(0, _BIN)

from files_memory import chunkers, identity  # noqa: E402
from files_memory.chunkers import iwork as iwork_chunker  # noqa: E402

pytestmark = pytest.mark.skipif(
    not iwork_chunker.available, reason="neither pypdf nor keynote-parser installed")


def _preview_pdf(lines_per_page):
    """A minimal multi-page PDF carrying real, extractable text.

    Hand-built rather than via reportlab: an optional build-only dependency
    would make these tests SKIP on most machines, and a skipped test proves
    nothing. PDF is simple enough to emit directly, and pypdf (already a core
    dependency) reads the result.
    """
    import io

    objects: dict[int, str] = {}
    kids: list[int] = []
    num = 4  # 1=Catalog, 2=Pages, 3=Font
    for lines in lines_per_page:
        stream = "BT /F1 12 Tf 72 720 Td " + " ".join(
            f"({line}) Tj 0 -18 Td" for line in lines) + " ET"
        objects[num] = (
            f"<< /Length {len(stream)} >>" + "\n" +
            "stream" + "\n" + stream + "\n" + "endstream"
        )
        contents = num
        num += 1
        objects[num] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {contents} 0 R >>"
        )
        kids.append(num)
        num += 1

    objects[1] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[2] = ("<< /Type /Pages /Kids ["
                  + " ".join(f"{k} 0 R" for k in kids)
                  + f"] /Count {len(kids)} >>")
    objects[3] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    nl = chr(10)
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4" + nl.encode())
    offsets: dict[int, int] = {}
    for n in sorted(objects):
        offsets[n] = buf.tell()
        buf.write((f"{n} 0 obj" + nl + objects[n] + nl + "endobj" + nl).encode())
    xref = buf.tell()
    highest = max(objects)
    buf.write(("xref" + nl + f"0 {highest + 1}" + nl
               + "0000000000 65535 f " + nl).encode())
    for n in range(1, highest + 1):
        buf.write((f"{offsets.get(n, 0):010d} 00000 n " + nl).encode())
    buf.write((("trailer" + nl + f"<< /Size {highest + 1} /Root 1 0 R >>" + nl
                + "startxref" + nl + f"{xref}" + nl + "%%EOF" + nl)).encode())
    return buf.getvalue()

def _bundle(tmp_path, name="doc.pages", *, preview=None, extra=None):
    """Write an iWork-shaped ZIP bundle."""
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Index/Document.iwa", b"\x00binary-iwa-payload")
        if preview is not None:
            z.writestr("QuickLook/Preview.pdf", preview)
        for n, data in (extra or {}).items():
            z.writestr(n, data)
    return str(p)


def test_extracts_text_from_the_quicklook_preview(tmp_path):
    pdf = _preview_pdf([["Pages Document Title", "First page body."]])
    leaves = list(iwork_chunker.chunk(_bundle(tmp_path, preview=pdf)))
    assert leaves, "no leaves from a bundle that has a preview"
    joined = " ".join(lf.text for lf in leaves)
    assert "Pages Document Title" in joined
    assert "First page body." in joined
    assert all(lf.extra.get("via") == "quicklook-preview" for lf in leaves)


def test_one_leaf_per_preview_page(tmp_path):
    pdf = _preview_pdf([["Page one text."], ["Page two text."]])
    leaves = list(iwork_chunker.chunk(_bundle(tmp_path, preview=pdf)))
    assert len(leaves) == 2
    assert [lf.division_type for lf in leaves] == ["page", "page"]
    assert [lf.division_id for lf in leaves] == ["1", "2"]
    assert "Page one text." in leaves[0].text
    assert "Page two text." in leaves[1].text


def test_iwa_bytes_never_reach_the_text(tmp_path):
    """The bundle's binary payload must not be ingested as prose."""
    pdf = _preview_pdf([["Clean readable text."]])
    leaves = list(iwork_chunker.chunk(_bundle(tmp_path, preview=pdf)))
    joined = " ".join(lf.text for lf in leaves)
    assert "Clean readable text." in joined
    assert "binary-iwa-payload" not in joined


def test_bundle_without_preview_is_reported_not_mangled(tmp_path):
    """No preview and no [iwork] extra: emit NOTHING rather than garbage.

    Silently emitting the container bytes is exactly the failure this whole
    chunker set exists to remove.
    """
    path = _bundle(tmp_path, preview=None)
    leaves = list(iwork_chunker.chunk(path))
    joined = " ".join(lf.text for lf in leaves)
    assert "binary-iwa-payload" not in joined


def test_non_zip_file_does_not_raise(tmp_path):
    """Pre-2013 iWork files are not ZIPs; an ingest must survive them."""
    p = tmp_path / "ancient.pages"
    p.write_bytes(b"old binary iwork format, not a zip")
    assert list(iwork_chunker.chunk(str(p))) == []


def test_missing_file_does_not_raise(tmp_path):
    assert list(iwork_chunker.chunk(str(tmp_path / "nope.key"))) == []


def test_char_ranges_advance(tmp_path):
    pdf = _preview_pdf([["Alpha text here."], ["Beta text here."]])
    leaves = list(iwork_chunker.chunk(_bundle(tmp_path, preview=pdf)))
    cursor = 0
    for lf in leaves:
        assert lf.char_range_start == cursor
        assert lf.char_range_end > lf.char_range_start
        cursor = lf.char_range_end


@pytest.mark.parametrize("ext", [".pages", ".key", ".numbers"])
def test_all_three_apple_formats_route_here(ext):
    """Pages, Keynote and Numbers share one container and one chunker."""
    assert identity.FILETYPE_BY_EXT.get(ext) == "iwork"
    mod = chunkers.get_chunker("iwork")
    assert mod.__name__.endswith(".iwork"), (
        f"{ext} resolved to {mod.__name__} — it would be ingested as raw bytes")
