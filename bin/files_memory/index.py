"""Summary-first wiki index — the Karpathy-LLM-wiki primitive.

Returns file-level summaries WITHOUT leaf content. The intended caller
pattern (FILE_INGESTION_PLAN.md §10.1):

  1. files_index(filter=...)      → ~50 tokens × N entries
  2. LLM picks 3-5 file UUIDs
  3. files_search(query, file_node IN chosen)  → targeted leaf retrieval
  4. LLM synthesizes answer with provenance

The win is token efficiency: a 200-file corpus = ~10k tokens for the
index call. Beats embed-search-everything when the corpus has good
file-level summaries.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

from .db import _db

logger = logging.getLogger("files_memory.index")


@dataclass
class IndexEntry:
    """One entry in the wiki-index — file-level summary + metadata only.

    `original_path` is the user-facing source-of-truth path (e.g. the
    .pdf for an ingested .pdf.txt). None if the file is its own
    original — `path` is then the right reference to surface to the
    user.
    """
    file_node_uuid: str
    filename: str
    filetype: str
    path: str
    version_label: str
    date_modified: str
    summary: Optional[str]
    original_path: Optional[str] = None
    corpus_id: Optional[str] = None


def files_index(
    *,
    corpus_id: Optional[str] = None,
    corpora: Optional[list[str]] = None,
    filetype: Optional[str] = None,
    directory: Optional[str] = None,
    filename_glob: Optional[str] = None,
    include_history: bool = False,
    limit: int = 1000,
    db_path: Optional[str] = None,
) -> list[IndexEntry]:
    """Return file-level summaries for triage.

    Args:
        corpus_id: single-corpus scope filter.
        corpora: list of corpus IDs to fan out across. Overrides corpus_id.
        filetype: filter to one filetype.
        directory: filter to file_nodes whose path_absolute starts with this.
        filename_glob: SQL GLOB pattern over filename.
        include_history: include superseded file_nodes.
        limit: max entries returned (sorted by date_modified DESC).
    """
    sql_parts = [
        "SELECT uuid, filename, filetype, path_absolute, version_label, "
        "       date_modified, file_summary, metadata, corpus_id "
        "FROM file_nodes WHERE 1 = 1"
    ]
    params: list = []
    if not include_history:
        sql_parts.append("AND superseded_by IS NULL")
    if corpora:
        clean = [c for c in corpora if c]
        if clean:
            placeholders = ",".join("?" * len(clean))
            sql_parts.append(f"AND corpus_id IN ({placeholders})")
            params.extend(clean)
    elif corpus_id:
        sql_parts.append("AND corpus_id = ?")
        params.append(corpus_id)
    if filetype:
        sql_parts.append("AND filetype = ?")
        params.append(filetype)
    if directory:
        import os as _os
        prefix = _os.path.abspath(directory)
        sql_parts.append("AND path_absolute LIKE ?")
        # SQLite LIKE uses % for any-chars; escape any literal % in path.
        params.append(prefix.replace("%", "[%]") + "%")
    if filename_glob:
        sql_parts.append("AND filename GLOB ?")
        params.append(filename_glob)

    sql_parts.append("ORDER BY date_modified DESC LIMIT ?")
    params.append(limit)

    with _db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(" ".join(sql_parts), params).fetchall()

    from .provenance import original_path_for_metadata
    out: list[IndexEntry] = []
    for r in rows:
        out.append(IndexEntry(
            file_node_uuid=r["uuid"],
            filename=r["filename"],
            filetype=r["filetype"],
            path=r["path_absolute"],
            version_label=r["version_label"],
            date_modified=r["date_modified"],
            summary=r["file_summary"],
            original_path=original_path_for_metadata(r["metadata"]),
            corpus_id=r["corpus_id"],
        ))
    return out


def files_stats(
    *,
    corpus_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Corpus-level counters for files_stats MCP tool."""
    with _db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        scope_clause = ""
        params: list = []
        if corpus_id:
            scope_clause = " WHERE corpus_id = ?"
            params = [corpus_id]

        fn_count = conn.execute(
            f"SELECT COUNT(*) FROM file_nodes{scope_clause}", params,
        ).fetchone()[0]
        fn_current = conn.execute(
            f"SELECT COUNT(*) FROM file_nodes{scope_clause} "
            f"{'AND' if scope_clause else 'WHERE'} superseded_by IS NULL",
            params,
        ).fetchone()[0]

        # Leaf counts can't be conditionally scoped without a join; we
        # join through file_nodes.
        leaf_q = (
            "SELECT COUNT(*) FROM leaves l JOIN file_nodes fn ON fn.uuid = l.file_node"
        )
        leaf_current_q = leaf_q + " WHERE l.superseded_by IS NULL AND fn.superseded_by IS NULL"
        if corpus_id:
            leaf_q += " WHERE fn.corpus_id = ?"
            leaf_current_q += " AND fn.corpus_id = ?"
        leaf_count = conn.execute(leaf_q, params).fetchone()[0]
        leaf_current = conn.execute(leaf_current_q, params).fetchone()[0]

        embed_q = (
            "SELECT COUNT(*) FROM leaf_embeddings le "
            "JOIN leaves l ON l.uuid = le.leaf_uuid "
            "JOIN file_nodes fn ON fn.uuid = l.file_node "
            "WHERE le.kind = 'text'"
        )
        if corpus_id:
            embed_q += " AND fn.corpus_id = ?"
        leaf_embed_count = conn.execute(embed_q, params).fetchone()[0]

        # Per-filetype breakdown (current only).
        ft_q = (
            "SELECT filetype, COUNT(*) AS n FROM file_nodes "
            "WHERE superseded_by IS NULL"
        )
        if corpus_id:
            ft_q += " AND corpus_id = ?"
        ft_q += " GROUP BY filetype ORDER BY n DESC"
        by_filetype = {r["filetype"]: r["n"] for r in conn.execute(ft_q, params).fetchall()}

        # Run counts
        run_q = "SELECT COUNT(DISTINCT run_id) FROM ingestion_runs"
        run_count = conn.execute(run_q).fetchone()[0]

        return {
            "file_nodes_total": fn_count,
            "file_nodes_current": fn_current,
            "file_nodes_superseded": fn_count - fn_current,
            "leaves_total": leaf_count,
            "leaves_current": leaf_current,
            "leaves_with_text_embedding": leaf_embed_count,
            "leaves_embed_coverage": (
                (leaf_embed_count / leaf_current) if leaf_current else 0.0
            ),
            "by_filetype": by_filetype,
            "ingest_runs": run_count,
            "corpus_id": corpus_id,
        }


def files_get(uuid: str, *, db_path: Optional[str] = None) -> Optional[dict]:
    """Fetch a single record by UUID. Tries file_nodes, then leaves.

    Returns a dict view of the row plus:
      _kind:         'file_node' | 'leaf'
      original_path: surfaced from file_nodes.metadata.provenance for
                     convenience. None if the file is its own original.

    Returns None if no UUID match.
    """
    from .provenance import original_path_for_metadata
    with _db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        # Try file_nodes first
        r = conn.execute(
            "SELECT * FROM file_nodes WHERE uuid = ?", (uuid,),
        ).fetchone()
        if r:
            d = dict(r)
            d["_kind"] = "file_node"
            d["original_path"] = original_path_for_metadata(d.get("metadata"))
            return d
        r = conn.execute(
            "SELECT l.*, fn.filename, fn.path_absolute, fn.filetype, "
            "       fn.metadata AS file_metadata "
            "FROM leaves l JOIN file_nodes fn ON fn.uuid = l.file_node "
            "WHERE l.uuid = ?",
            (uuid,),
        ).fetchone()
        if r:
            d = dict(r)
            d["_kind"] = "leaf"
            d["original_path"] = original_path_for_metadata(d.pop("file_metadata", None))
            return d
    return None
