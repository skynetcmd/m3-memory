"""PDF chunker — page-aware extraction.

Emits one Leaf per page. Tries PyMuPDF (`fitz`) first (faster, better
layout handling); falls back to `pypdf` (pure-Python, no native deps).
If neither is installed, sets `available = False` so the dispatcher
falls back to the text chunker.

Yields Leaf with:
  division_type = 'page'
  division_id   = '<n>' (1-indexed)
  division_label = 'page N' or first heading found on the page
  boundary_confidence = 1.0 (PDF page boundaries are structural)

Phase-2 upgrade: per-table preservation, per-figure caption extraction,
PyMuPDF-only mode for layout-aware bbox extraction.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

from . import Leaf

logger = logging.getLogger("files_memory.chunkers.pdf")

CHUNKER_VERSION = "1.0.0"

# Probe dependencies. We set `available = False` if neither is present so
# the dispatcher falls back to the text chunker.
_BACKEND: str | None = None
try:
    import fitz  # type: ignore
    _BACKEND = "fitz"
except ImportError:
    try:
        import pypdf  # type: ignore
        _BACKEND = "pypdf"
    except ImportError:
        pass

available = _BACKEND is not None


def chunk(path: str, text: str | None = None) -> Iterator[Leaf]:
    """Yield one Leaf per PDF page."""
    if not available:
        logger.warning("PDF chunker has no backend installed; skipping %s", path)
        return

    if _BACKEND == "fitz":
        yield from _chunk_fitz(path)
    else:
        yield from _chunk_pypdf(path)


def _chunk_fitz(path: str) -> Iterator[Leaf]:
    """Extract pages via PyMuPDF (preferred)."""
    try:
        doc = fitz.open(path)  # type: ignore
    except Exception as e:
        logger.warning("fitz.open failed for %s: %s", path, e)
        return

    try:
        running_offset = 0
        for page_idx in range(doc.page_count):
            page = doc.load_page(page_idx)
            try:
                page_text = page.get_text("text") or ""
            except Exception as e:
                logger.warning("page %d text extraction failed in %s: %s", page_idx + 1, path, e)
                continue
            cleaned = page_text.strip()
            if not cleaned:
                running_offset += len(page_text)
                continue

            label = f"page {page_idx + 1}"
            # Heuristic: use the first non-empty line as the label if it's
            # short enough to look like a heading.
            first_line = cleaned.split("\n", 1)[0].strip()
            if 0 < len(first_line) <= 80:
                label = f"page {page_idx + 1}: {first_line}"

            yield Leaf(
                text=cleaned,
                division_type="page",
                division_id=str(page_idx + 1),
                division_label=label,
                char_range_start=running_offset,
                char_range_end=running_offset + len(page_text),
                boundary_confidence=1.0,
                extra={"backend": "fitz"},
            )
            running_offset += len(page_text)
    finally:
        doc.close()


def _chunk_pypdf(path: str) -> Iterator[Leaf]:
    """Extract pages via pypdf (fallback)."""
    try:
        reader = pypdf.PdfReader(path)  # type: ignore
    except Exception as e:
        logger.warning("pypdf.PdfReader failed for %s: %s", path, e)
        return

    running_offset = 0
    for page_idx, page in enumerate(reader.pages):
        try:
            page_text = page.extract_text() or ""
        except Exception as e:
            logger.warning("page %d extraction failed in %s: %s", page_idx + 1, path, e)
            continue
        cleaned = page_text.strip()
        if not cleaned:
            running_offset += len(page_text)
            continue

        label = f"page {page_idx + 1}"
        first_line = cleaned.split("\n", 1)[0].strip()
        if 0 < len(first_line) <= 80:
            label = f"page {page_idx + 1}: {first_line}"

        yield Leaf(
            text=cleaned,
            division_type="page",
            division_id=str(page_idx + 1),
            division_label=label,
            char_range_start=running_offset,
            char_range_end=running_offset + len(page_text),
            boundary_confidence=1.0,
            extra={"backend": "pypdf"},
        )
        running_offset += len(page_text)
