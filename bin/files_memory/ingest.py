"""Ingestion orchestrator — walk → chunk → summarize → embed → write.

The heart of phase 1. Stitches every other module together. Idempotent
by design: re-running on an unchanged corpus is a no-op (skipped by
content_sha256). Re-running on a changed file produces a new file_node
version with the prior superseded.

Public API:
    ingest_path(root, **opts) -> IngestResult
    IngestResult — dataclass with counts, run_id, failures
    ingest_one_file(walk_entry, run_id, corpus_id, ...) -> FileIngestResult

CLI entry: `python -m files_memory.ingest <root>` (added in tools.py).
"""
from __future__ import annotations

import logging
import os
import platform
import socket
import time
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import config
from .chunkers import Leaf, chunk_file, chunker_version
from .db import _db
from .embed import (
    embed_texts,
    mark_leaves_embedded,
    write_file_embedding,
    write_leaf_embedding,
)
from .identity import (
    file_content_sha256,
    filetype_for,
    resolve_identity_key,
)
from .summarize import summarize_file, summarize_leaf
from .walker import WalkEntry, WalkStats, walk

logger = logging.getLogger("files_memory.ingest")

# Divisions that get a leaf-level summary in phase 1. Fine divisions
# (sub-headings, paragraph windows) skip the summary to bound row count.
_COARSE_DIVISIONS = frozenset({"page", "slide", "heading", "cell"})


@dataclass
class FileIngestResult:
    """Outcome of ingesting a single file."""
    path: str
    file_node_uuid: Optional[str]
    status: str           # 'created'|'unchanged_skipped'|'superseded'|'failed'
    leaf_count: int = 0
    fact_count: int = 0
    chars_embedded: int = 0
    duration_ms: int = 0
    reason: Optional[str] = None
    superseded_prior: Optional[str] = None  # uuid of prior version, if any


@dataclass
class IngestResult:
    """Outcome of a full directory walk + ingest."""
    run_id: str
    root: str
    started_at: float
    duration_ms: int = 0
    walk_stats: Optional[WalkStats] = None
    files_created: int = 0
    files_superseded: int = 0
    files_unchanged: int = 0
    files_failed: int = 0
    leaves_written: int = 0
    leaves_embedded: int = 0
    facts_extracted: int = 0
    failures: list[dict] = field(default_factory=list)
    per_file: list[FileIngestResult] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _iso_utc(ts: float | None = None) -> str:
    """ISO 8601 UTC string. Stable across timezones."""
    import datetime as _dt
    if ts is None:
        return _dt.datetime.now(_dt.timezone.utc).isoformat()
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()


def _source_host() -> str:
    """Identifier for the host that produced this ingest record."""
    return platform.node() or socket.gethostname() or "unknown"


def _next_version_label(conn, identity_key: str) -> str:
    """Resolve the next version label for this identity_key.

    Convention: `ingest-N` where N is `1 + count of prior versions`.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM file_nodes WHERE identity_key = ?",
        (identity_key,),
    ).fetchone()
    n = (row[0] if row else 0) + 1
    return f"ingest-{n}"


def _find_current_version(conn, identity_key: str) -> Optional[dict]:
    """Return the current (non-superseded) file_node for this identity_key.

    Returns a dict view (sqlite3.Row → dict) of the row, or None if no
    version exists yet.
    """
    row = conn.execute(
        "SELECT uuid, content_sha256, version_label, paths_seen, path_absolute "
        "FROM file_nodes "
        "WHERE identity_key = ? AND superseded_by IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (identity_key,),
    ).fetchone()
    return dict(row) if row else None


def _read_file_text(path: str, max_bytes: int) -> Optional[str]:
    """Read a file as UTF-8 with error replacement. Returns None on failure.

    Caps at max_bytes to avoid loading huge files into RAM during text
    extraction. Binary files are not expected here (walker filters them)
    but we use errors='replace' as a safety net.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(max_bytes)
        return raw.decode("utf-8", errors="replace")
    except OSError as e:
        logger.warning("read failed for %s: %s", path, e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Single-file ingest
# ──────────────────────────────────────────────────────────────────────────────
def ingest_one_file(
    entry: WalkEntry,
    run_id: str,
    corpus_id: str,
    *,
    version_label_override: Optional[str] = None,
    record_noops: bool = False,
    extract_mode: str = "none",
    db_path: Optional[str] = None,
) -> FileIngestResult:
    """Ingest a single file. The atomic unit of the pipeline.

    Transactional: either the file_node + ingestion_run + leaves all land,
    or none do. Embedding writes happen inside the same transaction so
    `embedded=1` is consistent with the existence of an embedding row.

    Idempotency:
      - Same content_sha256 as the current version → no-op (unless
        record_noops, which writes an 'unchanged_skipped' ingestion_run
        for audit).
      - Different content → new file_node, prior superseded.

    Extraction modes:
      - 'none'   — no fact extraction; leaves left with status='pending'
                   only if the caller plans a later queue drain. Default.
      - 'inline' — extract per-leaf sync inside the same transaction.
                   Slower but immediately queryable. Falls back to
                   marking failed/skipped leaves if LLM is unavailable.
      - 'queue'  — leaves get status='pending'; a separate drain pass
                   (files_extract_pending) processes them. The walk
                   itself stays fast.

    Args:
        entry: WalkEntry from the walker.
        run_id: shared across all files in this ingest invocation.
        corpus_id: which corpus this file belongs to.
        version_label_override: explicit label; usually None.
        record_noops: write a no-op ingestion_run row even for unchanged
            files. Off by default — keeps tables clean.
        extract_mode: 'none' | 'inline' | 'queue'.
        db_path: target files.db (None = use config.FILES_DB_PATH).
    """
    t_start = time.perf_counter()
    path = entry.path

    # 1. Compute sha256 + identity. Cheap; do before reading full file.
    try:
        content_sha = file_content_sha256(path)
    except OSError as e:
        return FileIngestResult(
            path=path, file_node_uuid=None, status="failed",
            reason=f"hash failed: {e}",
            duration_ms=int((time.perf_counter() - t_start) * 1000),
        )

    # 2. Read the file text. PDFs and other binary filetypes get None
    #    here; the chunker reads them itself. For text-shaped filetypes
    #    we pre-load so identity.detect_m3_doc_id can use it without
    #    a second read.
    text: Optional[str] = None
    if entry.filetype != "pdf":
        text = _read_file_text(path, config.FILES_MAX_FILE_BYTES)
        if text is None:
            return FileIngestResult(
                path=path, file_node_uuid=None, status="failed",
                reason="read failed",
                duration_ms=int((time.perf_counter() - t_start) * 1000),
            )

    identity_key = resolve_identity_key(path, text=text)

    # 3. Check for prior version. The supersession decision happens here.
    with _db(db_path) as conn:
        prior = _find_current_version(conn, identity_key)
        if prior and prior["content_sha256"] == content_sha:
            # Idempotent no-op.
            if record_noops:
                _record_noop_run(conn, prior["uuid"], run_id, entry)
            return FileIngestResult(
                path=path, file_node_uuid=prior["uuid"],
                status="unchanged_skipped",
                duration_ms=int((time.perf_counter() - t_start) * 1000),
                reason="content_sha256 matches current version",
            )

        # 4. Chunk the file. Done outside the chunking step's transaction
        #    handler so a chunker failure doesn't poison the DB.
        try:
            leaves = list(chunk_file(path, entry.filetype, text=text))
        except Exception as e:
            logger.exception("chunker raised for %s: %s", path, e)
            return FileIngestResult(
                path=path, file_node_uuid=None, status="failed",
                reason=f"chunker raised: {type(e).__name__}: {e}",
                duration_ms=int((time.perf_counter() - t_start) * 1000),
            )

        if not leaves:
            # Empty chunker output. Plan §13: skip + log, surfaces in
            # staleness review later. We still create a file_node so the
            # file is *known* — the empty content branch is the signal.
            logger.info("chunker yielded 0 leaves for %s", path)

        # 5. Summarize the file. Use raw text where available; else
        #    concatenate the leaf texts (PDF case).
        summary_input = text if text else "\n\n".join(l.text for l in leaves[:20])
        file_summary, file_summary_used_llm = summarize_file(
            summary_input, entry.filename, entry.filetype,
        )

        # 6. Insert file_node + ingestion_run + leaves in one transaction.
        file_node_uuid = str(_uuid.uuid4())
        version_label = version_label_override or _next_version_label(conn, identity_key)

        # Build paths_seen — start with current path; prior versions
        # contribute their paths_seen if identity_key matches.
        import json as _json
        paths_seen = [os.path.abspath(path)]
        if prior:
            try:
                prior_paths = _json.loads(prior.get("paths_seen") or "[]")
                for p in prior_paths:
                    if p not in paths_seen:
                        paths_seen.append(p)
                if prior.get("path_absolute") and prior["path_absolute"] not in paths_seen:
                    paths_seen.append(prior["path_absolute"])
            except (ValueError, TypeError):
                pass

        # Insert the new file_node.
        conn.execute(
            "INSERT INTO file_nodes("
            "uuid, identity_key, filename, filetype, path_absolute, path_repo_relative, "
            "size_bytes, content_sha256, date_created, date_modified, source_host, "
            "version_label, supersedes, paths_seen, corpus_id, file_summary, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_node_uuid, identity_key, entry.filename, entry.filetype,
                os.path.abspath(path), entry.repo_relative,
                entry.size_bytes, content_sha,
                _iso_utc(entry.ctime), _iso_utc(entry.mtime), _source_host(),
                version_label,
                prior["uuid"] if prior else None,
                _json.dumps(paths_seen),
                corpus_id,
                file_summary,
                _json.dumps({
                    "file_summary_used_llm": file_summary_used_llm,
                    "ingester_pid": os.getpid(),
                }),
            ),
        )

        # Supersede prior. The chain is bidirectional: supersedes on the
        # new row, superseded_by on the old.
        superseded_prior = None
        if prior:
            conn.execute(
                "UPDATE file_nodes "
                "SET superseded_by = ?, superseded_at = ?, supersession_reason = ? "
                "WHERE uuid = ?",
                (file_node_uuid, _iso_utc(), "content_changed", prior["uuid"]),
            )
            # Record the supersession edge for graph traversal.
            conn.execute(
                "INSERT OR IGNORE INTO memory_links(src_uuid, dst_uuid, edge_type) "
                "VALUES (?, ?, 'supersedes')",
                (file_node_uuid, prior["uuid"]),
            )
            superseded_prior = prior["uuid"]

        # Ingestion run record.
        run_uuid = str(_uuid.uuid4())
        chunker_ver = chunker_version(entry.filetype)
        # extractor_version is recorded ONLY if we actually plan to extract.
        # For mode='none' we leave it NULL — staleness review then knows
        # these leaves haven't been considered for extraction yet.
        run_extractor_version = (
            config.EXTRACTOR_VERSION if extract_mode != "none" else None
        )
        conn.execute(
            "INSERT INTO ingestion_runs("
            "uuid, file_node, run_id, ingest_date, ingester_version, "
            "chunker_version, extractor_version, extract_mode, model_id, "
            "chunk_count, leaf_count, status, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'ok', ?)",
            (
                run_uuid, file_node_uuid, run_id, _iso_utc(),
                config.INGESTER_VERSION, chunker_ver, run_extractor_version,
                extract_mode,
                len(leaves), len(leaves),
                _json.dumps({"filetype": entry.filetype}),
            ),
        )

        # Per-leaf extraction_status starts as:
        #   'skipped'  if extract_mode == 'none' (don't queue-drain these)
        #   'pending'  if extract_mode in {'inline', 'queue'} (work to do)
        # Inline mode then advances them to 'ok'/'failed' below.
        initial_status = "pending" if extract_mode != "none" else "skipped"

        # Insert leaves. Collect (uuid, text, summary_text) for batch embed.
        leaf_records: list[tuple[str, str, Optional[str]]] = []
        for leaf in leaves:
            leaf_uuid = str(_uuid.uuid4())
            wants_summary = leaf.division_type in _COARSE_DIVISIONS
            leaf_summary_text = None
            if wants_summary:
                summary_text, _ = summarize_leaf(leaf.text, file_summary=file_summary)
                leaf_summary_text = summary_text or None

            text_hash = _sha256_short(leaf.text)
            conn.execute(
                "INSERT INTO leaves("
                "uuid, file_node, ingestion_run, division_type, division_id, "
                "division_label, text, text_sha256, char_range_start, "
                "char_range_end, leaf_summary, boundary_confidence, truncated, "
                "extraction_status, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    leaf_uuid, file_node_uuid, run_uuid,
                    leaf.division_type, leaf.division_id, leaf.division_label,
                    leaf.text, text_hash,
                    leaf.char_range_start, leaf.char_range_end,
                    leaf_summary_text,
                    leaf.boundary_confidence,
                    1 if leaf.truncated else 0,
                    initial_status,
                    _json.dumps(leaf.extra or {}),
                ),
            )
            leaf_records.append((leaf_uuid, leaf.text, leaf_summary_text))

        # 7. Embed. The expensive step. Done inside the transaction so
        #    `embedded=1` is consistent with row presence.
        if leaf_records:
            texts_to_embed = [t for _, t, _ in leaf_records]
            try:
                vecs = embed_texts(texts_to_embed)
            except Exception as e:
                logger.warning("embed batch failed for %s: %s", path, e)
                vecs = [(None, "")] * len(leaf_records)

            embedded_count = 0
            for (luuid, _, _), (vec, model) in zip(leaf_records, vecs):
                if vec is not None:
                    write_leaf_embedding(conn, luuid, "text", vec, model)
                    embedded_count += 1

            # Leaf summaries that exist get a separate embedding.
            summary_pairs = [(luuid, s) for luuid, _, s in leaf_records if s]
            if summary_pairs:
                try:
                    sum_vecs = embed_texts([s for _, s in summary_pairs])
                    for (luuid, _), (vec, model) in zip(summary_pairs, sum_vecs):
                        if vec is not None:
                            write_leaf_embedding(conn, luuid, "summary", vec, model)
                except Exception as e:
                    logger.warning("leaf-summary embed batch failed: %s", e)

            mark_leaves_embedded(conn, [r[0] for r in leaf_records])
        else:
            embedded_count = 0

        # File-summary embedding.
        if file_summary:
            try:
                fs_vecs = embed_texts([file_summary])
                if fs_vecs:
                    vec, model = fs_vecs[0]
                    if vec is not None:
                        write_file_embedding(conn, file_node_uuid, vec, model)
            except Exception as e:
                logger.warning("file-summary embed failed for %s: %s", path, e)

        # 8. Inline extraction (mode='inline' only). For mode='queue' the
        #    leaves stay 'pending' and a separate drain handles them.
        fact_count = 0
        if extract_mode == "inline" and leaf_records:
            try:
                from .extract import (
                    extract_facts_for_leaf, write_extraction_result, llm_available
                )
                if llm_available():
                    model_id = (
                        os.environ.get("M3_FILES_EXTRACT_MODEL")
                        or os.environ.get("M3_FILES_SUMMARY_MODEL")
                        or "unknown"
                    )
                    for leaf_uuid, leaf_text, _ in leaf_records:
                        # division_type is the chunker's label; not used by
                        # the extractor today but passed through for prompt
                        # specialization later.
                        result = extract_facts_for_leaf(
                            leaf_uuid, leaf_text,
                            file_summary=file_summary,
                        )
                        write_extraction_result(
                            conn, result,
                            file_node_uuid=file_node_uuid,
                            ingestion_run_uuid=run_uuid,
                            extractor_version=config.EXTRACTOR_VERSION or "p2.0.0",
                            model_id=model_id,
                        )
                        if result.status == "ok":
                            fact_count += result.fact_count
                else:
                    # No LLM: mark every leaf 'failed' with reason logged.
                    for leaf_uuid, _, _ in leaf_records:
                        conn.execute(
                            "UPDATE leaves SET extraction_status = 'failed' "
                            "WHERE uuid = ?", (leaf_uuid,),
                        )
                    logger.warning(
                        "inline extraction requested but no LLM endpoint configured; "
                        "leaves marked extraction_status='failed' for %s", path,
                    )
            except Exception as e:
                logger.exception("inline extraction step failed for %s: %s", path, e)

        duration_ms = int((time.perf_counter() - t_start) * 1000)
        conn.execute(
            "UPDATE ingestion_runs SET duration_ms = ?, fact_count = ? WHERE uuid = ?",
            (duration_ms, fact_count, run_uuid),
        )

    return FileIngestResult(
        path=path,
        file_node_uuid=file_node_uuid,
        status="superseded" if prior else "created",
        leaf_count=len(leaves),
        fact_count=fact_count,
        chars_embedded=sum(len(t) for _, t, _ in leaf_records) if leaf_records else 0,
        duration_ms=duration_ms,
        superseded_prior=superseded_prior,
    )


def _record_noop_run(conn, file_node_uuid: str, run_id: str, entry: WalkEntry) -> None:
    """Write an 'unchanged_skipped' ingestion_run for audit purposes."""
    import json as _json
    conn.execute(
        "INSERT INTO ingestion_runs("
        "uuid, file_node, run_id, ingester_version, chunker_version, "
        "extractor_version, extract_mode, leaf_count, status, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, 'none', 0, 'unchanged_skipped', ?)",
        (
            str(_uuid.uuid4()), file_node_uuid, run_id,
            config.INGESTER_VERSION, chunker_version(entry.filetype),
            config.EXTRACTOR_VERSION,
            _json.dumps({"filetype": entry.filetype}),
        ),
    )


def _sha256_short(text: str) -> str:
    import hashlib as _h
    return _h.sha256(text.encode("utf-8")).hexdigest()


# ──────────────────────────────────────────────────────────────────────────────
# Whole-directory orchestrator
# ──────────────────────────────────────────────────────────────────────────────
def ingest_path(
    root: str,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_depth: int | None = None,
    follow_symlinks: bool | None = None,
    corpus_id: str | None = None,
    dry_run: bool = False,
    force_size: bool = False,
    record_noops: bool = False,
    extract_mode: str | None = None,
    repo_root: str | None = None,
    progress_cb=None,
    db_path: Optional[str] = None,
) -> IngestResult:
    """Walk a directory and ingest every surviving file.

    Args:
        root: directory to walk.
        include / exclude / max_depth / follow_symlinks / force_size:
            passed through to walker.walk().
        corpus_id: scope tag (default config.FILES_DEFAULT_CORPUS).
        dry_run: walk + count but DO NOT write to DB.
        record_noops: log unchanged-file ingestion records for audit.
        extract_mode: 'none' | 'inline' | 'queue'. None → config default.
        repo_root: alternate root for repo_relative computation.
        progress_cb: optional callable(idx, total_so_far, FileIngestResult).
        db_path: target files.db.

    Returns:
        IngestResult with counts and per-file results.
    """
    if extract_mode is None:
        extract_mode = config.DEFAULT_EXTRACT_MODE
    if extract_mode not in {"none", "inline", "queue"}:
        raise ValueError(f"extract_mode must be 'none'|'inline'|'queue', got: {extract_mode!r}")
    run_id = f"run_{int(time.time())}_{_uuid.uuid4().hex[:8]}"
    started = time.perf_counter()
    walk_stats = WalkStats()
    result = IngestResult(run_id=run_id, root=os.path.abspath(root),
                          started_at=time.time(), walk_stats=walk_stats)
    corpus = corpus_id or config.FILES_DEFAULT_CORPUS

    if dry_run:
        for _ in walk(root, include=include, exclude=exclude,
                      max_depth=max_depth, follow_symlinks=follow_symlinks,
                      force_size=force_size, stats=walk_stats,
                      repo_root=repo_root):
            pass
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        return result

    idx = 0
    for entry in walk(root, include=include, exclude=exclude,
                      max_depth=max_depth, follow_symlinks=follow_symlinks,
                      force_size=force_size, stats=walk_stats,
                      repo_root=repo_root):
        idx += 1
        if idx > config.FILES_MAX_FILES_PER_INGEST:
            result.failures.append({
                "path": entry.path,
                "reason": f"per-ingest file cap reached ({config.FILES_MAX_FILES_PER_INGEST})",
            })
            break

        try:
            fr = ingest_one_file(
                entry, run_id, corpus,
                record_noops=record_noops,
                extract_mode=extract_mode,
                db_path=db_path,
            )
        except Exception as e:
            logger.exception("ingest_one_file raised for %s", entry.path)
            fr = FileIngestResult(
                path=entry.path, file_node_uuid=None, status="failed",
                reason=f"{type(e).__name__}: {e}",
            )
            result.failures.append({"path": entry.path, "reason": fr.reason})

        result.per_file.append(fr)
        if fr.status == "created":
            result.files_created += 1
        elif fr.status == "superseded":
            result.files_superseded += 1
        elif fr.status == "unchanged_skipped":
            result.files_unchanged += 1
        elif fr.status == "failed":
            result.files_failed += 1

        result.leaves_written += fr.leaf_count
        result.facts_extracted += fr.fact_count
        if fr.chars_embedded:
            result.leaves_embedded += fr.leaf_count  # approximation

        if progress_cb:
            try:
                progress_cb(idx, len(result.per_file), fr)
            except Exception:
                pass

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result
