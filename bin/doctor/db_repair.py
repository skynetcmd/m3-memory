"""Legacy DB-repair phase of memory_doctor.

Three small repair passes the original 2025-era memory_doctor.py
shipped with. Kept verbatim semantically — the goal of B19 is the
package split, not a behavior change.

Each repair is idempotent and logs what it touched. Bundled under a
single `run()` entry point that owns the SQLite connection lifecycle.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys

logger = logging.getLogger("memory.doctor.db_repair")


def fix_missing_timestamps(conn: sqlite3.Connection) -> int:
    """Backfill NULL created_at on memory_items. Returns rows touched."""
    from m3_core.runtime import iso_utc_timestamp
    logger.info("Checking for missing timestamps...")
    res = conn.execute(
        "UPDATE memory_items SET created_at = ? WHERE created_at IS NULL",
        (iso_utc_timestamp(),),
    )
    if res.rowcount:
        logger.info(f"Fixed {res.rowcount} items with missing created_at.")
    return res.rowcount


def validate_relationships(conn: sqlite3.Connection) -> int:
    """Prune relationships pointing to non-existent items. Returns rows deleted."""
    logger.info("Validating relationship integrity...")
    res = conn.execute(
        "DELETE FROM memory_relationships "
        "WHERE from_id NOT IN (SELECT id FROM memory_items) "
        "   OR to_id   NOT IN (SELECT id FROM memory_items)"
    )
    if res.rowcount:
        logger.info(f"Pruned {res.rowcount} orphaned relationships.")
    return res.rowcount


def fix_metadata_json(conn: sqlite3.Connection) -> int:
    """Replace invalid JSON in memory_items.metadata_json with '{}'.
    Returns rows touched."""
    logger.info("Validating metadata JSON strings...")
    cursor = conn.execute(
        "SELECT id, metadata_json FROM memory_items WHERE metadata_json IS NOT NULL"
    )
    fixed = 0
    for rid, meta in cursor.fetchall():
        if not meta:
            continue
        try:
            json.loads(meta)
        except json.JSONDecodeError:
            logger.warning(f"Repairing invalid JSON for item {rid}")
            conn.execute(
                "UPDATE memory_items SET metadata_json = '{}' WHERE id = ?",
                (rid,),
            )
            fixed += 1
    if fixed:
        logger.info(f"Repaired {fixed} items with invalid metadata JSON.")
    return fixed


def run(db_path: str | None = None) -> int:
    """Run all three repairs against the resolved DB.

    Returns 0 on success, 1 on any unhandled exception (rolls back the
    transaction). Missing DB file is a fatal error.
    """
    # Late import: keep this module importable without dragging m3_sdk
    # into the package's import surface.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
    from m3_sdk import resolve_db_path

    resolved = resolve_db_path(db_path)
    if not os.path.exists(resolved):
        logger.error(f"Database not found at {resolved}")
        return 1

    conn = sqlite3.connect(resolved)
    try:
        fix_missing_timestamps(conn)
        validate_relationships(conn)
        fix_metadata_json(conn)
        conn.commit()
        logger.info("Memory health check and repair completed.")
        return 0
    except Exception as e:
        logger.error(f"db_repair failed: {e}")
        conn.rollback()
        return 1
    finally:
        conn.close()
