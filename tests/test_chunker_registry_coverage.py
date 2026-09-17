"""Every filetype identity.py names must be handled ON PURPOSE.

── WHY THIS FILE EXISTS ──────────────────────────────────────────────────────

`identity.FILETYPE_BY_EXT` maps an extension to a filetype name;
`chunkers.CHUNKER_REGISTRY` maps a filetype name to a chunker. Nothing tied the
two together, and `get_chunker()` returned the text chunker for anything
unregistered -- so a filetype could be *recognised* while being *unhandled*, and
look identical to a deliberate text-chunk.

Measured 2026-09-17: `.html` and `.htm` had resolved to filetype "html" since
the extension map was written, but no "html" key ever existed in the registry.
Every HTML file ingested since then went through the TEXT chunker: tags, inline
`<script>` bodies and `<style>` rules entered FTS and the embedding space as if
they were prose. Nothing failed. Nothing warned.

The same hole covered `.docx`, `.pptx`, `.xlsx` and `.epub` -- binary container
formats where text-chunking yields ZIP bytes, not content.

These tests make the two maps agree, and force the choice to be explicit: either
a chunker claims the filetype, or it is listed in `TEXT_CHUNKED_FILETYPES` as a
deliberate plain-text split. "We forgot" no longer resolves to a silent pass.
"""
from __future__ import annotations

import os
import sys

import pytest

_BIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin"))
sys.path.insert(0, _BIN)

from files_memory import chunkers, identity  # noqa: E402

#: Formats whose bytes are a container, not text. Text-chunking these produces
#: garbage rather than degraded output, so a missing chunker here is a DEFECT --
#: never something TEXT_CHUNKED_FILETYPES may excuse.
_BINARY_FILETYPES = frozenset({
    "pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "epub", "rtf", "odt",
})


def test_every_known_filetype_is_handled_deliberately():
    """No filetype may fall through to text by accident."""
    known = set(identity.FILETYPE_BY_EXT.values())
    registered = set(chunkers.CHUNKER_REGISTRY)
    declared = set(chunkers.TEXT_CHUNKED_FILETYPES)

    unaccounted = sorted(known - registered - declared)
    assert not unaccounted, (
        "these filetypes resolve from an extension but no chunker claims them, "
        "and they are not declared text-chunked -- they silently fall back to "
        "the text chunker:\n  " + "\n  ".join(unaccounted) +
        "\nAdd a chunker, or add them to TEXT_CHUNKED_FILETYPES if plain-text "
        "splitting is genuinely correct."
    )


def test_binary_filetypes_are_never_declared_text_chunked():
    """TEXT_CHUNKED_FILETYPES must not be used to paper over a binary format."""
    declared = set(chunkers.TEXT_CHUNKED_FILETYPES)
    wrong = sorted(_BINARY_FILETYPES & declared)
    assert not wrong, (
        f"binary container formats declared as text-chunked: {wrong}. "
        "Text-chunking these ingests ZIP/OLE bytes, not document content -- "
        "they need a real chunker."
    )


@pytest.mark.parametrize("ext,filetype", [
    (".html", "html"), (".htm", "html"), (".xhtml", "html"), (".pdf", "pdf"),
])
def test_structured_formats_reach_their_own_chunker(ext, filetype):
    """The regression that started this: recognised, mapped, but text-chunked.

    Asserts the WHOLE path -- extension resolves to the filetype, and the
    filetype resolves to its dedicated chunker rather than the text fallback.
    """
    assert identity.FILETYPE_BY_EXT.get(ext) == filetype, (
        f"{ext} no longer maps to {filetype}")
    mod = chunkers.get_chunker(filetype)
    if not getattr(chunkers.CHUNKER_REGISTRY[filetype], "available", True):
        pytest.skip(f"{filetype} chunker deps not installed in this environment")
    assert mod.__name__.endswith(f".{filetype}"), (
        f"{ext} ({filetype}) resolved to {mod.__name__}, not the {filetype} "
        f"chunker -- it would be ingested as raw text"
    )


def test_registry_entries_satisfy_the_chunker_protocol():
    """A registered module must actually be usable as a chunker."""
    for filetype, mod in sorted(
        chunkers.CHUNKER_REGISTRY.items(), key=lambda kv: kv[0]
    ):
        assert hasattr(mod, "chunk"), f"{filetype}: no chunk() function"
        assert callable(mod.chunk), f"{filetype}: chunk is not callable"
        assert hasattr(mod, "CHUNKER_VERSION"), (
            f"{filetype}: no CHUNKER_VERSION -- the ingester stamps it on every "
            f"leaf so a re-chunk can be detected")
