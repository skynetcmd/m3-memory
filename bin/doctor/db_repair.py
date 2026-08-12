"""Legacy DB-repair phase of memory_doctor.

Three small repair passes the original 2025-era memory_doctor.py
shipped with. Kept verbatim semantically — the goal of B19 is the
package split, not a behavior change.

Each repair is idempotent and logs what it touched. Bundled under a
single `run()` entry point.

Backend discipline (§10a): every statement here goes through the storage seam.
This module used to call ``sqlite3.connect(resolve_db_path(...))`` directly,
which had two consequences on a PostgreSQL-primary install:

  * ``db_path`` is a LABEL on a pooled backend, not a location (§10). The
    installer separately created an unused ``agent_memory.db``, so the guard
    found a file, opened it, "repaired" an EMPTY database, and reported
    "Memory health check and repair completed." A false green over the wrong
    store — the worst failure shape (§3 fail loud, §5 does it actually work).
  * The metadata pass SELECTed every row, parsed each in Python, and issued a
    per-row UPDATE. On a pooled backend that ships the whole table and pays N
    round-trips (§4, §8). It is now one set-based statement via
    ``Dialect.json_is_valid``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("memory.doctor.db_repair")


def fix_missing_timestamps(conn, dialect) -> int:
    """Backfill NULL created_at on memory_items. Returns rows touched."""
    from m3_core.runtime import iso_utc_timestamp
    logger.info("Checking for missing timestamps...")
    res = conn.execute(
        f"UPDATE memory_items SET created_at = {dialect.placeholder()} "
        "WHERE created_at IS NULL",
        (iso_utc_timestamp(),),
    )
    touched = res.rowcount if res is not None else 0
    if touched and touched > 0:
        logger.info(f"Fixed {touched} items with missing created_at.")
    return max(touched, 0)


def validate_relationships(conn, dialect) -> int:
    """Prune relationships pointing to non-existent items. Returns rows deleted."""
    logger.info("Validating relationship integrity...")
    res = conn.execute(
        "DELETE FROM memory_relationships "
        "WHERE from_id NOT IN (SELECT id FROM memory_items) "
        "   OR to_id   NOT IN (SELECT id FROM memory_items)"
    )
    touched = res.rowcount if res is not None else 0
    if touched and touched > 0:
        logger.info(f"Pruned {touched} orphaned relationships.")
    return max(touched, 0)


def fix_metadata_json(conn, dialect) -> int:
    """Replace unparseable metadata_json with an empty object. Rows touched.

    Set-based: the dialect renders the validity test, so the server does the
    scan. ``IS NOT NULL`` is required because SQLite's ``json_valid(NULL)``
    yields NULL rather than 0 — a NULL here means "no metadata", not "corrupt
    metadata", and must not be rewritten.

    On PostgreSQL ``metadata_json`` is JSONB, so the column type already
    guarantees parseability and the expression is a constant TRUE — this pass
    correctly matches nothing instead of scanning.
    """
    logger.info("Validating metadata JSON strings...")
    empty = dialect.empty_json_default() or "{}"
    res = conn.execute(
        f"UPDATE memory_items SET metadata_json = {dialect.placeholder()} "
        f"WHERE metadata_json IS NOT NULL AND NOT {dialect.json_is_valid('metadata_json')}",
        (empty,),
    )
    touched = res.rowcount if res is not None else 0
    if touched and touched > 0:
        logger.info(f"Repaired {touched} items with invalid metadata JSON.")
    return max(touched, 0)


def run(db_path: str | None = None) -> int:
    """Run the three repair passes against the ACTIVE store.

    Returns 0 on success, 1 on any unhandled exception.

    ``db_path`` is accepted for call-compatibility and honored only where it is
    meaningful (a file backend); on a pooled backend the seam resolves the one
    store and ignores it. There is deliberately no ``os.path.exists`` guard: on
    a pooled backend that check inspects a path that is not the database (§10),
    and it was how this module came to repair the wrong file.
    """
    import sys
    sys.path.insert(0, __file__.rsplit("db_repair.py", 1)[0] + "..")

    try:
        from memory.backends import active_backend
    except Exception as e:  # noqa: BLE001 — fail loud, never silently skip
        logger.error(f"db_repair: storage seam not loadable: {type(e).__name__}: {e}")
        return 1

    try:
        backend = active_backend()
        dialect = backend.dialect()
    except Exception as e:  # noqa: BLE001 — e.g. PG selected with no DSN
        logger.error(f"db_repair: no usable backend: {e}")
        return 1

    try:
        with backend.connection() as conn:
            fix_missing_timestamps(conn, dialect)
            validate_relationships(conn, dialect)
            fix_metadata_json(conn, dialect)
            commit = getattr(conn, "commit", None)
            if callable(commit):
                commit()
        logger.info("Memory health check and repair completed.")
        return 0
    except Exception as e:  # noqa: BLE001 — report, don't crash the doctor
        logger.error(f"db_repair failed: {e}")
        return 1
