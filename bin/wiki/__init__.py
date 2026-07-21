"""m3 wiki generator — compile core memories + files corpus into an interlinked vault.

A *projection*, not a new store: reads agent_memory.db and files_database.db and
renders deterministic Markdown pages (Obsidian-ready) under the engine root. See
`build.build_wiki` for the pure builder entry point and `bin/gen_wiki.py` for the
CLI shell.

Public surface:
    build.build_wiki(mem_conn, files_conn, opts) -> dict[relpath, markdown_text]
    build.WikiOptions                             — the knobs
"""
from __future__ import annotations

from .build import WikiOptions, build_wiki

__all__ = ["build_wiki", "WikiOptions"]
