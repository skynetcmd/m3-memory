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
    files_conn,
    *,
    include_all: bool = True,
    corpora: Optional[list[str]] = None,
    exclude_corpora: Optional[list[str]] = None,
    limit: int = 2000,
) -> FilesLayer:
    """Load non-superseded file_nodes + their notable facts.

    Backend-agnostic: ``files_conn`` may be a raw SQLite sidecar connection OR a
    seam connection to the PostgreSQL ``files`` schema. Table names are qualified
    via ``files_table()`` (bare on SQLite, ``files.<name>`` on PG) and placeholders
    via ``dialect().param()``, so the SAME read runs on both — this is what makes
    the wiki's files corpus visible on a PG deployment (previously it silently
    dropped, because files_layer assumed a local SQLite sidecar that does not exist
    on PG once the files store moved to a schema).

    Args:
        include_all: when True (default), no implicit private-corpus exclusion.
            When False, corpora matching _DEFAULT_EXCLUDE_SUBSTRINGS are dropped
            (for a shareable export).
        corpora: explicit allowlist of corpus_ids (None = all).
        exclude_corpora: explicit denylist of corpus_ids.
    """
    from files_memory.config import files_table
    from memory.backends import dialect as _dialect
    _d = _dialect()
    _p = _d.param()
    _fn_tbl = files_table("file_nodes")
    # Name-based row access is used below (r["corpus_id"] etc). On PG the seam
    # cursor yields dual name/position rows already; on a raw sqlite3 connection we
    # must set row_factory=Row (a caller/fixture may pass one without it). Guard on
    # the connection type so we never touch the PG compat connection's factory.
    if isinstance(files_conn, sqlite3.Connection):
        files_conn.row_factory = sqlite3.Row
    try:
        rows = files_conn.execute(
            f"SELECT uuid, filename, filetype, path_absolute, file_summary, corpus_id "
            f"FROM {_fn_tbl} WHERE superseded_by IS NULL "
            f"ORDER BY filename, uuid LIMIT {_p}",
            (limit,),
        ).fetchall()
    except Exception as e:
        # No files store / no file_nodes table — the memory-only vault still works.
        if _d.is_undefined_object_error(e) or isinstance(e, sqlite3.OperationalError):
            return FilesLayer()
        raise

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
        fn.facts = _load_facts(files_conn, fn.uuid, _d, _p)
        files.append(fn)

    files.sort(key=lambda f: f.rank_key())
    return FilesLayer(files=files)


def _load_facts(conn, file_node_uuid: str, _d, _p) -> list[Fact]:
    from files_memory.config import files_table
    try:
        rows = conn.execute(
            f"SELECT uuid, statement, confidence, leaf FROM {files_table('facts')} "
            f"WHERE file_node = {_p} AND superseded_by IS NULL "
            f"ORDER BY confidence DESC, uuid LIMIT {_p}",
            (file_node_uuid, _MAX_FACTS_PER_FILE),
        ).fetchall()
    except Exception as e:
        if _d.is_undefined_object_error(e) or isinstance(e, sqlite3.OperationalError):
            return []
        raise
    return [
        Fact(
            uuid=r["uuid"],
            statement=r["statement"] or "",
            confidence=float(r["confidence"] if r["confidence"] is not None else 1.0),
            leaf=r["leaf"],
        )
        for r in rows
    ]
