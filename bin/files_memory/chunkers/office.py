"""Office / document chunker — .docx .doc .pptx .ppt .xlsx .xls .rtf .epub .odt …

Converts a binary document to MARKDOWN, then delegates the actual splitting to
the markdown chunker. The heading-tree logic already exists and is tested; a
second copy of it here would be the §10a duplication defect.

── WHY THIS EXISTS ──────────────────────────────────────────────────────────

identity.py has mapped .docx/.doc/.pptx/.ppt/.xlsx/.xls/.epub to filetypes
since the extension map was written, but CHUNKER_REGISTRY had no entry for any
of them. Every one fell through to the TEXT chunker — which, for a ZIP-based
format like .docx, means the raw container bytes were ingested as if they were
prose, and for .doc means OLE compound-file bytes. Not degraded extraction:
nonsense entering FTS and the embedding space, silently. Measured 2026-09-17.

── WHY ONE LIBRARY RATHER THAN SIX ──────────────────────────────────────────

`sharepoint2text` (Apache-2.0) covers 35 MIME types through one normalized
model — including the LEGACY binaries (.doc, .xls, .ppt) that have no
trustworthy standalone pure-Python parser. Verified 2026-09-17 in a clean venv:
it round-trips .docx headings as `## Heading`, extracts .pptx slide text, and
renders .xlsx sheets as markdown tables.

The per-format alternative was python-docx + python-pptx + openpyxl + xlrd +
striprtf + ebooklib, which is six dependencies that still leave .doc unsolved
and pull ebooklib's AGPL-3.0 licence into a distributed tool. It also ships
zip-bomb limits, path-traversal rejection and an encrypted-file error class —
defensive work this chunker would otherwise have to write itself.

Heavier ML "unified" options (docling, unstructured, markitdown) were rejected:
they pull torch/onnxruntime and, in docling's case, fetch model weights from
HuggingFace at first use — a runtime network dependency m3 will not take.
"""
from __future__ import annotations

import logging
from typing import Iterator

from . import Leaf
from . import markdown as _markdown_chunker

logger = logging.getLogger("files_memory.chunkers.office")

CHUNKER_VERSION = "1.0.0"

try:
    import sharepoint2text as _s2t

    available = True
except ImportError:  # pragma: no cover — exercised on installs without the extra
    available = False

#: Cap on input size. sharepoint2text's own default is 100 MB; we pass it
#: explicitly so the limit is visible here rather than inherited silently.
MAX_FILE_BYTES = 100 * 1024 * 1024


def chunk(path: str, text: str | None = None) -> Iterator[Leaf]:
    """Yield Leaves for a binary document, split on its own heading structure.

    `text` is ignored: these formats are binary containers, so a caller's
    pre-read string is either absent or already wrong. Accepted only to satisfy
    the ChunkerProtocol signature.
    """
    if not available:
        logger.warning(
            "office chunker unavailable (sharepoint-to-text not installed); "
            "falling back for %s", path
        )
        return

    try:
        docs = list(
            _s2t.read_file(
                path,
                max_file_size=MAX_FILE_BYTES,
                # Images and annotations are not text; skipping them keeps the
                # extraction cheap and the output free of caption noise.
                ignore_images=True,
                extract_annotations=False,
            )
        )
    except _s2t.ExtractionFileEncryptedError:
        # A password-protected file is a legitimate state, not a bug. Say so
        # once, clearly, and ingest nothing rather than emitting garbage.
        logger.warning(
            "cannot extract %s. observed: file is encrypted/password-protected. "
            "possible: an intentionally protected document. "
            "inspect: decrypt it before ingestion if its content should be "
            "searchable.", path
        )
        return
    except _s2t.ExtractionError as e:
        # Covers zip-bomb, path-traversal, too-large, unsupported and the
        # legacy-parse failures. One handler: every case means "no content from
        # this file", and the type name says which.
        logger.warning(
            "cannot extract %s. observed: %s: %s. "
            "possible: corrupt file, an unsupported variant of the format, or a "
            "safety limit (size / zip-bomb / path-traversal) rejecting it. "
            "inspect: open the file in its native application to confirm it is "
            "readable.", path, type(e).__name__, e
        )
        return
    except Exception as e:  # noqa: BLE001 — one bad file must not kill an ingest
        logger.warning(
            "cannot extract %s. observed: unexpected %s: %s. "
            "possible: a library defect or an unhandled format edge case. "
            "inspect: report this with the file's extension and size.",
            path, type(e).__name__, e
        )
        return

    if not docs:
        return

    emitted = 0
    for doc in docs:
        try:
            md = _s2t.render_markdown(doc)
        except Exception as e:  # noqa: BLE001
            logger.warning("markdown render failed for %s: %s", path, e)
            continue
        if not md or not md.strip():
            continue
        # Delegate: the markdown chunker owns heading-tree splitting, section
        # merging and the size caps. Passing `text` means it never re-reads the
        # binary from disk.
        for leaf in _markdown_chunker.chunk(path, text=md):
            leaf.extra = {**(leaf.extra or {}),
                          "source_format": getattr(doc, "format", None),
                          "via": "office"}
            emitted += 1
            yield leaf

    if emitted == 0:
        # Extraction succeeded but produced nothing. Distinct from a failure,
        # and worth saying: an empty result that looks like success is how the
        # text-chunker fallback hid this whole class of bug for so long.
        logger.info("no text content extracted from %s (document may be "
                    "image-only or empty)", path)
