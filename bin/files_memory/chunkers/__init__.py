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
from dataclasses import dataclass, field, replace
from typing import Iterator, Protocol

logger = logging.getLogger("files_memory.chunkers")

# Floor for the token-forced leaf split: below this a piece is kept whole even
# if it still estimates over cap, so a degenerate input cannot recurse down to
# single characters.
_MIN_LEAF_SPLIT_CHARS = 256


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


def _enforce_leaf_token_cap(leaf: Leaf) -> "Iterator[Leaf]":
    """Split a leaf that exceeds FILES_MAX_LEAF_TOKENS, or yield it unchanged.

    `FILES_MAX_LEAF_TOKENS` (7000) was DECLARED in files_memory/config.py with a
    comment promising that oversize leaves "are truncated with a warning and
    `truncated=true` flag" -- and had ZERO references outside its own
    definition. Nothing truncated, nothing warned, and `Leaf.truncated` (a real
    column, written at ingest.py:420) was never set True by any code path, so
    `WHERE truncated=1` gave every operator a false clean bill of health.

    That mattered because the per-chunker caps are CHARACTER counts, and
    characters are not tokens. `markdown.py`'s MAX_SECTION_CHARS=16000 yields
    ~9,639 tokens of Chinese, ~9,816 of JSON and 16,000 of base64 -- all over
    bge-m3's 8,192 ceiling -- and Markdown is the format most likely to carry
    exactly that content (fenced code blocks, JSON examples, CJK docs).
    `text.py`'s MAX_CHARS=2400 is safe only by accident: 2400 characters cannot
    exceed 2400 tokens.

    Enforced HERE, at the single dispatch point, so every chunker (including
    ones added later) inherits it rather than each re-implementing the check --
    the same reasoning as the embed-path boundary guard.

    Splitting is preferred over truncation: truncation silently DISCARDS
    content, which is a worse failure than a slightly diluted vector.
    `truncated` is set only when a piece genuinely cannot be split further, so
    the column finally means what it says.
    """
    from files_memory.config import FILES_MAX_LEAF_TOKENS
    from memory.tokens import estimate_tokens

    if estimate_tokens(leaf.text) <= FILES_MAX_LEAF_TOKENS:
        yield leaf
        return

    # Halve until each piece fits. estimate_tokens (not count_tokens) on
    # purpose: this is a per-leaf hot path and the estimator is conservative --
    # it may over-split, never under-split.
    pieces: "list[str]" = []
    stack = [leaf.text]
    while stack:
        part = stack.pop()
        if estimate_tokens(part) <= FILES_MAX_LEAF_TOKENS or len(part) <= _MIN_LEAF_SPLIT_CHARS:
            pieces.append(part)
            continue
        mid = len(part) // 2
        stack.append(part[mid:])
        stack.append(part[:mid])
    pieces = [p for p in pieces if p]

    if len(pieces) <= 1:
        # Could not split (a single indivisible run). Flag it honestly rather
        # than pretending it was fine -- this is the ONLY case where
        # `truncated` is true, and it now carries real information.
        logger.warning(
            "files: leaf %r is %d est. tokens (cap %d) and could not be split; "
            "flagged truncated=True. review: M3_FILES_MAX_LEAF_TOKENS",
            leaf.division_id, estimate_tokens(leaf.text), FILES_MAX_LEAF_TOKENS,
        )
        leaf.truncated = True
        yield leaf
        return

    logger.info(
        "files: leaf %r exceeded the token cap (%d > %d); split into %d pieces "
        "so no content is lost.",
        leaf.division_id, estimate_tokens(leaf.text), FILES_MAX_LEAF_TOKENS, len(pieces),
    )
    offset = leaf.char_range_start
    for i, piece in enumerate(pieces):
        sub = replace(
            leaf,
            text=piece,
            # Keep division_id stable-but-distinct: the ingester treats it as
            # unique within (file_node, division_type).
            division_id=f"{leaf.division_id}#t{i}" if i else leaf.division_id,
            char_range_start=offset,
            char_range_end=offset + len(piece),
            # A token-forced split is not a structural boundary, so downstream
            # rankers should not treat it as one.
            boundary_confidence=min(leaf.boundary_confidence, 0.5),
        )
        offset += len(piece)
        yield sub


def chunk_file(path: str, filetype: str, text: str | None = None) -> Iterator[Leaf]:
    """Convenience: dispatch + chunk in one call.

    Every leaf passes through the token cap, so a chunker's CHARACTER budget
    can no longer emit a leaf that overflows the embedder's context window.
    """
    mod = get_chunker(filetype)
    for leaf in mod.chunk(path, text=text):
        yield from _enforce_leaf_token_cap(leaf)
