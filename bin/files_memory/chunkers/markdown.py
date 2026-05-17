"""Markdown / RST chunker — heading-tree splitter.

Splits a markdown file at top-level headings (ATX `#`, `##`, `###`) into
one Leaf per section. Preserves the heading hierarchy as
`division_id = 'h1/h1.h2/...'` so a search for "section X" can match the
full path.

Code fences (```...```) are kept intact within their parent section —
never split across leaves. Frontmatter (YAML/TOML at file start) is
stripped before chunking but the m3_doc_id from it has already been
extracted by identity.py.

Tunables (kept as module constants for now; env-driven if needed later):
  MAX_HEADING_DEPTH = 3      — h4+ are kept inline with their h3 parent
  MIN_SECTION_CHARS = 50     — sections below this merge with the next sibling
  MAX_SECTION_CHARS = 16000  — sections above this split mid-paragraph

Yields Leaf with:
  division_type = 'heading'
  division_id   = 'h2/h2.h3' (nested anchor path; 'preamble' for pre-h1 text)
  division_label = the literal heading text
  boundary_confidence = 1.0 (structural split — high confidence)
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator

from . import Leaf

logger = logging.getLogger("files_memory.chunkers.markdown")

CHUNKER_VERSION = "1.0.0"
available = True

# ─── Regexes ──────────────────────────────────────────────────────────────────
# ATX heading: `# text` through `###### text`. ATX-style closing (`# text #`)
# is also recognized (rare but valid).
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?\s*$", re.MULTILINE)

# YAML or TOML frontmatter at file start.
_YAML_FM_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
_TOML_FM_RE = re.compile(r"\A\+\+\+\s*\n.*?\n\+\+\+\s*\n", re.DOTALL)

# Code fences. We keep these intact within a section.
_FENCE_RE = re.compile(r"^```", re.MULTILINE)


MAX_HEADING_DEPTH = 3
MIN_SECTION_CHARS = 50
MAX_SECTION_CHARS = 16000


def _strip_frontmatter(text: str) -> tuple[str, int]:
    """Strip leading YAML/TOML frontmatter. Returns (stripped, offset).

    The offset is needed so char_range fields in emitted Leaves still
    point at the correct location in the original file.
    """
    m = _YAML_FM_RE.match(text)
    if m:
        return text[m.end():], m.end()
    m = _TOML_FM_RE.match(text)
    if m:
        return text[m.end():], m.end()
    return text, 0


def _is_inside_fence(text: str, pos: int) -> bool:
    """True if `pos` is inside an open code fence at that point."""
    fence_count = sum(1 for _ in _FENCE_RE.finditer(text, 0, pos))
    return (fence_count % 2) == 1


def _heading_anchor(label: str) -> str:
    """Slug-ify a heading label for use in division_id paths."""
    s = label.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s.strip("-") or "section"


def chunk(path: str, text: str | None = None) -> Iterator[Leaf]:
    if text is None:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("markdown chunker: read failed for %s: %s", path, e)
            return

    if not text:
        return

    stripped, fm_offset = _strip_frontmatter(text)

    # Collect heading positions, ignoring those inside fenced code blocks.
    headings: list[tuple[int, int, int, str]] = []  # (start, end, depth, label)
    for m in _HEADING_RE.finditer(stripped):
        if _is_inside_fence(stripped, m.start()):
            continue
        depth = len(m.group(1))
        if depth > MAX_HEADING_DEPTH:
            continue
        headings.append((m.start(), m.end(), depth, m.group(2).strip()))

    # If no headings, emit the whole document as a single 'preamble' leaf.
    if not headings:
        yield from _emit_preamble(stripped, fm_offset, label="(whole file)")
        return

    # Preamble (anything before the first heading).
    first_start = headings[0][0]
    if first_start > 0:
        preamble = stripped[:first_start].strip()
        if preamble and len(preamble) >= MIN_SECTION_CHARS:
            yield from _emit_preamble(preamble, fm_offset, label="preamble")

    # Walk headings, emitting one leaf per section. Maintain a heading
    # stack so nested headings get composite division_ids.
    stack: list[tuple[int, str]] = []  # [(depth, anchor)]
    for i, (start, h_end, depth, label) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(stripped)
        section_text = stripped[start:end]

        # Update heading stack
        while stack and stack[-1][0] >= depth:
            stack.pop()
        anchor = _heading_anchor(label)
        stack.append((depth, anchor))
        div_id = "/".join(a for _, a in stack)

        cleaned = section_text.strip()
        if not cleaned:
            continue

        # Oversize sections split mid-paragraph
        if len(cleaned) > MAX_SECTION_CHARS:
            yield from _split_oversize(
                cleaned, start + fm_offset, div_id, label,
            )
            continue

        yield Leaf(
            text=cleaned,
            division_type="heading",
            division_id=div_id,
            division_label=label,
            char_range_start=start + fm_offset,
            char_range_end=end + fm_offset,
            boundary_confidence=1.0,
            extra={"depth": depth, "anchor": anchor},
        )


def _emit_preamble(text: str, fm_offset: int, label: str) -> Iterator[Leaf]:
    """Emit a preamble (no heading) as one or more leaves."""
    cleaned = text.strip()
    if not cleaned:
        return
    if len(cleaned) > MAX_SECTION_CHARS:
        yield from _split_oversize(cleaned, fm_offset, "preamble", label)
        return
    yield Leaf(
        text=cleaned,
        division_type="heading",
        division_id="preamble",
        division_label=label,
        char_range_start=fm_offset,
        char_range_end=fm_offset + len(text),
        boundary_confidence=0.9,
        extra={"depth": 0, "anchor": "preamble"},
    )


def _split_oversize(text: str, start_offset: int, div_id: str, label: str) -> Iterator[Leaf]:
    """Split a too-large section by paragraph boundaries.

    Yields multiple Leaves with `division_id = <orig>/part-N` and
    boundary_confidence dropped to reflect mid-section splits.
    """
    paragraphs = re.split(r"\n\s*\n", text)
    bucket: list[str] = []
    bucket_chars = 0
    part = 1
    section_start = start_offset

    for para in paragraphs:
        para_len = len(para) + 2  # +2 for the blank line separator
        if bucket_chars + para_len > MAX_SECTION_CHARS and bucket:
            chunk_text = "\n\n".join(bucket).strip()
            yield Leaf(
                text=chunk_text,
                division_type="heading",
                division_id=f"{div_id}/part-{part}",
                division_label=f"{label} (part {part})",
                char_range_start=section_start,
                char_range_end=section_start + len(chunk_text),
                boundary_confidence=0.6,  # mid-section split
                extra={"oversize_split": True},
            )
            section_start += len(chunk_text) + 2
            bucket = []
            bucket_chars = 0
            part += 1
        bucket.append(para)
        bucket_chars += para_len

    if bucket:
        chunk_text = "\n\n".join(bucket).strip()
        yield Leaf(
            text=chunk_text,
            division_type="heading",
            division_id=f"{div_id}/part-{part}" if part > 1 else div_id,
            division_label=f"{label} (part {part})" if part > 1 else label,
            char_range_start=section_start,
            char_range_end=section_start + len(chunk_text),
            boundary_confidence=0.7 if part > 1 else 1.0,
            extra={"oversize_split": part > 1},
        )
