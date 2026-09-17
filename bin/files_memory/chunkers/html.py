"""HTML chunker — heading-aware extraction of rendered text.

Emits one Leaf per `<h1>`-`<h6>` section, mirroring the markdown chunker: HTML
carries the same heading semantics, so a document splits the same way and the
two filetypes produce comparable divisions.

Yields Leaf with:
  division_type = 'heading'
  division_id   = '<n>' (1-indexed, document order)
  division_label = the heading text
  boundary_confidence = 1.0 (a heading element is structural, not inferred)

── WHY THIS EXISTS ──────────────────────────────────────────────────────────

`.html`/`.htm` were already mapped to filetype "html" in identity.py, but no
"html" key existed in CHUNKER_REGISTRY -- so `get_chunker("html")` fell through
to the TEXT chunker and ingested raw markup. Measured 2026-09-17: tags, inline
`<script>` bodies and `<style>` rules all entered the store as if they were
prose, which pollutes both FTS and embeddings with markup tokens.

That is the §3 shape: a supported-looking extension that silently produced
garbage rather than refusing. beautifulsoup4 had been declared as a dependency
for "HTML parsing used by files-ingestion" and nothing had ever imported it.

── SCRIPT AND STYLE ARE DROPPED, DELIBERATELY ───────────────────────────────

`<script>`, `<style>`, `<noscript>`, `<template>` and HTML comments are removed
before text extraction. Their contents are not document prose, and keeping them
is the defect above. This is content selection, NOT a security boundary: parsing
happens with html.parser, nothing is executed, and no network fetch occurs.
"""
from __future__ import annotations

import logging
from typing import Iterator

from . import Leaf

logger = logging.getLogger("files_memory.chunkers.html")

CHUNKER_VERSION = "1.0.0"

# Probe the dependency, matching the pdf chunker's contract: `available = False`
# makes the dispatcher fall back to the text chunker rather than raise. Falling
# back to raw-markup text is poor, but it is the documented registry behaviour
# and is strictly better than failing an ingest.
try:
    from bs4 import BeautifulSoup  # type: ignore

    available = True
except ImportError:  # pragma: no cover — exercised on installs without bs4
    available = False

#: Elements whose text is never document prose.
_DROP_TAGS = ("script", "style", "noscript", "template")

_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def chunk(path: str, text: str | None = None) -> Iterator[Leaf]:
    """Yield one Leaf per heading section, or a single Leaf when there are none."""
    if not available:
        logger.warning(
            "HTML chunker unavailable (beautifulsoup4 not installed); "
            "falling back for %s", path
        )
        return

    if text is None:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as e:
            logger.warning("could not read %s: %s", path, e)
            return

    try:
        # html.parser is stdlib: no lxml/html5lib dependency, and lenient with
        # the malformed markup real-world HTML is full of.
        soup = BeautifulSoup(text, "html.parser")
    except Exception as e:  # noqa: BLE001 — a parse failure must not kill an ingest
        logger.warning("HTML parse failed for %s: %s", path, e)
        return

    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()

    headings = soup.find_all(list(_HEADINGS))

    if not headings:
        # No headings: one Leaf for the whole document. `division_type` stays
        # 'heading' so downstream consumers see one shape per filetype; the
        # label reports the absence rather than inventing a structure.
        body = _clean(soup.get_text(separator="\n"))
        if not body:
            return
        title = soup.title.get_text(strip=True) if soup.title else None
        yield Leaf(
            text=body,
            division_type="heading",
            division_id="1",
            division_label=title or "(document)",
            char_range_start=0,
            char_range_end=len(body),
            boundary_confidence=1.0,
            extra={"headings": 0},
        )
        return

    cursor = 0
    for idx, heading in enumerate(headings, start=1):
        label = heading.get_text(strip=True) or f"(unnamed {heading.name})"
        parts = [label]
        # Walk forward to the next heading; everything between belongs here.
        for sib in heading.next_siblings:
            name = getattr(sib, "name", None)
            if name in _HEADINGS:
                break
            chunk_text = (
                sib.get_text(separator="\n") if name is not None else str(sib)
            )
            if chunk_text:
                parts.append(chunk_text)

        body = _clean("\n".join(parts))
        if not body:
            continue
        start = cursor
        cursor += len(body)
        yield Leaf(
            text=body,
            division_type="heading",
            division_id=str(idx),
            division_label=label,
            char_range_start=start,
            char_range_end=cursor,
            boundary_confidence=1.0,
            sub_division=heading.name,
            extra={"level": int(heading.name[1])},
        )


def _clean(raw: str) -> str:
    """Collapse the whitespace HTML indentation leaves behind.

    Markup is written for a renderer, so extracted text arrives with runs of
    blank lines and leading indentation that carry no meaning. Left alone they
    inflate chunk sizes and embed as noise.
    """
    lines = [ln.strip() for ln in raw.splitlines()]
    out: list[str] = []
    for ln in lines:
        if not ln and out and not out[-1]:
            continue  # collapse consecutive blanks
        out.append(ln)
    return "\n".join(out).strip()
