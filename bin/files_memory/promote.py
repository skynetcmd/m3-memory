"""Ascension — promote items from files.db to memory.db.

A promotion is a COPY: the source memory in files.db stays untouched,
and a new row in memory.db is created with metadata back-pointer
(source_path, source_version, source_memory_id). A promotion_marker
row in files.db links the two so we can find what's been promoted.

What can be promoted:
  - 'fact'           — extracted fact in files.db.facts
  - 'leaf'           — a leaf (snippet of text) in files.db.leaves
  - 'file_summary'   — the file_summary on a file_node

Type mapping (plan §14 Q7):
  fact         → memory_items.type='fact'
  leaf         → memory_items.type='knowledge'  (default; can override)
  file_summary → memory_items.type='reference'  (default; can override)

Promotion is manual by design in phase 2. Heuristic suggestions and
auto-promote-on-ingest land in phase 3.

Public API:
    files_promote(source_uuid, reason, mapped_type=None,
                  scope=None, importance=None, agent_id=None) -> dict
    files_promotion_list(source_file_node=None, source_superseded=None) -> list
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import uuid as _uuid
from typing import Optional

from . import config
from .db import _db

logger = logging.getLogger("files_memory.promote")


# ──────────────────────────────────────────────────────────────────────────────
# Source resolution
# ──────────────────────────────────────────────────────────────────────────────
def _resolve_source(conn: sqlite3.Connection, source_uuid: str) -> Optional[dict]:
    """Find a fact / leaf / file_node by UUID in files.db.

    Returns a dict with at minimum:
        kind:        'fact' | 'leaf' | 'file_summary'
        content:     the text to promote
        title:       a short title
        file_node:   the file_node UUID (where applicable)
        leaf:        the leaf UUID (where applicable)
        source_path: filesystem path at promotion time
        version_label: ingest-N label
    """
    # Try facts first (most likely target).
    row = conn.execute(
        "SELECT f.uuid AS fact_uuid, f.statement, f.confidence, f.leaf, "
        "       f.file_node, l.text AS leaf_text, l.division_label, "
        "       fn.filename, fn.path_absolute, fn.version_label "
        "FROM facts f "
        "JOIN leaves l ON l.uuid = f.leaf "
        "JOIN file_nodes fn ON fn.uuid = f.file_node "
        "WHERE f.uuid = ?",
        (source_uuid,),
    ).fetchone()
    if row:
        return {
            "kind": "fact",
            "content": row["statement"],
            "title": f"{row['filename']} · {row['division_label'] or ''}".strip(" ·"),
            "fact_uuid": row["fact_uuid"],
            "leaf": row["leaf"],
            "file_node": row["file_node"],
            "source_path": row["path_absolute"],
            "version_label": row["version_label"],
            "confidence": row["confidence"],
        }

    # Try leaves.
    row = conn.execute(
        "SELECT l.uuid AS leaf_uuid, l.text, l.leaf_summary, l.division_type, "
        "       l.division_id, l.division_label, l.file_node, "
        "       fn.filename, fn.path_absolute, fn.version_label "
        "FROM leaves l "
        "JOIN file_nodes fn ON fn.uuid = l.file_node "
        "WHERE l.uuid = ?",
        (source_uuid,),
    ).fetchone()
    if row:
        return {
            "kind": "leaf",
            "content": row["text"],
            "title": f"{row['filename']} · {row['division_label'] or row['division_type'] + ':' + row['division_id']}".strip(),
            "leaf": row["leaf_uuid"],
            "file_node": row["file_node"],
            "source_path": row["path_absolute"],
            "version_label": row["version_label"],
            "leaf_summary": row["leaf_summary"],
        }

    # Try file_summaries.
    row = conn.execute(
        "SELECT uuid, filename, file_summary, path_absolute, version_label "
        "FROM file_nodes WHERE uuid = ?",
        (source_uuid,),
    ).fetchone()
    if row:
        if not row["file_summary"]:
            return None
        return {
            "kind": "file_summary",
            "content": row["file_summary"],
            "title": f"summary of {row['filename']}",
            "file_node": row["uuid"],
            "source_path": row["path_absolute"],
            "version_label": row["version_label"],
        }

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Type mapping
# ──────────────────────────────────────────────────────────────────────────────
_DEFAULT_TYPE_MAP = {
    "fact": "fact",
    "leaf": "knowledge",
    "file_summary": "reference",
}


def _default_type_for(kind: str, override: Optional[str]) -> str:
    if override:
        return override
    return _DEFAULT_TYPE_MAP.get(kind, "knowledge")


def _find_promoted_orphan(source_uuid: str) -> Optional[str]:
    """Look in memory.db for a row that was promoted from `source_uuid` but
    whose marker is missing in files.db. Returns the memory_items.id or None.

    Used by files_promote() for orphan recovery — see promote() docstring.
    """
    from .entities import _memory_db  # reuse the same connection helper
    try:
        with _memory_db() as conn:
            row = conn.execute(
                "SELECT id FROM memory_items "
                "WHERE json_extract(metadata_json, '$.promoted_from') = 'files.db' "
                "  AND json_extract(metadata_json, '$.source_memory_id') = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (source_uuid,),
            ).fetchone()
            return row[0] if row else None
    except (FileNotFoundError, sqlite3.Error) as e:
        logger.debug("_find_promoted_orphan failed: %s", e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Promotion
# ──────────────────────────────────────────────────────────────────────────────
def files_promote(
    source_uuid: str,
    *,
    reason: str = "",
    mapped_type: Optional[str] = None,
    scope: Optional[str] = None,
    importance: float = 0.6,
    agent_id: str = "files_memory.promote",
    db_path: Optional[str] = None,
) -> dict:
    """Promote one item from files.db into memory.db.

    Returns a dict:
      {
        "promoted_to": <memory.db UUID>,
        "marker_uuid": <files.db promotion_markers UUID>,
        "kind": "fact"|"leaf"|"file_summary",
        "mapped_type": <memory type used>,
        "source_uuid": <input>,
      }

    Idempotent: if source_uuid was already promoted (a marker exists),
    returns the existing promotion's record without writing a new one.
    """
    if scope is None:
        scope = config.PROMOTION_DEFAULT_SCOPE

    with _db(db_path) as conn:
        source = _resolve_source(conn, source_uuid)
        if source is None:
            raise ValueError(f"no fact/leaf/file_node found for UUID {source_uuid!r}")

        # Idempotency: was this already promoted? (marker exists)
        existing = conn.execute(
            "SELECT uuid, promoted_to, mapped_type FROM promotion_markers "
            "WHERE source_memory = ? "
            "ORDER BY promoted_at DESC LIMIT 1",
            (source_uuid,),
        ).fetchone()
        if existing:
            return {
                "promoted_to": existing["promoted_to"],
                "marker_uuid": existing["uuid"],
                "kind": source["kind"],
                "mapped_type": existing["mapped_type"],
                "source_uuid": source_uuid,
                "already_promoted": True,
            }

        promoted_type = _default_type_for(source["kind"], mapped_type)

        # Orphan recovery: a prior promote may have written to memory.db
        # but failed before writing the marker (cross-DB writes are not
        # atomic). Look for an existing memory_items row whose metadata
        # back-points at our source_uuid; if found, re-attach by writing
        # the missing marker instead of creating a second memory.db row.
        orphan = _find_promoted_orphan(source_uuid)
        if orphan:
            marker_uuid = str(_uuid.uuid4())
            conn.execute(
                "INSERT INTO promotion_markers("
                "uuid, source_memory, source_memory_type, promoted_to, promoted_by, "
                "reason, mapped_type, memory_db_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    marker_uuid, source_uuid, source["kind"],
                    orphan, agent_id,
                    f"{reason} [orphan-recovered]" if reason else "[orphan-recovered]",
                    promoted_type, config.MEMORY_DB_PATH,
                ),
            )
            logger.info(
                "promote: recovered orphan memory_items row %s for source %s",
                orphan, source_uuid,
            )
            return {
                "promoted_to": orphan,
                "marker_uuid": marker_uuid,
                "kind": source["kind"],
                "mapped_type": promoted_type,
                "source_uuid": source_uuid,
                "orphan_recovered": True,
            }

        # Write to memory.db via memory_write_impl. Async function, so we
        # run it in a private event loop. Note: we hold the files.db txn
        # open while doing the cross-DB write; if the memory.db write
        # fails we roll back the files.db marker.
        memory_uuid = _write_to_memory_db(
            content=source["content"],
            title=source["title"],
            mtype=promoted_type,
            scope=scope,
            importance=importance,
            agent_id=agent_id,
            metadata={
                "promoted_from": "files.db",
                "source_memory_id": source_uuid,
                "source_memory_kind": source["kind"],
                "source_file_node": source.get("file_node"),
                "source_leaf": source.get("leaf"),
                "source_path": source["source_path"],
                "source_version_label": source["version_label"],
                "promotion_reason": reason,
            },
        )
        if not memory_uuid:
            raise RuntimeError("memory_write returned no UUID; promotion aborted")

        # Write the marker in files.db.
        marker_uuid = str(_uuid.uuid4())
        conn.execute(
            "INSERT INTO promotion_markers("
            "uuid, source_memory, source_memory_type, promoted_to, promoted_by, "
            "reason, mapped_type, memory_db_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                marker_uuid, source_uuid, source["kind"],
                memory_uuid, agent_id, reason, promoted_type,
                config.MEMORY_DB_PATH,  # NULL = active M3Context default
            ),
        )

        return {
            "promoted_to": memory_uuid,
            "marker_uuid": marker_uuid,
            "kind": source["kind"],
            "mapped_type": promoted_type,
            "source_uuid": source_uuid,
            "already_promoted": False,
        }


def _write_to_memory_db(
    content: str,
    title: str,
    mtype: str,
    scope: str,
    importance: float,
    agent_id: str,
    metadata: dict,
) -> Optional[str]:
    """Run memory.memory_write_impl from this sync context.

    Returns the new memory_items.id, or None on failure.

    memory_write_impl is async; we run it in a private event loop so we
    don't fight an outer async context (the file ingester is sync, but
    if a caller wraps us in async, get_event_loop().run_until_complete
    would raise — fresh loop avoids that).
    """
    # Resolve memory_write_impl. After the Phase 7+8 refactor (commit
    # bd07525) it lives in `memory.write`; the `memory_core` shim still
    # re-exports it for parity. Try the canonical path first, fall back
    # to the shim — this keeps us forward-compatible without breaking on
    # older trees.
    memory_write_impl = None
    try:
        from memory.write import memory_write_impl  # type: ignore
    except ImportError:
        try:
            from memory_core import memory_write_impl  # type: ignore
        except ImportError as e:
            logger.error(
                "neither memory.write nor memory_core exposes memory_write_impl: %s", e,
            )
            return None

    async def _run():
        return await memory_write_impl(
            type=mtype,
            content=content,
            title=title,
            metadata=json.dumps(metadata),
            agent_id=agent_id,
            importance=float(importance),
            source="files_memory.promote",
            scope=scope,
        )

    try:
        # asyncio.run requires no running loop. We're sync, so this is fine.
        result = asyncio.run(_run())
    except RuntimeError as e:
        # Already in an event loop — run in a thread.
        if "running event loop" in str(e):
            import threading
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                result = ex.submit(lambda: asyncio.run(_run())).result()
        else:
            raise

    # memory_write_impl returns the new uuid as a string ("Created: <uuid>")
    # in some paths and the uuid directly in others. Handle both shapes.
    if isinstance(result, str):
        # Pattern: "Created: 1234abcd..." or just the uuid
        if result.startswith("Created: "):
            return result.split(": ", 1)[1].strip()
        if result.startswith("Error"):
            logger.error("memory_write_impl returned error: %s", result)
            return None
        return result.strip()
    if isinstance(result, dict):
        return result.get("id") or result.get("uuid")
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Listing
# ──────────────────────────────────────────────────────────────────────────────
def files_promotion_list(
    *,
    source_file_node: Optional[str] = None,
    source_superseded: Optional[bool] = None,
    limit: int = 100,
    db_path: Optional[str] = None,
) -> list[dict]:
    """List promotions, optionally filtered.

    Args:
        source_file_node: only promotions whose source belongs to this
            file_node (whether the source is a fact, leaf, or summary).
        source_superseded: if True, only promotions whose source file_node
            is now superseded (candidates for promotion review).
        limit: cap on results.
    """
    sql_parts = [
        "SELECT pm.uuid AS marker_uuid, pm.source_memory, pm.source_memory_type, "
        "       pm.promoted_to, pm.promoted_at, pm.reason, pm.mapped_type, "
        "       fn.uuid AS file_node_uuid, fn.filename, fn.path_absolute, "
        "       fn.version_label, fn.superseded_by, fn.superseded_at "
        "FROM promotion_markers pm "
        "LEFT JOIN file_nodes fn ON ( "
        "    fn.uuid = pm.source_memory "
        "    OR fn.uuid = (SELECT file_node FROM facts WHERE uuid = pm.source_memory) "
        "    OR fn.uuid = (SELECT file_node FROM leaves WHERE uuid = pm.source_memory) "
        ") "
        "WHERE 1 = 1"
    ]
    params: list = []
    if source_file_node:
        sql_parts.append("AND fn.uuid = ?")
        params.append(source_file_node)
    if source_superseded is True:
        sql_parts.append("AND fn.superseded_by IS NOT NULL")
    elif source_superseded is False:
        sql_parts.append("AND fn.superseded_by IS NULL")
    sql_parts.append("ORDER BY pm.promoted_at DESC LIMIT ?")
    params.append(limit)

    with _db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(" ".join(sql_parts), params).fetchall()

    return [
        {
            "marker_uuid": r["marker_uuid"],
            "source_memory": r["source_memory"],
            "source_memory_type": r["source_memory_type"],
            "promoted_to": r["promoted_to"],
            "promoted_at": r["promoted_at"],
            "reason": r["reason"],
            "mapped_type": r["mapped_type"],
            "filename": r["filename"],
            "source_path": r["path_absolute"],
            "version_label": r["version_label"],
            "source_superseded": bool(r["superseded_by"]),
            "source_superseded_at": r["superseded_at"],
        }
        for r in rows
    ]
