"""MCP tool registration for the files.db subsystem.

Six tools in phase 1:
  - files_ingest        — walk + ingest a directory
  - files_index         — wiki-index of file summaries (cheap-first triage)
  - files_search        — hybrid FTS5 + vector search over leaves
  - files_get           — fetch a single record by UUID
  - files_stats         — corpus-level counters
  - files_health        — DB integrity + FTS5 sync check

Standalone script: run `python -m files_memory.tools` to start a
FastMCP stdio server exposing just these tools. The same module is
imported by the main MCP bridge in phase 2 to merge into the unified
tool catalog.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from . import config
from .db import integrity_check, init_db, rebuild_fts
from .index import files_get, files_index, files_stats
from .ingest import IngestResult, ingest_path
from .search import files_search as _files_search

logger = logging.getLogger("files_memory.tools")


# ──────────────────────────────────────────────────────────────────────────────
# Tool implementations (FastMCP-agnostic; thin wrappers serialize results)
# ──────────────────────────────────────────────────────────────────────────────
def _serialize_ingest_result(r: IngestResult) -> dict:
    return {
        "run_id": r.run_id,
        "root": r.root,
        "duration_ms": r.duration_ms,
        "files_created": r.files_created,
        "files_superseded": r.files_superseded,
        "files_unchanged": r.files_unchanged,
        "files_failed": r.files_failed,
        "leaves_written": r.leaves_written,
        "walk": {
            "files_seen": r.walk_stats.files_seen if r.walk_stats else 0,
            "files_yielded": r.walk_stats.files_yielded if r.walk_stats else 0,
            "skipped_ext": r.walk_stats.skipped_ext if r.walk_stats else 0,
            "skipped_binary": r.walk_stats.skipped_binary if r.walk_stats else 0,
            "skipped_size": r.walk_stats.skipped_size if r.walk_stats else 0,
            "skipped_gitignore": r.walk_stats.skipped_gitignore if r.walk_stats else 0,
            "skipped_glob": r.walk_stats.skipped_glob if r.walk_stats else 0,
        } if r.walk_stats else {},
        "failures": r.failures[:50],   # cap the response payload
    }


def files_ingest_impl(
    path: str,
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
    max_depth: Optional[int] = None,
    corpus: Optional[str] = None,
    dry_run: bool = False,
    force_size: bool = False,
    record_noops: bool = False,
    follow_symlinks: Optional[bool] = None,
) -> dict:
    """Walk PATH and ingest every supported file.

    Args:
      path: directory to walk. Required.
      include: glob patterns; only matching files are ingested.
      exclude: glob patterns; matching files are skipped.
      max_depth: max recursion depth (0 = root only).
      corpus: scope tag (default 'default').
      dry_run: walk + count but DO NOT write to DB.
      force_size: bypass the per-file size cap.
      record_noops: write 'unchanged_skipped' rows for audit.
      follow_symlinks: override default-off symlink policy.

    Returns:
      JSON-safe dict with run summary and walk stats. See plan §10.
    """
    result = ingest_path(
        path,
        include=include, exclude=exclude, max_depth=max_depth,
        corpus_id=corpus, dry_run=dry_run, force_size=force_size,
        record_noops=record_noops, follow_symlinks=follow_symlinks,
    )
    return _serialize_ingest_result(result)


def files_index_impl(
    corpus: Optional[str] = None,
    filetype: Optional[str] = None,
    directory: Optional[str] = None,
    filename_glob: Optional[str] = None,
    include_history: bool = False,
    limit: int = 500,
) -> list[dict]:
    """Return file-level summaries for triage (wiki-index primitive).

    Cheap-first retrieval: returns metadata + file_summary only, NO leaf
    content. Use this BEFORE files_search to triage which files are worth
    deep-reading. Default sort: date_modified DESC.

    Args:
      corpus: scope filter.
      filetype: filter to one filetype (e.g. 'markdown', 'pdf').
      directory: filter to files whose absolute path starts here.
      filename_glob: SQL GLOB pattern over filename ('*.md', 'README*').
      include_history: include superseded file_nodes.
      limit: max entries.
    """
    entries = files_index(
        corpus_id=corpus, filetype=filetype, directory=directory,
        filename_glob=filename_glob, include_history=include_history,
        limit=limit,
    )
    return [
        {
            "uuid": e.file_node_uuid,
            "filename": e.filename,
            "filetype": e.filetype,
            "path": e.path,
            "version": e.version_label,
            "date_modified": e.date_modified,
            "summary": e.summary,
        }
        for e in entries
    ]


def files_search_impl(
    query: str,
    limit: int = 10,
    corpus: Optional[str] = None,
    filetype: Optional[str] = None,
    include_history: bool = False,
) -> list[dict]:
    """Hybrid search over leaves: FTS5 + vector cosine + RRF.

    Default filters to current (non-superseded) leaves. Pass
    include_history=True for time-travel queries.

    Returns ranked SearchHit records with text, provenance (file +
    division), and per-channel rank info for debugging.
    """
    hits = _files_search(
        query, limit=limit, corpus_id=corpus,
        filetype=filetype, include_history=include_history,
    )
    return [
        {
            "leaf_uuid": h.leaf_uuid,
            "file_node_uuid": h.file_node_uuid,
            "filename": h.filename,
            "path": h.path,
            "division": f"{h.division_type}:{h.division_id}",
            "division_label": h.division_label,
            "text": h.text,
            "score": h.score,
            "fts_rank": h.fts_rank,
            "vec_rank": h.vec_rank,
        }
        for h in hits
    ]


def files_get_impl(uuid: str) -> Optional[dict]:
    """Fetch one record by UUID (file_node or leaf)."""
    return files_get(uuid)


def files_stats_impl(corpus: Optional[str] = None) -> dict:
    """Corpus-level counters: file_nodes, leaves, embed coverage, by-filetype."""
    return files_stats(corpus_id=corpus)


def files_health_impl(rebuild: bool = False) -> dict:
    """DB integrity + FTS5 sync check.

    Args:
      rebuild: if True, rebuild FTS5 indexes if out-of-sync. Otherwise
               just report.
    """
    check = integrity_check()
    if rebuild and not check.get("fts5_in_sync", True):
        rebuild_fts()
        check = integrity_check()
        check["fts_rebuilt"] = True
    return check


# ──────────────────────────────────────────────────────────────────────────────
# Standalone MCP server entry point
# ──────────────────────────────────────────────────────────────────────────────
def register(mcp) -> None:
    """Register all files_* tools on a FastMCP instance.

    Used by both the standalone server below and the main MCP bridge
    when it imports us in phase 2.
    """
    @mcp.tool()
    def files_ingest(
        path: str,
        include: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        max_depth: Optional[int] = None,
        corpus: Optional[str] = None,
        dry_run: bool = False,
        force_size: bool = False,
        record_noops: bool = False,
    ) -> dict:
        """Walk a directory and ingest supported files into files.db.
        Idempotent: same content_sha256 → no-op; changed content →
        new file_node version supersedes prior."""
        return files_ingest_impl(
            path=path, include=include, exclude=exclude, max_depth=max_depth,
            corpus=corpus, dry_run=dry_run, force_size=force_size,
            record_noops=record_noops,
        )

    @mcp.tool()
    def files_index(
        corpus: Optional[str] = None,
        filetype: Optional[str] = None,
        directory: Optional[str] = None,
        filename_glob: Optional[str] = None,
        include_history: bool = False,
        limit: int = 500,
    ) -> list[dict]:
        """Return file-level summaries for triage.
        Cheap-first retrieval — no leaf content. Use BEFORE files_search."""
        return files_index_impl(
            corpus=corpus, filetype=filetype, directory=directory,
            filename_glob=filename_glob, include_history=include_history,
            limit=limit,
        )

    @mcp.tool()
    def files_search(
        query: str,
        limit: int = 10,
        corpus: Optional[str] = None,
        filetype: Optional[str] = None,
        include_history: bool = False,
    ) -> list[dict]:
        """Hybrid FTS5 + vector search over leaves.
        Default: current versions only. Set include_history=True for time-travel."""
        return files_search_impl(
            query=query, limit=limit, corpus=corpus,
            filetype=filetype, include_history=include_history,
        )

    @mcp.tool()
    def files_get(uuid: str) -> Optional[dict]:
        """Fetch one record by UUID. Tries file_nodes then leaves."""
        return files_get_impl(uuid=uuid)

    @mcp.tool()
    def files_stats(corpus: Optional[str] = None) -> dict:
        """Corpus-level counters: file_nodes, leaves, embed coverage."""
        return files_stats_impl(corpus=corpus)

    @mcp.tool()
    def files_health(rebuild: bool = False) -> dict:
        """DB integrity + FTS5 sync check. Set rebuild=True to fix drift."""
        return files_health_impl(rebuild=rebuild)


def _build_standalone_server():
    """Build a FastMCP server exposing only files_* tools."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        from mcp import FastMCP  # type: ignore

    mcp = FastMCP("files-memory")
    register(mcp)
    return mcp


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry: `python -m files_memory.tools [ingest|search|index|stats|health|serve]`
# ──────────────────────────────────────────────────────────────────────────────
def main() -> int:
    import argparse
    import json as _json
    import sys

    p = argparse.ArgumentParser(prog="files-memory")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="walk a directory and ingest files")
    p_ing.add_argument("path")
    p_ing.add_argument("--include", action="append", default=None)
    p_ing.add_argument("--exclude", action="append", default=None)
    p_ing.add_argument("--max-depth", type=int, default=None)
    p_ing.add_argument("--corpus", default=None)
    p_ing.add_argument("--dry-run", action="store_true")
    p_ing.add_argument("--force-size", action="store_true")
    p_ing.add_argument("--record-noops", action="store_true")

    p_sea = sub.add_parser("search", help="hybrid search over leaves")
    p_sea.add_argument("query")
    p_sea.add_argument("--limit", type=int, default=10)
    p_sea.add_argument("--corpus", default=None)
    p_sea.add_argument("--filetype", default=None)
    p_sea.add_argument("--include-history", action="store_true")

    p_idx = sub.add_parser("index", help="file-level summary triage")
    p_idx.add_argument("--corpus", default=None)
    p_idx.add_argument("--filetype", default=None)
    p_idx.add_argument("--directory", default=None)
    p_idx.add_argument("--glob", dest="filename_glob", default=None)
    p_idx.add_argument("--include-history", action="store_true")
    p_idx.add_argument("--limit", type=int, default=500)

    p_get = sub.add_parser("get", help="fetch by UUID")
    p_get.add_argument("uuid")

    sub.add_parser("stats", help="corpus counters").add_argument(
        "--corpus", default=None,
    )

    p_h = sub.add_parser("health", help="integrity + FTS5 check")
    p_h.add_argument("--rebuild", action="store_true")

    sub.add_parser("serve", help="run FastMCP stdio server")

    args = p.parse_args()

    if args.cmd == "ingest":
        r = files_ingest_impl(
            path=args.path, include=args.include, exclude=args.exclude,
            max_depth=args.max_depth, corpus=args.corpus,
            dry_run=args.dry_run, force_size=args.force_size,
            record_noops=args.record_noops,
        )
    elif args.cmd == "search":
        r = files_search_impl(
            query=args.query, limit=args.limit, corpus=args.corpus,
            filetype=args.filetype, include_history=args.include_history,
        )
    elif args.cmd == "index":
        r = files_index_impl(
            corpus=args.corpus, filetype=args.filetype,
            directory=args.directory, filename_glob=args.filename_glob,
            include_history=args.include_history, limit=args.limit,
        )
    elif args.cmd == "get":
        r = files_get_impl(uuid=args.uuid)
    elif args.cmd == "stats":
        r = files_stats_impl(corpus=args.corpus)
    elif args.cmd == "health":
        r = files_health_impl(rebuild=args.rebuild)
    elif args.cmd == "serve":
        mcp = _build_standalone_server()
        mcp.run()
        return 0
    else:
        p.print_help()
        return 2

    print(_json.dumps(r, indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
