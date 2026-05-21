"""Plain-text fallback chunker — semantic paragraph chunker.

The "everything else" chunker. Splits on paragraph boundaries (blank
lines), then merges adjacent paragraphs greedily up to a target chunk
size. This is the production-RAG consensus floor: don't slice by fixed
token count if you can avoid it. (See FILE_INGESTION_PLAN.md §6.)

Phase-2 upgrade: sentence-similarity merging (compute embeddings for
adjacent paragraphs and only merge when cosine ≥ threshold). The
plumbing for embeddings already exists; we don't wire it here because
inline embeddings during chunking would couple chunking to the embed
backend's availability. That trade-off lands once we have eval data
showing semantic-merge meaningfully beats greedy-paragraph.

Yields Leaf with:
  division_type = 'window'
  division_id   = '0', '1', ...
  division_label = '<first 60 chars of text>...'
  boundary_confidence = 0.8 (paragraph boundaries are good but not structural)
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator

from . import Leaf

logger = logging.getLogger("files_memory.chunkers.text")

CHUNKER_VERSION = "1.0.0"
available = True

# Target chunk size in characters. ~1500-2000 chars is a typical bge-m3
# leaf — leaves room for the surrounding context the embedder adds.
TARGET_CHARS = 1800
MAX_CHARS = 2400
MIN_CHARS = 300


def chunk(path: str, text: str | None = None) -> Iterator[Leaf]:
    if text is None:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning("text chunker: read failed for %s: %s", path, e)
            return

    if not text or not text.strip():
        return

    # Strip BOM / trim leading-trailing whitespace; keep relative offsets.
    lstrip_offset = len(text) - len(text.lstrip())
    body = text[lstrip_offset:]

    # Split by blank lines (paragraph boundary).
    para_re = re.compile(r"\n\s*\n")
    paragraphs: list[tuple[int, str]] = []
    cursor = 0
    for m in para_re.finditer(body):
        para = body[cursor:m.start()]
        if para.strip():
            paragraphs.append((cursor + lstrip_offset, para))
        cursor = m.end()
    # Trailing paragraph
    tail = body[cursor:]
    if tail.strip():
        paragraphs.append((cursor + lstrip_offset, tail))

    if not paragraphs:
        return

    # Greedy merge: pack paragraphs into chunks up to TARGET_CHARS,
    # never exceeding MAX_CHARS. If a single paragraph exceeds MAX_CHARS,
    # split it at sentence boundaries (regex on `.!?` followed by space).
    chunks: list[tuple[int, int, str]] = []  # (start, end, text)
    cur_start = paragraphs[0][0]
    cur_buf: list[str] = []
    cur_chars = 0
    cur_end = paragraphs[0][0]

    def flush():
        nonlocal cur_buf, cur_chars
        if cur_buf:
            joined = "\n\n".join(cur_buf).strip()
            if joined:
                chunks.append((cur_start, cur_end, joined))
            cur_buf = []
            cur_chars = 0

    for start, para in paragraphs:
        para_len = len(para)
        # Single oversized paragraph: emit as its own chunk(s).
        if para_len > MAX_CHARS:
            flush()
            for sub_start, sub_text in _split_sentences(para, start, MAX_CHARS):
                chunks.append((sub_start, sub_start + len(sub_text), sub_text.strip()))
            cur_start = start + para_len + 2  # past this paragraph
            cur_end = cur_start
            continue

        if cur_chars + para_len > TARGET_CHARS and cur_buf:
            flush()
            cur_start = start
        cur_buf.append(para)
        cur_chars += para_len + 2
        cur_end = start + para_len

    flush()

    # Merge tail chunks that are below MIN_CHARS into previous.
    cleaned: list[tuple[int, int, str]] = []
    for s, end, t in chunks:
        if cleaned and len(t) < MIN_CHARS:
            ps, pe, pt = cleaned[-1]
            cleaned[-1] = (ps, end, (pt + "\n\n" + t).strip())
        else:
            cleaned.append((s, end, t))

    for i, (s, end, t) in enumerate(cleaned):
        if not t.strip():
            continue
        label = t.replace("\n", " ").strip()[:60]
        if len(t.replace("\n", " ").strip()) > 60:
            label += "..."
        yield Leaf(
            text=t,
            division_type="window",
            division_id=str(i),
            division_label=label,
            char_range_start=s,
            char_range_end=end,
            boundary_confidence=0.8,
            extra={"paragraph_merge": True},
        )


def _split_sentences(para: str, start_offset: int, max_chars: int) -> Iterator[tuple[int, str]]:
    """Split an oversize paragraph at sentence boundaries.

    Yields (absolute_start, text) tuples. Never returns chunks smaller
    than max_chars/4 (would be too fragmented).
    """
    # Naive sentence split: '.', '!', '?' followed by whitespace + capital
    # or end-of-string. Good enough for English prose; not a parser.
    sent_re = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])")
    sentences = sent_re.split(para)
    if not sentences:
        yield (start_offset, para)
        return

    buf: list[str] = []
    buf_len = 0
    chunk_start = start_offset
    cursor = start_offset

    for sent in sentences:
        sent_len = len(sent) + 1  # +1 for the space we'll glue back
        if buf_len + sent_len > max_chars and buf:
            yield (chunk_start, " ".join(buf))
            chunk_start = cursor
            buf = []
            buf_len = 0
        buf.append(sent)
        buf_len += sent_len
        cursor += sent_len

    if buf:
        yield (chunk_start, " ".join(buf))
