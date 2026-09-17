"""Apple iWork chunker — .pages (Pages), .key (Keynote), .numbers (Numbers).

All three are the SAME container: a ZIP bundle holding an `Index/*.iwa`
payload (protobuf framed in Snappy) plus, usually, a rendered
`QuickLook/Preview.pdf`. One chunker therefore serves all three.

── TWO EXTRACTION PATHS, CHEAPEST FIRST ─────────────────────────────────────

1. QuickLook/Preview.pdf via pypdf — ALREADY a declared dependency, so this
   path costs nothing new. iWork apps write this preview on save by default,
   so it is present for the large majority of real documents.

2. Index/*.iwa via keynote-parser — always works, but pulls protobuf,
   python-snappy, pillow and ~20 transitive packages. Measured 2026-09-17:
   adding numbers-parser + keynote-parser took a 14-package environment to 36.

Path 2 is therefore OPTIONAL. Without it, an iWork file that happens to carry
no preview is reported and skipped rather than silently mangled; with it,
coverage is complete. That keeps the common case free and the cost opt-in,
rather than charging every m3 user ~20 packages for a format they may never
ingest.

⚠ The QuickLook PDF is a RENDERING. Text extracted from it is the laid-out
text, which is what a reader sees and what a searcher wants — but it is not the
document model, so it can differ in ordering for complex multi-column layouts
and will not carry spreadsheet formulas. Stated so nobody reads a .numbers
extraction as authoritative cell data.
"""
from __future__ import annotations

import logging
import zipfile
from typing import Iterator

from . import Leaf

logger = logging.getLogger("files_memory.chunkers.iwork")

CHUNKER_VERSION = "1.0.0"

# Path 1 — the cheap one. pypdf is already required for the pdf chunker.
try:
    import pypdf  # type: ignore

    _HAVE_PDF = True
except ImportError:  # pragma: no cover
    _HAVE_PDF = False

# Path 2 — optional, heavy.
try:
    from keynote_parser.codec import IWAFile  # type: ignore

    _HAVE_IWA = True
except ImportError:
    _HAVE_IWA = False

# The chunker is usable if EITHER path is available.
available = _HAVE_PDF or _HAVE_IWA

_PREVIEW_NAMES = (
    "QuickLook/Preview.pdf",
    "preview.pdf",
    "Preview.pdf",
)

#: Ignore IWA strings shorter than this. The archive is full of one- and
#: two-character style tokens and UUID fragments; they are not prose and they
#: pollute embeddings.
_MIN_IWA_STRING = 4


def chunk(path: str, text: str | None = None) -> Iterator[Leaf]:
    """Yield one Leaf per preview page, or one per IWA component."""
    if not available:
        logger.warning(
            "iwork chunker unavailable (needs pypdf, or keynote-parser for the "
            "full path); falling back for %s", path
        )
        return

    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            preview = next((n for n in _PREVIEW_NAMES if n in names), None)
            if preview and _HAVE_PDF:
                yield from _from_preview(zf, preview, path)
                return
            if _HAVE_IWA:
                yield from _from_iwa(zf, path)
                return
    except zipfile.BadZipFile:
        logger.warning(
            "cannot read %s. observed: not a valid ZIP container. "
            "possible: a pre-2013 iWork document (old binary format), or a "
            "corrupt file. inspect: re-save it from a current iWork app.", path
        )
        return
    except OSError as e:
        logger.warning("cannot read %s: %s", path, e)
        return

    logger.warning(
        "no text extracted from %s. observed: the bundle has no QuickLook "
        "preview and keynote-parser is not installed. "
        "possible: saved with previews disabled. "
        "inspect: install the iwork extra (keynote-parser) to read the Index/"
        "*.iwa payload directly, or re-save the file with previews enabled.",
        path,
    )


def _from_preview(zf: zipfile.ZipFile, preview: str, path: str) -> Iterator[Leaf]:
    """Extract per-page text from the bundled QuickLook PDF."""
    import io

    try:
        reader = pypdf.PdfReader(io.BytesIO(zf.read(preview)))  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.warning("preview PDF unreadable in %s: %s", path, e)
        return

    cursor = 0
    for idx, page in enumerate(reader.pages, start=1):
        try:
            body = (page.extract_text() or "").strip()
        except Exception as e:  # noqa: BLE001 — one bad page must not stop the rest
            logger.warning("page %d of %s failed to extract: %s", idx, path, e)
            continue
        if not body:
            continue
        start = cursor
        cursor += len(body)
        yield Leaf(
            text=body,
            division_type="page",
            division_id=str(idx),
            division_label=f"page {idx}",
            char_range_start=start,
            char_range_end=cursor,
            boundary_confidence=1.0,
            extra={"via": "quicklook-preview"},
        )


def _from_iwa(zf: zipfile.ZipFile, path: str) -> Iterator[Leaf]:
    """Extract text from the IWA payload (full fidelity, heavier deps)."""
    iwa_names = sorted(n for n in zf.namelist()
                       if n.startswith("Index/") and n.endswith(".iwa"))
    cursor = 0
    idx = 0
    for name in iwa_names:
        try:
            parsed = IWAFile.from_buffer(zf.read(name), name).to_dict()  # type: ignore
        except Exception as e:  # noqa: BLE001
            logger.debug("iwa component %s unreadable: %s", name, e)
            continue
        strings = _harvest_strings(parsed)
        if not strings:
            continue
        body = "\n".join(strings).strip()
        if not body:
            continue
        idx += 1
        start = cursor
        cursor += len(body)
        yield Leaf(
            text=body,
            division_type="component",
            division_id=str(idx),
            division_label=name.rsplit("/", 1)[-1],
            char_range_start=start,
            char_range_end=cursor,
            # Lower than the preview path: IWA harvesting recovers strings from
            # the object graph without reconstructing reading order, so the
            # boundary is real but the sequence within it is approximate.
            boundary_confidence=0.6,
            extra={"via": "iwa", "component": name},
        )


def _harvest_strings(node, out: "list[str] | None" = None) -> "list[str]":
    """Walk a decoded IWA dict and collect plausible prose strings."""
    if out is None:
        out = []
    if isinstance(node, dict):
        for key, value in node.items():
            # `.text` fields carry document prose in every iWork schema seen.
            if key in ("text", "string") and isinstance(value, str):
                if len(value) >= _MIN_IWA_STRING:
                    out.append(value)
            else:
                _harvest_strings(value, out)
    elif isinstance(node, list):
        for item in node:
            _harvest_strings(item, out)
    return out
