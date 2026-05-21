"""Per-filetype chunker dispatcher.

Each chunker implements:

    def chunk(path: str, text: str | None = None) -> Iterator[Leaf]

where `text` may be passed pre-loaded (for filetypes where the file
contents are read once by the caller for hashing + doc_id detection).
For binary filetypes (PDF), the chunker reads the file itself.

The dispatcher resolves filetype → chunker module → chunk() function. If
a chunker's optional dependency is missing (PyMuPDF/pypdf for pdf), the
dispatcher logs a warning and falls back. If no chunker can handle a
filetype, returns the `text` chunker (semantic-paragraph fallback).

Each module defines a CHUNKER_VERSION constant that contributes to the
ingestion record's chunker_version. Bumping a chunker's version forces
re-ingest of files under staleness review.

Public API:
    Leaf                — dataclass yielded by chunkers
    get_chunker(ft)     — returns the chunker callable for filetype `ft`
    chunk_file(...)     — convenience wrapper that dispatches + reads
    CHUNKER_REGISTRY    — dict[filetype → module]
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterator, Protocol

logger = logging.getLogger("files_memory.chunkers")


@dataclass
class Leaf:
    """One chunk yielded by a chunker.

    Fields map 1:1 onto columns in the `leaves` table (plus a few
    chunker-local fields that the ingester translates).
    """
    text: str
    division_type: str        # 'heading'|'page'|'window'|'slide'|'function'|...
    division_id: str          # stable within (file_node, division_type)
    division_label: str | None = None
    char_range_start: int = 0
    char_range_end: int = 0
    boundary_confidence: float = 1.0
    truncated: bool = False
    sub_division: str | None = None    # e.g. sub-heading; metadata-only
    extra: dict = field(default_factory=dict)


class ChunkerProtocol(Protocol):
    CHUNKER_VERSION: str
    def chunk(self, path: str, text: str | None = ...) -> Iterator[Leaf]: ...


# ──────────────────────────────────────────────────────────────────────────────
# Registry — populated by submodule imports below
# ──────────────────────────────────────────────────────────────────────────────
from . import markdown as _markdown_chunker  # noqa: E402
from . import pdf as _pdf_chunker  # noqa: E402
from . import text as _text_chunker  # noqa: E402

CHUNKER_REGISTRY: dict[str, object] = {
    "markdown": _markdown_chunker,
    "rst": _markdown_chunker,  # close-enough; heading-tree split works
    "pdf": _pdf_chunker,
    "text": _text_chunker,
    "log": _text_chunker,
    "unknown": _text_chunker,  # last-resort fallback
}


def get_chunker(filetype: str):
    """Return the chunker module for `filetype`, or the text fallback.

    If the registered chunker's dependencies are missing (sets
    .available = False during import), falls back to text.
    """
    mod = CHUNKER_REGISTRY.get(filetype)
    if mod is None:
        return _text_chunker
    if not getattr(mod, "available", True):
        logger.warning(
            "chunker for %s unavailable (deps missing); falling back to text",
            filetype,
        )
        return _text_chunker
    return mod


def chunker_version(filetype: str) -> str:
    """Return the version string of the chunker that would handle `filetype`."""
    mod = get_chunker(filetype)
    return getattr(mod, "CHUNKER_VERSION", "unknown")


def chunk_file(path: str, filetype: str, text: str | None = None) -> Iterator[Leaf]:
    """Convenience: dispatch + chunk in one call."""
    mod = get_chunker(filetype)
    yield from mod.chunk(path, text=text)
