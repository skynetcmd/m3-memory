"""Staleness review — surface files that need re-ingestion attention.

For a given directory or scope, walks the filesystem and compares
against files.db. Surfaces:

  - stale       : mtime > last ingestion AND sha256 changed → re-ingest
  - touched     : mtime > last ingestion, sha256 UNCHANGED  → skip (touch only)
  - missing     : in files.db but no longer on disk           → mark retired?
  - new         : on disk but never ingested                  → ingest
  - failed      : files with failed-extraction leaves         → retry
  - drifted-promos: promoted memories whose source is superseded
                    → review with files_promotion_review

The helper is a SEPARATE concern from the core ingester. It just
inspects. Acting on its output is the caller's job (interactive UI,
batch flags, or just a report).

Public API:
    files_staleness_review(directory, ...) -> StalenessReport
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from .db import _db
from .identity import file_content_sha256, file_content_sha256_batch

logger = logging.getLogger("files_memory.staleness")


@dataclass
class StaleFile:
    """One file flagged as stale (changed since last ingest)."""
    path: str
    identity_key: str
    last_ingested_version: str          # e.g. "ingest-2"
    last_ingest_date: str               # ISO 8601
    last_sha256: str
    current_sha256: str
    mtime: str                          # ISO 8601
    fact_count: int
    promoted_count: int                 # facts/leaves/summary already promoted
    suggested_action: str               # "re-ingest" | "skip-touched" | etc.


@dataclass
class MissingFile:
    """File in files.db, no longer on disk."""
    path: str
    file_node_uuid: str
    last_ingest_date: str
    fact_count: int
    promoted_count: int
    suggested_action: str = "mark_retired"


@dataclass
class NewFile:
    """File on disk, never ingested."""
    path: str
    size_bytes: int
    mtime: str
    filetype: str


@dataclass
class FailedExtraction:
    """File with at least one extraction_status='failed' leaf."""
    path: str
    file_node_uuid: str
    failed_leaf_count: int
    total_leaf_count: int
    last_error: Optional[str]


@dataclass
class DriftedPromotion:
    """Promoted memory whose source has been superseded."""
    marker_uuid: str
    promoted_to: str
    source_memory: str
    source_path: str
    source_superseded_at: str
    reason: Optional[str]


@dataclass
class RenameCandidate:
    """A pair of (missing, new) files that look like a rename / move.

    Phase 3.4: detected by exact sha256 match between an on-disk file
    that has never been ingested and a file_node whose path is no
    longer on disk. Always user-confirmed — `files_link_rename` does
    the explicit linking.
    """
    missing_file_node_uuid: str
    missing_path: str
    new_path: str
    content_sha256: str
    confidence: str  # 'exact_sha' for now; phase 4 may add 'fuzzy'
    suggested_action: str = "link"


@dataclass
class StalenessReport:
    """Full review output."""
    directory: Optional[str]
    corpus_id: Optional[str]
    stale: list[StaleFile] = field(default_factory=list)
    touched_only: list[StaleFile] = field(default_factory=list)
    missing: list[MissingFile] = field(default_factory=list)
    new: list[NewFile] = field(default_factory=list)
    failed_extraction: list[FailedExtraction] = field(default_factory=list)
    drifted_promotions: list[DriftedPromotion] = field(default_factory=list)
    rename_candidates: list[RenameCandidate] = field(default_factory=list)
    scanned_disk_files: int = 0
    scanned_db_files: int = 0
    duration_ms: int = 0


# ──────────────────────────────────────────────────────────────────────────────
# Implementation
# ──────────────────────────────────────────────────────────────────────────────
def files_staleness_review(
    directory: Optional[str] = None,
    *,
    corpus_id: Optional[str] = None,
    include_failed_extraction: bool = True,
    include_drifted_promotions: bool = True,
    include_rename_candidates: bool = True,
    rehash: bool = True,
    db_path: Optional[str] = None,
) -> StalenessReport:
    """Compare filesystem state against files.db.

    Args:
        directory: if set, restrict review to files under this directory.
            Otherwise scan all file_nodes in the DB (and the directories
            their paths_seen records mention).
        corpus_id: scope filter.
        include_failed_extraction: surface extraction-failure files.
        include_drifted_promotions: surface promoted-memory drift.
        rehash: when True, recompute sha256 for candidate stale files
            (slow but accurate). When False, mtime alone classifies —
            cheap but may flag touched-only as stale.
        db_path: target files.db.
    """
    import time
    t0 = time.perf_counter()
    report = StalenessReport(directory=directory, corpus_id=corpus_id)

    with _db(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # 1. Load current file_nodes scoped by corpus + directory.
        sql_parts = [
            "SELECT uuid, path_absolute, identity_key, content_sha256, "
            "       date_modified, version_label, "
            "       (SELECT MAX(ingest_date) FROM ingestion_runs "
            "        WHERE file_node = fn.uuid) AS last_ingest_date "
            "FROM file_nodes fn "
            "WHERE superseded_by IS NULL"
        ]
        params: list = []
        if corpus_id:
            sql_parts.append("AND corpus_id = ?")
            params.append(corpus_id)
        if directory:
            sql_parts.append("AND path_absolute LIKE ?")
            params.append(os.path.abspath(directory).replace("%", "[%]") + "%")
        db_rows = conn.execute(" ".join(sql_parts), params).fetchall()
        report.scanned_db_files = len(db_rows)

        # Index db rows by path for set-difference later.
        db_by_path: dict[str, sqlite3.Row] = {r["path_absolute"]: r for r in db_rows}

        # 2. Scan the filesystem for `directory`. If no directory given,
        #    use the parent dirs of every db_by_path entry — bounds the scan.
        disk_paths: set[str] = set()
        scan_roots: list[str] = []
        if directory:
            scan_roots.append(os.path.abspath(directory))
        else:
            seen_parents: set[str] = set()
            for p in db_by_path:
                parent = os.path.dirname(p)
                if parent and parent not in seen_parents:
                    seen_parents.add(parent)
                    if os.path.isdir(parent):
                        scan_roots.append(parent)

        for root in scan_roots:
            # Use the walker so we get the same filtering as the ingester.
            try:
                from .walker import walk
                for entry in walk(root):
                    disk_paths.add(entry.path)
            except Exception as e:
                logger.debug("walker failed at %s: %s", root, e)
        report.scanned_disk_files = len(disk_paths)

        # 2b. Pre-pass: collect the paths whose mtime bumped so we can hash
        #     them all at once. Batch hashing routes through the native
        #     rayon-parallel m3_core_rs.hash_files (GIL released), which is
        #     far faster than a serial per-file loop on large stale sets.
        #     Behavior is identical — same digests, same skip-on-error — just
        #     computed up front instead of inline below.
        rehash_paths: list[str] = []
        if rehash:
            for db_row in db_rows:
                path = db_row["path_absolute"]
                if not os.path.isfile(path):
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if _iso_utc(st.st_mtime) > (db_row["date_modified"] or ""):
                    rehash_paths.append(path)
        sha_by_path = file_content_sha256_batch(rehash_paths) if rehash_paths else {}

        # 3. Classify each db_row.
        for db_row in db_rows:
            path = db_row["path_absolute"]
            db_sha = db_row["content_sha256"]
            last_ingest = db_row["last_ingest_date"] or ""

            if not os.path.isfile(path):
                # Missing.
                fact_n, promoted_n = _fact_and_promotion_counts(conn, db_row["uuid"])
                report.missing.append(MissingFile(
                    path=path,
                    file_node_uuid=db_row["uuid"],
                    last_ingest_date=last_ingest,
                    fact_count=fact_n,
                    promoted_count=promoted_n,
                ))
                continue

            try:
                st = os.stat(path)
            except OSError:
                continue
            disk_mtime_iso = _iso_utc(st.st_mtime)
            mtime_changed = disk_mtime_iso > (db_row["date_modified"] or "")

            if not mtime_changed:
                continue  # not stale, not touched — quiet good.

            # mtime bumped — confirm with sha256 (precomputed in the batch above).
            if rehash:
                current_sha = sha_by_path.get(path)
                if current_sha is None:
                    # Unreadable at hash time — skip, matching the prior
                    # inline OSError behavior.
                    continue
            else:
                current_sha = ""

            fact_n, promoted_n = _fact_and_promotion_counts(conn, db_row["uuid"])
            stale_record = StaleFile(
                path=path,
                identity_key=db_row["identity_key"],
                last_ingested_version=db_row["version_label"],
                last_ingest_date=last_ingest,
                last_sha256=db_sha,
                current_sha256=current_sha,
                mtime=disk_mtime_iso,
                fact_count=fact_n,
                promoted_count=promoted_n,
                suggested_action="re-ingest" if rehash else "re-ingest (unverified)",
            )
            if rehash and current_sha == db_sha:
                stale_record.suggested_action = "skip (touched-only)"
                report.touched_only.append(stale_record)
            else:
                report.stale.append(stale_record)

        # 4. New files: on disk, not in db.
        for path in sorted(disk_paths - set(db_by_path.keys())):
            try:
                st = os.stat(path)
            except OSError:
                continue
            from .identity import filetype_for
            report.new.append(NewFile(
                path=path,
                size_bytes=st.st_size,
                mtime=_iso_utc(st.st_mtime),
                filetype=filetype_for(path),
            ))

        # 5. Failed extractions.
        if include_failed_extraction:
            sql = (
                "SELECT fn.uuid, fn.path_absolute, "
                "       COUNT(*) AS failed_count, "
                "       (SELECT COUNT(*) FROM leaves WHERE file_node = fn.uuid) AS total_count, "
                "       (SELECT error FROM extraction_attempts ea "
                "        WHERE ea.leaf_uuid = l.uuid AND ea.status = 'failed' "
                "        ORDER BY attempted_at DESC LIMIT 1) AS last_error "
                "FROM leaves l "
                "JOIN file_nodes fn ON fn.uuid = l.file_node "
                "WHERE l.extraction_status = 'failed' "
                "  AND fn.superseded_by IS NULL "
            )
            extra_params: list = []
            if corpus_id:
                sql += "  AND fn.corpus_id = ? "
                extra_params.append(corpus_id)
            if directory:
                sql += "  AND fn.path_absolute LIKE ? "
                extra_params.append(os.path.abspath(directory).replace("%", "[%]") + "%")
            sql += "GROUP BY fn.uuid, fn.path_absolute"
            for r in conn.execute(sql, extra_params).fetchall():
                report.failed_extraction.append(FailedExtraction(
                    path=r["path_absolute"],
                    file_node_uuid=r["uuid"],
                    failed_leaf_count=r["failed_count"],
                    total_leaf_count=r["total_count"],
                    last_error=r["last_error"],
                ))

        # 6. Drifted promotions (source superseded after promotion).
        if include_drifted_promotions:
            sql = (
                "SELECT pm.uuid AS marker_uuid, pm.promoted_to, pm.source_memory, "
                "       pm.reason, fn.path_absolute, fn.superseded_at "
                "FROM promotion_markers pm "
                "JOIN file_nodes fn ON ( "
                "    fn.uuid = pm.source_memory "
                "    OR fn.uuid = (SELECT file_node FROM facts WHERE uuid = pm.source_memory) "
                "    OR fn.uuid = (SELECT file_node FROM leaves WHERE uuid = pm.source_memory) "
                ") "
                "WHERE fn.superseded_by IS NOT NULL"
            )
            for r in conn.execute(sql).fetchall():
                report.drifted_promotions.append(DriftedPromotion(
                    marker_uuid=r["marker_uuid"],
                    promoted_to=r["promoted_to"],
                    source_memory=r["source_memory"],
                    source_path=r["path_absolute"],
                    source_superseded_at=r["superseded_at"],
                    reason=r["reason"],
                ))

        # 7. Rename heuristic. Pair every `missing` file with a `new` file
        #    whose on-disk content_sha256 matches. Exact match only; never
        #    auto-link. User confirms via files_link_rename.
        if include_rename_candidates and report.missing and report.new:
            # Index missing files by their (DB-recorded) sha.
            missing_by_sha: dict[str, MissingFile] = {}
            for m in report.missing:
                row = conn.execute(
                    "SELECT content_sha256 FROM file_nodes WHERE uuid = ?",
                    (m.file_node_uuid,),
                ).fetchone()
                if row:
                    missing_by_sha[row["content_sha256"]] = m

            # Hash each new file once; match against the missing set.
            seen_missing: set[str] = set()
            for n in report.new:
                try:
                    sha = file_content_sha256(n.path)
                except OSError as e:
                    logger.debug("rename: hash failed for %s: %s", n.path, e)
                    continue
                m = missing_by_sha.get(sha)
                if m is None or m.file_node_uuid in seen_missing:
                    continue
                seen_missing.add(m.file_node_uuid)
                report.rename_candidates.append(RenameCandidate(
                    missing_file_node_uuid=m.file_node_uuid,
                    missing_path=m.path,
                    new_path=n.path,
                    content_sha256=sha,
                    confidence="exact_sha",
                ))

    report.duration_ms = int((time.perf_counter() - t0) * 1000)
    return report


def link_rename(
    missing_file_node_uuid: str,
    new_path: str,
    *,
    expect_sha256: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict:
    """Re-point an existing file_node at a new path (rename / move).

    NOT a supersession — the content_sha256 stays identical. The file_node's
    path_absolute is updated, paths_seen is appended with the old path,
    metadata records the rename event. Idempotent: calling with the same
    new_path twice is a no-op (path_absolute already matches).

    Args:
        missing_file_node_uuid: the file_node whose path is being updated.
        new_path: the new on-disk location.
        expect_sha256: if set, the new file's sha256 MUST match. Defends
            against the user pointing at the wrong file.
        db_path: target files.db.
    """
    import json as _json
    new_abs = os.path.abspath(new_path)
    if not os.path.isfile(new_abs):
        raise FileNotFoundError(f"new_path is not a file: {new_abs}")

    actual_sha = file_content_sha256(new_abs)
    if expect_sha256 and actual_sha != expect_sha256:
        raise ValueError(
            f"sha mismatch: expected {expect_sha256!r}, got {actual_sha!r}"
        )

    with _db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT uuid, path_absolute, content_sha256, paths_seen, metadata "
            "FROM file_nodes WHERE uuid = ?",
            (missing_file_node_uuid,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no file_node {missing_file_node_uuid!r}")
        if row["content_sha256"] != actual_sha:
            raise ValueError(
                "file_node content_sha256 differs from on-disk sha; "
                "cannot link a rename (content has changed too). "
                "Re-ingest the file instead, which will properly supersede."
            )
        if row["path_absolute"] == new_abs:
            return {
                "file_node_uuid": missing_file_node_uuid,
                "action": "noop",
                "path": new_abs,
            }

        # Build the updated paths_seen list (prepend new, keep old).
        try:
            paths_seen = _json.loads(row["paths_seen"] or "[]")
        except (ValueError, TypeError):
            paths_seen = []
        if row["path_absolute"] not in paths_seen:
            paths_seen.append(row["path_absolute"])
        paths_seen = [new_abs] + [p for p in paths_seen if p != new_abs]

        # Annotate metadata with the rename event.
        try:
            md = _json.loads(row["metadata"] or "{}")
        except (ValueError, TypeError):
            md = {}
        renames = md.get("rename_history") or []
        import datetime as _dt
        renames.append({
            "from": row["path_absolute"],
            "to": new_abs,
            "at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "method": "files_link_rename",
        })
        md["rename_history"] = renames

        conn.execute(
            "UPDATE file_nodes SET path_absolute = ?, paths_seen = ?, metadata = ? "
            "WHERE uuid = ?",
            (new_abs, _json.dumps(paths_seen), _json.dumps(md), missing_file_node_uuid),
        )

    return {
        "file_node_uuid": missing_file_node_uuid,
        "action": "linked",
        "old_path": row["path_absolute"],
        "new_path": new_abs,
    }


def _fact_and_promotion_counts(conn: sqlite3.Connection, file_node_uuid: str) -> tuple[int, int]:
    """Return (fact_count, promoted_count) for a file_node."""
    facts = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE file_node = ?", (file_node_uuid,),
    ).fetchone()[0]
    # Promoted = anything in promotion_markers whose source is owned by this file_node.
    promoted = conn.execute(
        "SELECT COUNT(*) FROM promotion_markers pm "
        "WHERE pm.source_memory = ? "
        "   OR pm.source_memory IN (SELECT uuid FROM leaves WHERE file_node = ?) "
        "   OR pm.source_memory IN (SELECT uuid FROM facts WHERE file_node = ?)",
        (file_node_uuid, file_node_uuid, file_node_uuid),
    ).fetchone()[0]
    return (facts, promoted)


def _iso_utc(ts: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()
