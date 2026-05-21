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
from typing import Optional

from .db import integrity_check, rebuild_fts
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
        "facts_extracted": r.facts_extracted,
        "leaves_carried": r.leaves_carried,
        "leaves_evolved": r.leaves_evolved,
        "embeds_avoided": r.embeds_avoided,
        "facts_carried": r.facts_carried,
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
    extract_mode: Optional[str] = None,
    original_path: Optional[str] = None,
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
      extract_mode: 'none' | 'inline' | 'queue'. Default 'none'.
        'inline' extracts facts synchronously inside the ingest txn.
        'queue' marks leaves pending; drain with files_extract_pending.
      original_path: optional pointer at the user-facing source artifact
        (e.g. the .pdf for an ingested .pdf.txt). Applied to every file
        in the walk; per-file overrides via a sidecar <path>.m3meta.json.
        See files_memory.provenance for the sidecar shape.

    Returns:
      JSON-safe dict with run summary and walk stats. See plan §10.
    """
    result = ingest_path(
        path,
        include=include, exclude=exclude, max_depth=max_depth,
        corpus_id=corpus, dry_run=dry_run, force_size=force_size,
        record_noops=record_noops, follow_symlinks=follow_symlinks,
        extract_mode=extract_mode,
        cli_original_path=original_path,
    )
    return _serialize_ingest_result(result)


def files_extract_pending_impl(limit: int = 100) -> dict:
    """Drain leaves with extraction_status='pending'.

    Process up to `limit` leaves in a single pass. Safe to call
    repeatedly; idempotent (already-extracted leaves are not touched).
    Returns counts: {ok, failed, skipped, model_id}.
    """
    from .extract import extract_for_pending_leaves
    return extract_for_pending_leaves(limit=limit)


def files_promote_impl(
    source_uuid: str,
    reason: str = "",
    mapped_type: Optional[str] = None,
    scope: Optional[str] = None,
    importance: float = 0.6,
) -> dict:
    """Promote a fact / leaf / file_summary from files.db to memory.db.

    The source stays in files.db; a copy lands in memory.db with a
    metadata back-pointer. Idempotent: already-promoted UUIDs return
    the existing promotion record without re-writing.
    """
    from .promote import files_promote as _impl
    return _impl(
        source_uuid, reason=reason, mapped_type=mapped_type,
        scope=scope, importance=importance,
    )


def files_promotion_list_impl(
    source_file_node: Optional[str] = None,
    source_superseded: Optional[bool] = None,
    limit: int = 100,
) -> list[dict]:
    """List existing promotions. Filter to a file_node or to drifted-source items."""
    from .promote import files_promotion_list as _impl
    return _impl(
        source_file_node=source_file_node,
        source_superseded=source_superseded,
        limit=limit,
    )


def files_promotable_impl(
    limit: int = 20,
    min_score: float = 0.30,
    corpus: Optional[str] = None,
    include_already_promoted: bool = False,
) -> list[dict]:
    """Surface top promotion candidates by usage-weighted heuristic score.

    Lists facts that have been hit by files_search often enough that the
    user might want to promote them to memory.db. NEVER auto-promotes —
    this is suggestion-only.
    """
    from .promotability import files_promotable
    return files_promotable(
        limit=limit, min_score=min_score, corpus_id=corpus,
        include_already_promoted=include_already_promoted,
    )


def files_dedup_impl(
    threshold: float = 0.92,
    max_pairs: int = 500,
    leaf_limit: int = 10000,
    corpus: Optional[str] = None,
    include_already_detected: bool = False,
) -> dict:
    """Scan leaf embeddings for near-duplicates above cosine threshold.

    Detection-only — pairs land in semantic_dedup_candidates for review.
    No automatic merging. Use files_dedup_list to inspect candidates and
    files_dedup_review to record decisions.
    """
    from .dedup import files_dedup as _impl
    return _impl(
        threshold=threshold, max_pairs=max_pairs, leaf_limit=leaf_limit,
        corpus_id=corpus, include_already_detected=include_already_detected,
    )


def files_dedup_list_impl(
    reviewed: Optional[bool] = False,
    limit: int = 100,
    min_cosine: Optional[float] = None,
) -> list[dict]:
    """List near-duplicate candidate pairs detected by files_dedup."""
    from .dedup import list_dedup_candidates
    return list_dedup_candidates(
        reviewed=reviewed, limit=limit, min_cosine=min_cosine,
    )


def files_dedup_review_impl(
    candidate_uuid: str,
    action: str,
    note: str = "",
) -> dict:
    """Record a review decision for a near-duplicate candidate.

    Action must be 'kept' | 'merged' | 'ignored'. 'merged' is intent-only
    in phase 3; actual leaf merging is a future-phase operation.
    """
    from .dedup import review_dedup_candidate
    return review_dedup_candidate(candidate_uuid, action, note=note)


def files_staleness_review_impl(
    directory: Optional[str] = None,
    corpus: Optional[str] = None,
    include_failed_extraction: bool = True,
    include_drifted_promotions: bool = True,
    include_rename_candidates: bool = True,
    rehash: bool = True,
    limit: int = 200,
) -> dict:
    """Report which files need attention: stale, missing, new, failed, drifted,
    plus rename candidates (missing files whose content reappeared at a new path).

    Returns a JSON-safe dict with six lists and a summary block. The caller
    (interactive tool, batch script, etc.) decides what to do.
    """
    from .staleness import files_staleness_review
    rpt = files_staleness_review(
        directory=directory, corpus_id=corpus,
        include_failed_extraction=include_failed_extraction,
        include_drifted_promotions=include_drifted_promotions,
        include_rename_candidates=include_rename_candidates,
        rehash=rehash,
    )
    return {
        "summary": {
            "directory": rpt.directory,
            "corpus_id": rpt.corpus_id,
            "duration_ms": rpt.duration_ms,
            "scanned_disk_files": rpt.scanned_disk_files,
            "scanned_db_files": rpt.scanned_db_files,
            "stale_count": len(rpt.stale),
            "touched_only_count": len(rpt.touched_only),
            "missing_count": len(rpt.missing),
            "new_count": len(rpt.new),
            "failed_extraction_count": len(rpt.failed_extraction),
            "drifted_promotion_count": len(rpt.drifted_promotions),
            "rename_candidate_count": len(rpt.rename_candidates),
        },
        "stale": [
            {
                "path": s.path, "version": s.last_ingested_version,
                "last_ingest_date": s.last_ingest_date,
                "last_sha": s.last_sha256[:12], "current_sha": s.current_sha256[:12],
                "fact_count": s.fact_count, "promoted_count": s.promoted_count,
                "action": s.suggested_action,
            } for s in rpt.stale[:limit]
        ],
        "touched_only": [
            {"path": s.path, "mtime": s.mtime} for s in rpt.touched_only[:limit]
        ],
        "missing": [
            {
                "path": m.path, "fact_count": m.fact_count,
                "promoted_count": m.promoted_count, "action": m.suggested_action,
            } for m in rpt.missing[:limit]
        ],
        "new": [
            {"path": n.path, "filetype": n.filetype, "size_bytes": n.size_bytes}
            for n in rpt.new[:limit]
        ],
        "failed_extraction": [
            {
                "path": f.path, "failed": f.failed_leaf_count,
                "total": f.total_leaf_count, "last_error": f.last_error,
            } for f in rpt.failed_extraction[:limit]
        ],
        "drifted_promotions": [
            {
                "marker_uuid": d.marker_uuid, "promoted_to": d.promoted_to,
                "source_path": d.source_path,
                "source_superseded_at": d.source_superseded_at,
                "reason": d.reason,
            } for d in rpt.drifted_promotions[:limit]
        ],
        "rename_candidates": [
            {
                "missing_file_node": r.missing_file_node_uuid,
                "missing_path": r.missing_path,
                "new_path": r.new_path,
                "content_sha256": r.content_sha256[:16] + "...",
                "confidence": r.confidence,
                "action": r.suggested_action,
            } for r in rpt.rename_candidates[:limit]
        ],
    }


def files_link_rename_impl(
    missing_file_node_uuid: str,
    new_path: str,
    expect_sha256: Optional[str] = None,
) -> dict:
    """Re-point an existing file_node at a new path (rename / move).

    The content_sha256 must still match (it's a rename, not a content
    change). Use files_ingest with the new path to handle a content
    change instead — that path will properly supersede the prior
    file_node.
    """
    from .staleness import link_rename
    return link_rename(
        missing_file_node_uuid=missing_file_node_uuid,
        new_path=new_path,
        expect_sha256=expect_sha256,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Multi-corpus impls (phase 4)
# ──────────────────────────────────────────────────────────────────────────────
def _corpus_info_to_dict(info) -> dict:
    return {
        "corpus_id": info.corpus_id,
        "settings": info.settings,
        "file_node_count": info.file_node_count,
        "leaf_count": info.leaf_count,
        "created_at": info.created_at,
        "is_default": info.is_default,
    }


def files_corpus_create_impl(
    corpus_id: str,
    description: Optional[str] = None,
    extract_mode: Optional[str] = None,
    scope: Optional[str] = None,
    default: bool = False,
) -> dict:
    """Register a new corpus."""
    from .corpora import corpus_create
    info = corpus_create(
        corpus_id=corpus_id, description=description,
        extract_mode=extract_mode, scope=scope, default=default,
    )
    return _corpus_info_to_dict(info)


def files_corpus_list_impl() -> list[dict]:
    """Enumerate corpora with row counts."""
    from .corpora import corpus_list
    return [_corpus_info_to_dict(i) for i in corpus_list()]


def files_corpus_get_impl(corpus_id: str) -> Optional[dict]:
    """Fetch a single corpus's settings + counts. None if unknown."""
    from .corpora import corpus_get
    info = corpus_get(corpus_id)
    return _corpus_info_to_dict(info) if info else None


def files_corpus_set_impl(
    corpus_id: str,
    description: Optional[str] = None,
    extract_mode: Optional[str] = None,
    scope: Optional[str] = None,
    default: Optional[bool] = None,
    retention_days: Optional[int] = None,
) -> dict:
    """Update settings on an existing corpus (creates settings row if absent)."""
    from .corpora import corpus_set
    info = corpus_set(
        corpus_id=corpus_id, description=description,
        extract_mode=extract_mode, scope=scope, default=default,
        retention_days=retention_days,
    )
    return _corpus_info_to_dict(info)


def files_corpus_delete_impl(corpus_id: str, cascade: bool = False) -> dict:
    """Delete a corpus (and its contents when cascade=True)."""
    from .corpora import corpus_delete
    return corpus_delete(corpus_id=corpus_id, cascade=cascade)


# ──────────────────────────────────────────────────────────────────────────────
# Watch-mode impls (phase 4)
# ──────────────────────────────────────────────────────────────────────────────
def files_watch_once_impl(
    directory: Optional[str] = None,
    corpus: Optional[str] = None,
    agent_id: str = "files_memory.watch",
    cooldown_seconds: float = 3600.0,
) -> dict:
    """Single staleness-review + notify pass; returns counters."""
    from .watch import watch_once
    result = watch_once(
        directory=directory, corpus_id=corpus,
        agent_id=agent_id, cooldown_seconds=cooldown_seconds,
    )
    return {
        "duration_ms": result.duration_ms,
        "stale_count": result.stale_count,
        "new_count": result.new_count,
        "missing_count": result.missing_count,
        "failed_extraction_count": result.failed_extraction_count,
        "rename_candidate_count": result.rename_candidate_count,
        "drifted_promotion_count": result.drifted_promotion_count,
        "notifications_emitted": result.notifications_emitted,
        "notifications_suppressed_by_cooldown": result.notifications_suppressed_by_cooldown,
        "errors": result.errors,
    }


def files_watch_loop_impl(
    directory: Optional[str] = None,
    corpus: Optional[str] = None,
    interval_seconds: float = 300.0,
    agent_id: str = "files_memory.watch",
    cooldown_seconds: float = 3600.0,
    max_cycles: Optional[int] = None,
) -> dict:
    """Blocking polling loop. Long-running; suitable for `python -m … watch`.
    Returns the cycle count after KeyboardInterrupt or max_cycles."""
    from .watch import watch_loop
    cycles = watch_loop(
        directory=directory, corpus_id=corpus,
        interval_seconds=interval_seconds, agent_id=agent_id,
        cooldown_seconds=cooldown_seconds, max_cycles=max_cycles,
    )
    return {"cycles_completed": cycles}


def files_index_impl(
    corpus: Optional[str] = None,
    corpora: Optional[list[str]] = None,
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
      corpus: single-corpus scope filter.
      corpora: list of corpus IDs to fan out across. Overrides corpus.
      filetype: filter to one filetype (e.g. 'markdown', 'pdf').
      directory: filter to files whose absolute path starts here.
      filename_glob: SQL GLOB pattern over filename ('*.md', 'README*').
      include_history: include superseded file_nodes.
      limit: max entries.
    """
    entries = files_index(
        corpus_id=corpus, corpora=corpora, filetype=filetype, directory=directory,
        filename_glob=filename_glob, include_history=include_history,
        limit=limit,
    )
    return [
        {
            "uuid": e.file_node_uuid,
            "filename": e.filename,
            "filetype": e.filetype,
            "path": e.path,
            "original_path": e.original_path,
            "version": e.version_label,
            "date_modified": e.date_modified,
            "summary": e.summary,
            "corpus_id": e.corpus_id,
        }
        for e in entries
    ]


def files_search_impl(
    query: str,
    limit: int = 10,
    corpus: Optional[str] = None,
    corpora: Optional[list[str]] = None,
    filetype: Optional[str] = None,
    include_history: bool = False,
) -> list[dict]:
    """Hybrid search over leaves: FTS5 + vector cosine + RRF.

    Default filters to current (non-superseded) leaves. Pass
    include_history=True for time-travel queries.

    Args:
      corpus: single-corpus scope filter.
      corpora: list of corpus IDs to fan out across. Overrides corpus.

    Returns ranked SearchHit records with text, provenance (file +
    division), and per-channel rank info for debugging.
    """
    hits = _files_search(
        query, limit=limit, corpus_id=corpus, corpora=corpora,
        filetype=filetype, include_history=include_history,
    )
    return [
        {
            "leaf_uuid": h.leaf_uuid,
            "file_node_uuid": h.file_node_uuid,
            "filename": h.filename,
            "path": h.path,
            "original_path": h.original_path,
            "corpus_id": h.corpus_id,
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
        extract_mode: Optional[str] = None,
        original_path: Optional[str] = None,
    ) -> dict:
        """Walk a directory and ingest supported files into files.db.
        Idempotent: same content_sha256 → no-op; changed content →
        new file_node version supersedes prior. Use extract_mode to
        opt into fact extraction; use original_path (or a
        <path>.m3meta.json sidecar) to point search results at a
        source-of-truth file when the ingested file is a conversion."""
        return files_ingest_impl(
            path=path, include=include, exclude=exclude, max_depth=max_depth,
            corpus=corpus, dry_run=dry_run, force_size=force_size,
            record_noops=record_noops,
            extract_mode=extract_mode,
            original_path=original_path,
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

    @mcp.tool()
    def files_extract_pending(limit: int = 100) -> dict:
        """Drain leaves with extraction_status='pending' through the LLM extractor.
        Used after a queue-mode ingest. Safe to call repeatedly."""
        return files_extract_pending_impl(limit=limit)

    @mcp.tool()
    def files_promote(
        source_uuid: str,
        reason: str = "",
        mapped_type: Optional[str] = None,
        scope: Optional[str] = None,
        importance: float = 0.6,
    ) -> dict:
        """Promote (ascend) a fact / leaf / file_summary from files.db to
        memory.db. Source stays untouched; copy lands in memory.db with a
        metadata back-pointer. Idempotent."""
        return files_promote_impl(
            source_uuid=source_uuid, reason=reason, mapped_type=mapped_type,
            scope=scope, importance=importance,
        )

    @mcp.tool()
    def files_promotion_list(
        source_file_node: Optional[str] = None,
        source_superseded: Optional[bool] = None,
        limit: int = 100,
    ) -> list[dict]:
        """List existing promotions. source_superseded=True surfaces
        promotions whose source file has since been superseded —
        candidates for review."""
        return files_promotion_list_impl(
            source_file_node=source_file_node,
            source_superseded=source_superseded,
            limit=limit,
        )

    @mcp.tool()
    def files_promotable(
        limit: int = 20,
        min_score: float = 0.30,
        corpus: Optional[str] = None,
        include_already_promoted: bool = False,
    ) -> list[dict]:
        """List top promotion candidates by usage-weighted heuristic score.
        Suggestion-only; use files_promote to actually ascend any."""
        return files_promotable_impl(
            limit=limit, min_score=min_score, corpus=corpus,
            include_already_promoted=include_already_promoted,
        )

    @mcp.tool()
    def files_dedup(
        threshold: float = 0.92,
        max_pairs: int = 500,
        leaf_limit: int = 10000,
        corpus: Optional[str] = None,
        include_already_detected: bool = False,
    ) -> dict:
        """Scan leaf embeddings for near-duplicates. Detection only —
        pairs land in semantic_dedup_candidates for human review."""
        return files_dedup_impl(
            threshold=threshold, max_pairs=max_pairs, leaf_limit=leaf_limit,
            corpus=corpus, include_already_detected=include_already_detected,
        )

    @mcp.tool()
    def files_dedup_list(
        reviewed: Optional[bool] = False,
        limit: int = 100,
        min_cosine: Optional[float] = None,
    ) -> list[dict]:
        """List near-duplicate candidate pairs with text snippets and paths."""
        return files_dedup_list_impl(
            reviewed=reviewed, limit=limit, min_cosine=min_cosine,
        )

    @mcp.tool()
    def files_dedup_review(
        candidate_uuid: str,
        action: str,
        note: str = "",
    ) -> dict:
        """Record a review decision: 'kept' | 'merged' | 'ignored'."""
        return files_dedup_review_impl(
            candidate_uuid=candidate_uuid, action=action, note=note,
        )

    @mcp.tool()
    def files_staleness_review(
        directory: Optional[str] = None,
        corpus: Optional[str] = None,
        rehash: bool = True,
        limit: int = 200,
    ) -> dict:
        """Compare filesystem against files.db. Surfaces stale, touched-only,
        missing, new, failed-extraction, drifted-promotion files, and rename
        candidates (missing files whose content reappeared elsewhere on disk).
        Report-only: does not modify anything."""
        return files_staleness_review_impl(
            directory=directory, corpus=corpus, rehash=rehash, limit=limit,
        )

    @mcp.tool()
    def files_link_rename(
        missing_file_node_uuid: str,
        new_path: str,
        expect_sha256: Optional[str] = None,
    ) -> dict:
        """Re-point an existing file_node at a new path (rename / move).
        NOT a supersession — content stays identical. Use this only when
        staleness review surfaces a rename candidate."""
        return files_link_rename_impl(
            missing_file_node_uuid=missing_file_node_uuid,
            new_path=new_path,
            expect_sha256=expect_sha256,
        )

    # ── Multi-corpus management (phase 4) ────────────────────────────────
    @mcp.tool()
    def files_corpus_create(
        corpus_id: str,
        description: Optional[str] = None,
        extract_mode: Optional[str] = None,
        scope: Optional[str] = None,
        default: bool = False,
    ) -> dict:
        """Register a new corpus with optional default overrides.
        `default=True` marks this corpus as the installation's default
        (clears the flag on any prior default in the same transaction)."""
        return files_corpus_create_impl(
            corpus_id=corpus_id, description=description,
            extract_mode=extract_mode, scope=scope, default=default,
        )

    @mcp.tool()
    def files_corpus_list() -> list[dict]:
        """Enumerate corpora with row counts."""
        return files_corpus_list_impl()

    @mcp.tool()
    def files_corpus_get(corpus_id: str) -> Optional[dict]:
        """Fetch a single corpus's settings + counts."""
        return files_corpus_get_impl(corpus_id=corpus_id)

    @mcp.tool()
    def files_corpus_set(
        corpus_id: str,
        description: Optional[str] = None,
        extract_mode: Optional[str] = None,
        scope: Optional[str] = None,
        default: Optional[bool] = None,
        retention_days: Optional[int] = None,
    ) -> dict:
        """Update settings for an existing corpus. None args are no-ops.
        Creates the corpus_settings row if absent."""
        return files_corpus_set_impl(
            corpus_id=corpus_id, description=description,
            extract_mode=extract_mode, scope=scope, default=default,
            retention_days=retention_days,
        )

    @mcp.tool()
    def files_corpus_delete(corpus_id: str, cascade: bool = False) -> dict:
        """Delete a corpus's settings row. Cascade=True also deletes its
        file_nodes and all dependent rows — DESTRUCTIVE. Without cascade,
        refuses when the corpus has file_nodes."""
        return files_corpus_delete_impl(corpus_id=corpus_id, cascade=cascade)

    # ── Watch-mode daemon (phase 4) ──────────────────────────────────────
    @mcp.tool()
    def files_watch_once(
        directory: Optional[str] = None,
        corpus: Optional[str] = None,
        agent_id: str = "files_memory.watch",
        cooldown_seconds: float = 3600.0,
    ) -> dict:
        """Single-pass staleness check + notification dispatch. Suitable
        for cron / scheduled runners. Notifications are emitted via the
        memory.db notifications inbox; cooldown suppresses duplicates."""
        return files_watch_once_impl(
            directory=directory, corpus=corpus,
            agent_id=agent_id, cooldown_seconds=cooldown_seconds,
        )


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
    p_ing.add_argument(
        "--mode", choices=["none", "inline", "queue"], default=None,
        dest="extract_mode",
        help="extract mode: none=no facts, inline=sync, queue=defer (drain with `extract`)",
    )
    p_ing.add_argument(
        "--original-path", dest="original_path", default=None,
        help=(
            "pointer to the user-facing source artifact when the ingested "
            "file is a conversion (e.g. .pdf for an ingested .pdf.txt). "
            "Applied to every file in the walk; per-file overrides via "
            "a sidecar <path>.m3meta.json."
        ),
    )

    p_ext = sub.add_parser("extract", help="drain leaves with extraction_status='pending'")
    p_ext.add_argument("--limit", type=int, default=100)

    p_pro = sub.add_parser("promote", help="promote a fact/leaf/file_summary to memory.db")
    p_pro.add_argument("source_uuid")
    p_pro.add_argument("--reason", default="")
    p_pro.add_argument("--type", dest="mapped_type", default=None,
                       help="override memory.db type (fact|knowledge|reference|note|...)")
    p_pro.add_argument("--scope", default=None)
    p_pro.add_argument("--importance", type=float, default=0.6)

    p_pl = sub.add_parser("promotion-list", help="list existing promotions")
    p_pl.add_argument("--file-node", dest="source_file_node", default=None)
    p_pl.add_argument("--drifted", dest="source_superseded", action="store_true",
                      help="only promotions whose source has been superseded")
    p_pl.add_argument("--limit", type=int, default=100)

    p_dd = sub.add_parser("dedup", help="scan for near-duplicate leaves")
    p_dd.add_argument("--threshold", type=float, default=0.92)
    p_dd.add_argument("--max-pairs", type=int, default=500)
    p_dd.add_argument("--leaf-limit", type=int, default=10000)
    p_dd.add_argument("--corpus", default=None)
    p_dd.add_argument("--include-already-detected", action="store_true")

    p_dl = sub.add_parser("dedup-list", help="list near-duplicate candidates")
    p_dl.add_argument("--reviewed", action="store_true")
    p_dl.add_argument("--all", dest="reviewed_all", action="store_true",
                      help="include both reviewed and unreviewed")
    p_dl.add_argument("--limit", type=int, default=100)
    p_dl.add_argument("--min-cosine", type=float, default=None)

    p_dr = sub.add_parser("dedup-review", help="record a review decision")
    p_dr.add_argument("candidate_uuid")
    p_dr.add_argument("--action", required=True, choices=["kept", "merged", "ignored"])
    p_dr.add_argument("--note", default="")

    p_pr = sub.add_parser("promotable", help="list top promotion candidates by usage")
    p_pr.add_argument("--limit", type=int, default=20)
    p_pr.add_argument("--min-score", type=float, default=0.30)
    p_pr.add_argument("--corpus", default=None)
    p_pr.add_argument("--include-promoted", action="store_true",
                      help="include facts that have already been promoted")

    p_sr = sub.add_parser("staleness", help="report stale / missing / new / failed files")
    p_sr.add_argument("--directory", default=None)
    p_sr.add_argument("--corpus", default=None)
    p_sr.add_argument("--no-rehash", action="store_true",
                      help="mtime-only classification (cheaper but less accurate)")
    p_sr.add_argument("--limit", type=int, default=200)

    p_lr = sub.add_parser("link-rename", help="re-point a file_node at a new path")
    p_lr.add_argument("file_node_uuid")
    p_lr.add_argument("new_path")
    p_lr.add_argument("--expect-sha256", default=None)

    p_sea = sub.add_parser("search", help="hybrid search over leaves")
    p_sea.add_argument("query")
    p_sea.add_argument("--limit", type=int, default=10)
    p_sea.add_argument("--corpus", default=None,
                       help="single-corpus filter (default: all current)")
    p_sea.add_argument("--corpora", default=None,
                       help="comma-separated corpus IDs for fan-out search "
                            "(overrides --corpus)")
    p_sea.add_argument("--filetype", default=None)
    p_sea.add_argument("--include-history", action="store_true")

    p_idx = sub.add_parser("index", help="file-level summary triage")
    p_idx.add_argument("--corpus", default=None)
    p_idx.add_argument("--corpora", default=None,
                       help="comma-separated corpus IDs for fan-out index "
                            "(overrides --corpus)")
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

    # ── Multi-corpus subcommands (phase 4) ───────────────────────────────
    p_cc = sub.add_parser("corpus-create", help="register a new corpus")
    p_cc.add_argument("corpus_id")
    p_cc.add_argument("--description", default=None)
    p_cc.add_argument("--extract-mode", choices=["none", "inline", "queue"], default=None,
                      dest="extract_mode")
    p_cc.add_argument("--scope", default=None)
    p_cc.add_argument("--default", action="store_true",
                      help="mark this corpus as the installation default")

    sub.add_parser("corpus-list", help="enumerate corpora with row counts")

    p_cg = sub.add_parser("corpus-get", help="show one corpus's settings + counts")
    p_cg.add_argument("corpus_id")

    p_cs = sub.add_parser("corpus-set", help="update a corpus's settings")
    p_cs.add_argument("corpus_id")
    p_cs.add_argument("--description", default=None)
    p_cs.add_argument("--extract-mode", choices=["none", "inline", "queue"], default=None,
                      dest="extract_mode")
    p_cs.add_argument("--scope", default=None)
    p_cs.add_argument("--default", dest="set_default", action="store_true",
                      help="set as installation default")
    p_cs.add_argument("--no-default", dest="unset_default", action="store_true",
                      help="clear the default flag")
    p_cs.add_argument("--retention-days", type=int, default=None)

    p_cd = sub.add_parser("corpus-delete", help="remove a corpus")
    p_cd.add_argument("corpus_id")
    p_cd.add_argument("--cascade", action="store_true",
                      help="also delete every file_node in the corpus (DESTRUCTIVE)")

    # ── Watch-mode subcommands (phase 4) ─────────────────────────────────
    p_wo = sub.add_parser("watch-once", help="single staleness + notify pass")
    p_wo.add_argument("--directory", default=None)
    p_wo.add_argument("--corpus", default=None)
    p_wo.add_argument("--agent-id", default="files_memory.watch", dest="agent_id")
    p_wo.add_argument("--cooldown-seconds", type=float, default=3600.0,
                      dest="cooldown_seconds")

    p_wl = sub.add_parser("watch", help="long-running staleness poller")
    p_wl.add_argument("--directory", default=None)
    p_wl.add_argument("--corpus", default=None)
    p_wl.add_argument("--interval-seconds", type=float, default=300.0,
                      dest="interval_seconds")
    p_wl.add_argument("--agent-id", default="files_memory.watch", dest="agent_id")
    p_wl.add_argument("--cooldown-seconds", type=float, default=3600.0,
                      dest="cooldown_seconds")
    p_wl.add_argument("--max-cycles", type=int, default=None, dest="max_cycles")

    args = p.parse_args()

    if args.cmd == "ingest":
        r = files_ingest_impl(
            path=args.path, include=args.include, exclude=args.exclude,
            max_depth=args.max_depth, corpus=args.corpus,
            dry_run=args.dry_run, force_size=args.force_size,
            record_noops=args.record_noops,
            extract_mode=args.extract_mode,
            original_path=args.original_path,
        )
    elif args.cmd == "extract":
        r = files_extract_pending_impl(limit=args.limit)
    elif args.cmd == "promote":
        r = files_promote_impl(
            source_uuid=args.source_uuid, reason=args.reason,
            mapped_type=args.mapped_type, scope=args.scope,
            importance=args.importance,
        )
    elif args.cmd == "promotion-list":
        r = files_promotion_list_impl(
            source_file_node=args.source_file_node,
            source_superseded=(True if args.source_superseded else None),
            limit=args.limit,
        )
    elif args.cmd == "dedup":
        r = files_dedup_impl(
            threshold=args.threshold, max_pairs=args.max_pairs,
            leaf_limit=args.leaf_limit, corpus=args.corpus,
            include_already_detected=args.include_already_detected,
        )
    elif args.cmd == "dedup-list":
        reviewed_arg: Optional[bool] = False
        if args.reviewed_all:
            reviewed_arg = None
        elif args.reviewed:
            reviewed_arg = True
        r = files_dedup_list_impl(
            reviewed=reviewed_arg, limit=args.limit, min_cosine=args.min_cosine,
        )
    elif args.cmd == "dedup-review":
        r = files_dedup_review_impl(
            candidate_uuid=args.candidate_uuid, action=args.action, note=args.note,
        )
    elif args.cmd == "promotable":
        r = files_promotable_impl(
            limit=args.limit, min_score=args.min_score, corpus=args.corpus,
            include_already_promoted=args.include_promoted,
        )
    elif args.cmd == "staleness":
        r = files_staleness_review_impl(
            directory=args.directory, corpus=args.corpus,
            rehash=(not args.no_rehash), limit=args.limit,
        )
    elif args.cmd == "link-rename":
        r = files_link_rename_impl(
            missing_file_node_uuid=args.file_node_uuid,
            new_path=args.new_path,
            expect_sha256=args.expect_sha256,
        )
    elif args.cmd == "search":
        corpora_list = (
            [c.strip() for c in args.corpora.split(",") if c.strip()]
            if getattr(args, "corpora", None) else None
        )
        r = files_search_impl(
            query=args.query, limit=args.limit, corpus=args.corpus,
            corpora=corpora_list,
            filetype=args.filetype, include_history=args.include_history,
        )
    elif args.cmd == "index":
        corpora_list = (
            [c.strip() for c in args.corpora.split(",") if c.strip()]
            if getattr(args, "corpora", None) else None
        )
        r = files_index_impl(
            corpus=args.corpus, corpora=corpora_list,
            filetype=args.filetype,
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
    elif args.cmd == "corpus-create":
        r = files_corpus_create_impl(
            corpus_id=args.corpus_id, description=args.description,
            extract_mode=args.extract_mode, scope=args.scope,
            default=args.default,
        )
    elif args.cmd == "corpus-list":
        r = files_corpus_list_impl()
    elif args.cmd == "corpus-get":
        r = files_corpus_get_impl(corpus_id=args.corpus_id)
    elif args.cmd == "corpus-set":
        # --default and --no-default are mutually exclusive; treat both
        # off as "don't touch the default flag".
        default_arg: Optional[bool] = None
        if args.set_default and args.unset_default:
            raise SystemExit("--default and --no-default are mutually exclusive")
        if args.set_default:
            default_arg = True
        elif args.unset_default:
            default_arg = False
        r = files_corpus_set_impl(
            corpus_id=args.corpus_id, description=args.description,
            extract_mode=args.extract_mode, scope=args.scope,
            default=default_arg, retention_days=args.retention_days,
        )
    elif args.cmd == "corpus-delete":
        r = files_corpus_delete_impl(corpus_id=args.corpus_id, cascade=args.cascade)
    elif args.cmd == "watch-once":
        r = files_watch_once_impl(
            directory=args.directory, corpus=args.corpus,
            agent_id=args.agent_id, cooldown_seconds=args.cooldown_seconds,
        )
    elif args.cmd == "watch":
        r = files_watch_loop_impl(
            directory=args.directory, corpus=args.corpus,
            interval_seconds=args.interval_seconds,
            agent_id=args.agent_id, cooldown_seconds=args.cooldown_seconds,
            max_cycles=args.max_cycles,
        )
    else:
        p.print_help()
        return 2

    print(_json.dumps(r, indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main())
