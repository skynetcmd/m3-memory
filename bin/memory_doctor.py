#!/usr/bin/env python3
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format='%(name)s: [%(levelname)s] %(message)s')
logger = logging.getLogger("memory_doctor")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")

def fix_missing_timestamps(conn):
    """Ensures all items have at least a created_at timestamp."""
    logger.info("Checking for missing timestamps...")
    now = datetime.now(timezone.utc).isoformat() + "Z"

    # Fix items with NULL created_at
    res = conn.execute(
        "UPDATE memory_items SET created_at = ? WHERE created_at IS NULL",
        (now,)
    )
    if res.rowcount:
        logger.info(f"Fixed {res.rowcount} items with missing created_at.")

def validate_relationships(conn):
    """Prunes relationships pointing to non-existent items."""
    logger.info("Validating relationship integrity...")
    res = conn.execute("""
        DELETE FROM memory_relationships
        WHERE from_id NOT IN (SELECT id FROM memory_items)
           OR to_id NOT IN (SELECT id FROM memory_items)
    """)
    if res.rowcount:
        logger.info(f"Pruned {res.rowcount} orphaned relationships.")

def fix_metadata_json(conn):
    """Ensures metadata_json is valid JSON."""
    logger.info("Validating metadata JSON strings...")
    cursor = conn.execute("SELECT id, metadata_json FROM memory_items WHERE metadata_json IS NOT NULL")
    rows = cursor.fetchall()
    fixed = 0
    for rid, meta in rows:
        try:
            if meta:
                json.loads(meta)
        except json.JSONDecodeError:
            logger.warning(f"Repairing invalid JSON for item {rid}")
            conn.execute("UPDATE memory_items SET metadata_json = '{}' WHERE id = ?", (rid,))
            fixed += 1
    if fixed:
        logger.info(f"Repaired {fixed} items with invalid metadata JSON.")

def main():
    if not os.path.exists(DB_PATH):
        logger.error(f"Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    try:
        fix_missing_timestamps(conn)
        validate_relationships(conn)
        fix_metadata_json(conn)
        conn.commit()
        logger.info("Memory health check and repair completed.")
    except Exception as e:
        logger.error(f"Doctor failed: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    main()
