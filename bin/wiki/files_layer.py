"""Load the files-corpus (evidence) layer for the wiki.

Pure reads over the files DB connection. Produces one FileNode per non-superseded
file_node (its file_summary becomes a `sources/*` page) plus the notable facts per
file (linked as evidence). Optional corpus allow/exclude filtering is a code seam
for a future share/export path — by default (per the locked decision) nothing is
excluded.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Fact:
    uuid: str
    statement: str
    confidence: float
    leaf: Optional[str]


@dataclass
class FileNode:
    uuid: str
    filename: str
    filetype: str
    path: str
    summary: Optional[str]
    corpus_id: Optional[str]
    facts: list[Fact] = field(default_factory=list)

    def rank_key(self) -> tuple:
        return (self.filename or "", self.uuid)


@dataclass
class FilesLayer:
    files: list[FileNode] = field(default_factory=list)

    @property
    def by_uuid(self) -> dict[str, FileNode]:
        return {f.uuid: f for f in self.files}


# Conservative default exclusions for a would-be *shareable* export. NOT applied
# by default (include_all=True) — kept so a future `--export`/share path can opt
# in. There is no first-class private-corpus flag in the schema, so any export
# must own this decision explicitly.
_DEFAULT_EXCLUDE_SUBSTRINGS = ("bench", "lme", "locomo", "private", "eval-", "_test")

_MAX_FACTS_PER_FILE = 12


def load_files_layer(
    files_conn: sqlite3.Connection,
    *,
    include_all: bool = True,
    corpora: Optional[list[str]] = None,
    exclude_corpora: Optional[list[str]] = None,
    limit: int = 2000,
) -> FilesLayer:
    """Load non-superseded file_nodes + their notable facts.

    Args:
        include_all: when True (default), no implicit private-corpus exclusion.
            When False, corpora matching _DEFAULT_EXCLUDE_SUBSTRINGS are dropped
            (for a shareable export).
        corpora: explicit allowlist of corpus_ids (None = all).
        exclude_corpora: explicit denylist of corpus_ids.
    """
    files_conn.row_factory = sqlite3.Row
    try:
        rows = files_conn.execute(
            "SELECT uuid, filename, filetype, path_absolute, file_summary, corpus_id "
            "FROM file_nodes WHERE superseded_by IS NULL "
            "ORDER BY filename, uuid LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        # No files DB / no file_nodes table — the memory-only vault still works.
        return FilesLayer()

    allow = set(corpora) if corpora else None
    deny = set(exclude_corpora) if exclude_corpora else set()

    files: list[FileNode] = []
    for r in rows:
        cid = r["corpus_id"]
        if allow is not None and cid not in allow:
            continue
        if cid in deny:
            continue
        if not include_all and cid and any(s in cid.lower() for s in _DEFAULT_EXCLUDE_SUBSTRINGS):
            continue
        fn = FileNode(
            uuid=r["uuid"],
            filename=r["filename"] or "(unnamed)",
            filetype=r["filetype"] or "",
            path=r["path_absolute"] or "",
            summary=r["file_summary"],
            corpus_id=cid,
        )
        fn.facts = _load_facts(files_conn, fn.uuid)
        files.append(fn)

    files.sort(key=lambda f: f.rank_key())
    return FilesLayer(files=files)


def _load_facts(conn: sqlite3.Connection, file_node_uuid: str) -> list[Fact]:
    try:
        rows = conn.execute(
            "SELECT uuid, statement, confidence, leaf FROM facts "
            "WHERE file_node = ? AND superseded_by IS NULL "
            "ORDER BY confidence DESC, uuid LIMIT ?",
            (file_node_uuid, _MAX_FACTS_PER_FILE),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [
        Fact(
            uuid=r["uuid"],
            statement=r["statement"] or "",
            confidence=float(r["confidence"] if r["confidence"] is not None else 1.0),
            leaf=r["leaf"],
        )
        for r in rows
    ]
